# -*- coding: utf-8 -*-
"""语音拟人化：场景情绪识别 → 参数化情绪判别 → 多情绪标签混合 → 人设偏置。

设计要点（为什么这么做）：
  1. 两个 TTS 后端（MiMo / Voice Studio）都不接受数值情绪字段，只吃一句中文
     instruction，因此「多情绪标签」最终混合成一句风格指令；数值语速交给已有的
     ffmpeg atempo 后处理，音高/音量写成指令文字，避免为了拟人化去改后端协议。
  2. 判别不是 if/else 硬编码，而是一个可热更新的参数模型：
     关键词 / NLP 模型 / 场景先验 / 上下文延续 / 人设共情 五路信号按 JSON 里的
     权重加权打分，阈值 + top-k 产出多标签。权重未训练时模型那一路自动为 0，
     规则仍能正常工作。
  3. 人设不是写死的：从 assets/persona.json 实时读「说话风格.语气」做风格底座、
     「情感.共情规则」做情绪/语速偏置、「性格.核心特质」做强度修正、
     「说话风格.禁止」做输出约束。改人设即时生效，无需重启。

对外主入口：resolve(text, mtype=None, ctx=None, base_style=None) -> VoiceParams | None
"""
import os
import sys
import re
import json
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 可选 NLP 模块：缺依赖 / 缺权重时各自回退为 None，本模块照常工作
try:
    import emotion_model
except Exception:
    emotion_model = None
try:
    import nlp_understanding
except Exception:
    nlp_understanding = None
try:
    import intent_model
except Exception:
    intent_model = None

PARAMS_FILE = os.path.join(_ROOT, "assets", "voice_emotion_params.json")
PERSONA_FILE = os.path.join(_ROOT, "assets", "persona.json")
_CACHE = {"params": None, "params_mtime": -1, "persona": None, "persona_mtime": -1}
_PUNCT = "！？!?，。、~～"
# 意图模型权重未训练时的轻量兜底：把「听故事」类请求归到 story 场景
_STORY_RE = re.compile(
    r"(讲|说|编|来|给|想听|听个).{0,8}(?:恐怖|鬼|吓人|灵异|悬疑|温暖|搞笑|有趣)?\s*(故事|童话|笑话|段子)"
    r"|我想听故事|来个段子|编个故事"
)


def _read_hot(path, key, mkey):
    """按 mtime 读 JSON（与项目里 load_persona / load_voice_config 同样的热更新套路）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1
    if _CACHE[key] is not None and _CACHE[mkey] == mtime:
        return _CACHE[key]
    data = {}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"[语音拟人化] 读取 {os.path.basename(path)} 失败：{e}")
    if not isinstance(data, dict):
        data = {}
    _CACHE[key] = data
    _CACHE[mkey] = mtime
    return data


def load_params():
    return _read_hot(PARAMS_FILE, "params", "params_mtime")


def load_persona():
    return _read_hot(PERSONA_FILE, "persona", "persona_mtime")


@dataclass
class VoiceParams:
    """一次合成所需的拟人化参数。style 为空表示「不覆盖上游显式 style」。"""
    style: str = ""
    speed_ratio: float = 1.0      # 相对 voice_speed 的倍率，主程序乘到基准语速上
    effect: str = ""              # 空 = 不覆盖全局 ffmpeg 音效
    scene: str = "chat"
    labels: list = field(default_factory=list)   # [(情绪标签, 得分)]
    intensity: float = 0.0
    signals: dict = field(default_factory=dict)
    debug: str = ""


def _is_punct_kw(kw):
    return isinstance(kw, str) and len(kw) == 1 and kw in _PUNCT


def _norm_label(x, emo_cfg, aliases):
    """把 30 类细粒度情绪 / 别名归一到配置里的 19 类。"""
    if not isinstance(x, str):
        return None
    if x in emo_cfg:
        return x
    return aliases.get(x)


def _model_labels(text):
    """统一多任务模型 + 情绪模型；权重未训练时返回空，由关键词那一路兜底。"""
    out = []
    if nlp_understanding is not None:
        try:
            r = nlp_understanding.predict(text)
            if r:
                out.extend(r.get("emotions") or [])
        except Exception:
            pass
    if emotion_model is not None:
        try:
            out.extend(emotion_model.predict_emotions(text) or [])
        except Exception:
            pass
    return out


def recognize_scene(text, mtype=None, ctx=None):
    """场景情绪识别：消息类型 + 意图 + 被@/私聊/主人 + 文本语义 → 场景名。

    场景名对应 voice_emotion_params.json 的 scenes 键，决定先验与语速偏置。
    """
    ctx = ctx or {}
    t = text or ""
    valid = {"chat", "proactive", "story", "command", "greeting", "private", "at", "comfort", "celebrate", "apology"}
    hint = ctx.get("scene_hint")
    base = hint if isinstance(hint, str) and hint in valid else (
        mtype if isinstance(mtype, str) and mtype in valid else "chat")
    if ctx.get("is_private") and base == "chat":
        base = "private"

    intent = None
    if intent_model is not None:
        try:
            intent = intent_model.predict_intent(t)
        except Exception:
            pass
    if intent == "story" or _STORY_RE.search(t):
        base = "story"
    # 道歉兜底：小忺自己的回复里出现歉意词（mtype="apology" 已由调用方优先设置）
    if re.search(r"抱歉|对不起|我的错|我错了|认错|愧疚|反省|疏忽|失误|不该这样|不好意思", t):
        base = "apology"

    comfort_kw = ("难过", "伤心", "哭", "委屈", "好累", "崩溃", "想哭", "不开心", "低落", "安慰", "难受")
    joy_kw = ("好开心", "太棒", "中奖", "成功", "好耶", "庆祝", "恭喜", "通过了", "太好了")
    if base == "story":
        scene = "story"
    elif any(k in t for k in comfort_kw):
        scene = "comfort"
    elif any(k in t for k in joy_kw):
        scene = "celebrate"
    elif ctx.get("is_at") and base in ("chat", "private"):
        scene = "at"
    else:
        scene = base

    signals = {
        "base": base,
        "intent": intent,
        "is_at": bool(ctx.get("is_at")),
        "is_private": bool(ctx.get("is_private")),
        "is_owner": bool(ctx.get("is_owner")),
        "recent_count": len(ctx.get("recent_texts") or []),
        "model_available": emotion_model is not None or nlp_understanding is not None,
    }
    return scene, signals


def _persona_bias(persona, cfg, emo_cfg, aliases, triggered):
    """把人设转成偏置：只启用与当前文本情绪相关的共情规则，避免所有规则一起抬高分数。"""
    out = {"labels": [], "spd_bias": 0.0, "it_scale": 1.0, "hints": [],
           "constraints": [], "penalty": {}, "tone": "", "active": []}
    if not isinstance(persona, dict):
        return out

    style = persona.get("说话风格") or {}
    emo = persona.get("情感") or {}
    char = persona.get("性格") or {}

    if cfg.get("use_tone_base", True) and isinstance(style, dict):
        tone = style.get("语气")
        if isinstance(tone, str) and tone.strip():
            out["tone"] = tone.strip()

    empathy = emo.get("共情规则") or {}
    emap = cfg.get("empathy_map") or {}
    if isinstance(empathy, dict) and isinstance(emap, dict):
        for key, rule in emap.items():
            if key not in empathy or not isinstance(rule, dict):
                continue
            labs = {_norm_label(x, emo_cfg, aliases) for x in (rule.get("labels") or [])}
            labs.discard(None)
            if not (labs & set(triggered)):
                continue
            out["active"].append(key)
            out["labels"].extend(labs)
            out["spd_bias"] += float(rule.get("spd_bias", 0.0))
            if rule.get("hint"):
                out["hints"].append(str(rule["hint"]))

    traits = char.get("核心特质") or []
    tmap = cfg.get("trait_bias") or {}
    if isinstance(traits, list) and isinstance(tmap, dict):
        for tr in traits:
            rule = tmap.get(tr)
            if isinstance(rule, dict):
                out["it_scale"] *= float(rule.get("it_scale", 1.0))
                out["spd_bias"] += float(rule.get("spd_bias", 0.0))

    forbids = style.get("禁止") or []
    fmap = cfg.get("forbid_map") or {}
    if isinstance(forbids, list) and isinstance(fmap, dict):
        for fb in forbids:
            rule = None
            if isinstance(fb, str):
                rule = fmap.get(fb)
                if rule is None:
                    # 人设里可能写成「太过热情，适当吃醋」这类长句，做子串兜底
                    for k, v in fmap.items():
                        if k and (k in fb or fb in k):
                            rule = v
                            break
            if not isinstance(rule, dict):
                continue
            for lab, v in (rule.get("penalty") or {}).items():
                n = _norm_label(lab, emo_cfg, aliases)
                if n:
                    out["penalty"][n] = max(out["penalty"].get(n, 0.0), float(v))
            if rule.get("constraint"):
                out["constraints"].append(str(rule["constraint"]))
    if cfg.get("default_constraint"):
        out["constraints"].append(str(cfg["default_constraint"]))
    out["constraints"] = list(dict.fromkeys(out["constraints"]))
    return out


def score_emotions(text, scene, ctx, params, persona):
    """参数化判别：五路信号加权打分，返回 {标签: 0~1 得分} 与人设偏置。"""
    emo_cfg = params.get("emotions") or {}
    aliases = params.get("aliases") or {}
    dec = params.get("decision") or {}
    W = dec.get("weights") or {}
    scene_cfg = (params.get("scenes") or {}).get(scene) or {}
    pcfg = params.get("persona") or {}
    t = text or ""
    raw = {}        # 标签 -> {信号源: 分值}
    direct = set()  # 由文本/模型/上下文直接命中的标签（不含场景先验与人设回灌）

    def add(lab, src, val, is_direct=False):
        if lab not in emo_cfg or val <= 0:
            return
        d = raw.setdefault(lab, {})
        d[src] = max(d.get(src, 0.0), float(val))
        if is_direct:
            direct.add(lab)

    # ① 关键词（标点类降权，避免一个「？」把「疑问」顶到满分）
    for lab, cfg in emo_cfg.items():
        hits = 0.0
        for kw in (cfg.get("kw") or []):
            if isinstance(kw, str) and kw and kw in t:
                hits += 0.4 if _is_punct_kw(kw) else 1.0
        if hits:
            # 关键词表都是明确情绪词，命中 1 个即视为强信号；标点类已按 0.4 降权，
            # 单独一个「？」不足以触发「疑问」，避免疑问句被无差别打上情绪
            add(lab, "keyword", min(hits, 1.0), is_direct=True)

    # ② 模型输出（30 类细粒度 / 19 类多标签，经别名归一）
    for lab in _model_labels(t):
        n = _norm_label(lab, emo_cfg, aliases)
        if n:
            add(n, "model", 1.0, is_direct=True)

    # ③ 场景先验（不算直接命中，否则人设共情规则会被无差别激活）
    for lab, v in (scene_cfg.get("prior") or {}).items():
        try:
            add(lab, "scene", float(v))
        except (TypeError, ValueError):
            pass

    # ④ 上下文情绪延续：最近几条消息里出现的情绪词，给一点惯性
    for rt in (ctx or {}).get("recent_texts") or []:
        if not isinstance(rt, str):
            continue
        for lab, cfg in emo_cfg.items():
            if any(k in rt for k in (cfg.get("kw") or [])
                   if isinstance(k, str) and k and not _is_punct_kw(k)):
                add(lab, "context", 0.5, is_direct=True)

    # ⑤ 人设共情：只启用由实词/模型明确命中的情绪（标点类弱信号不足以触发共情）
    strong = {lab for lab in direct
              if raw.get(lab, {}).get("keyword", 0.0) >= 0.5
              or raw.get(lab, {}).get("model", 0.0) >= 1.0}
    pb = _persona_bias(persona, pcfg, emo_cfg, aliases, strong)
    for lab in set(pb["labels"]):
        add(lab, "persona", 0.8)

    total_w = sum(float(W.get(k, 0.0)) for k in ("keyword", "model", "scene", "context", "persona")) or 1.0
    scores = {}
    for lab, srcs in raw.items():
        s = sum(float(W.get(src, 0.0)) * val for src, val in srcs.items()) / total_w
        s *= (1.0 - float(pb["penalty"].get(lab, 0.0)))
        scores[lab] = max(0.0, s)
    return scores, pb


def select_labels(scores, dec):
    """阈值 + top-k + 副标签比例，产出多情绪标签（带得分）。"""
    th = float(dec.get("threshold", 0.3))
    mx = max(1, int(dec.get("max_labels", 3)))
    ratio = float(dec.get("secondary_ratio", 0.4))
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    picked = [(l, s) for l, s in ordered if s >= th][:mx]
    if picked:
        floor = max(th, picked[0][1] * ratio)
        picked = [x for x in picked if x[1] >= floor]
    return picked


def build_style(labels, scene, params, pb, dec, intensity):
    """把多标签 + 场景 + 人设拼成一句给 TTS 的中文 instruction。"""
    emo_cfg = params.get("emotions") or {}
    scene_cfg = (params.get("scenes") or {}).get(scene) or {}
    parts = []
    if pb.get("tone"):
        parts.append("整体" + pb["tone"])
    if labels:
        main = labels[0][0]
        parts.append("这句" + str(emo_cfg.get(main, {}).get("style", "自然一点")))
        subs = [l for l, _ in labels[1:]]
        if subs:
            parts.append("略带" + "、".join(subs[:2]))
        mc = emo_cfg.get(main, {})
        if mc.get("pitch"):
            parts.append(str(mc["pitch"]))
        if mc.get("vol"):
            parts.append(str(mc["vol"]))
    else:
        parts.append("这句自然、松弛地说")
    if scene_cfg.get("hint"):
        parts.append(str(scene_cfg["hint"]))
    for h in pb.get("hints", [])[:1]:
        parts.append(h)
    if intensity < 0.6:
        parts.append("情绪收着一点")
    elif intensity > 0.85:
        parts.append("情绪可以明显一点")
    body = "，".join(x for x in parts if x)

    cons = "；".join(pb.get("constraints") or [])
    tail = ("。" + cons) if cons else ""
    maxc = int(dec.get("style_max_chars", 110))
    budget = maxc - len(tail)
    if len(body) > budget and budget > 20:
        cut = body[:budget]
        pos = max(cut.rfind("，"), cut.rfind("、"))
        body = cut[:pos] if pos > 10 else cut
    return body + tail


def merge_params(labels, scene, params, pb, dec):
    """多标签按得分加权混合成语音参数。"""
    emo_cfg = params.get("emotions") or {}
    scene_cfg = (params.get("scenes") or {}).get(scene) or {}
    if labels:
        total = sum(s for _, s in labels) or 1.0
        ratio = sum(float(emo_cfg.get(l, {}).get("spd", 1.0)) * s for l, s in labels) / total
    else:
        ratio = 1.0
    ratio *= (1.0 + float(scene_cfg.get("spd_bias", 0.0)) + pb["spd_bias"])
    ratio = max(float(dec.get("speed_min", 0.85)), min(float(dec.get("speed_max", 1.35)), ratio))

    it = float(emo_cfg.get(labels[0][0], {}).get("it", 0.6)) if labels else 0.5
    it *= float(scene_cfg.get("it_scale", 1.0)) * pb["it_scale"]
    it = max(0.0, min(1.0, it))

    effect = ""
    if dec.get("apply_effect"):
        for lab, _ in labels[:1]:
            fx = (emo_cfg.get(lab) or {}).get("fx")
            if fx and fx != "none":
                effect = str(fx)
                break
    return {
        "speed_ratio": round(ratio, 3),
        "intensity": round(it, 3),
        "effect": effect,
        "style": build_style(labels, scene, params, pb, dec, it),
    }


def resolve(text, mtype=None, ctx=None, base_style=None):
    """主入口。返回 VoiceParams；功能关闭 / 参数文件缺失时返回 None（主程序自动走原逻辑）。

    base_style: 上游显式指定的情绪描述（故事点名情绪、/情绪 指令）。默认保留不覆盖，
                只在缺失时补齐拟人化，避免破坏原有确定行为。
    """
    params = load_params()
    if not params.get("enabled"):
        return None
    persona = load_persona()
    dec = params.get("decision") or {}
    scene, signals = recognize_scene(text, mtype, ctx)
    scores, pb = score_emotions(text, scene, ctx, params, persona)
    labels = select_labels(scores, dec)
    merged = merge_params(labels, scene, params, pb, dec)

    vp = VoiceParams(
        scene=scene,
        labels=[(l, round(s, 3)) for l, s in labels],
        intensity=merged["intensity"],
        speed_ratio=merged["speed_ratio"],
        effect=merged["effect"],
        signals={**signals, "scores": {k: round(v, 3) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
                 "persona_rules": pb["active"]},
    )
    tag = "、".join(f"{l}({s:.2f})" for l, s in vp.labels) or "无"
    if base_style and params.get("preserve_explicit_style", True):
        vp.style = ""
        vp.speed_ratio = 1.0
        vp.debug = f"scene={scene} 情绪=[{tag}] 人设规则={pb['active']} → 保留显式 style，仅做判别"
    else:
        vp.style = merged["style"]
        vp.debug = f"scene={scene} 情绪=[{tag}] 语速×{vp.speed_ratio} 强度={vp.intensity} 人设规则={pb['active']} → {vp.style}"
    if dec.get("debug") is False:
        vp.debug = ""
    return vp


# ---- 供评估脚本 / 外部调用的小接口 ----
def _norm_labels(labels):
    """兼容 [(标签,得分)] 与 [标签] 两种输入。"""
    out = []
    for x in labels or []:
        if isinstance(x, (tuple, list)) and len(x) >= 2:
            try:
                out.append((str(x[0]), float(x[1])))
            except (TypeError, ValueError):
                out.append((str(x[0]), 1.0))
        else:
            out.append((str(x), 1.0))
    return out


def labels_to_style(labels, scene="chat"):
    """把多情绪标签直接转成风格指令。"""
    p = load_params()
    dec = p.get("decision") or {}
    norm = _norm_labels(labels)
    pb = _persona_bias(load_persona(), p.get("persona") or {},
                       p.get("emotions") or {}, p.get("aliases") or {}, [l for l, _ in norm])
    return build_style(norm, scene, p, pb, dec, 0.7)


def labels_to_params(labels, scene="chat"):
    """把多情绪标签直接转成语音参数字典。"""
    p = load_params()
    dec = p.get("decision") or {}
    norm = _norm_labels(labels)
    pb = _persona_bias(load_persona(), p.get("persona") or {},
                       p.get("emotions") or {}, p.get("aliases") or {}, [l for l, _ in norm])
    return merge_params(norm, scene, p, pb, dec)
