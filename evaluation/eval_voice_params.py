# -*- coding: utf-8 -*-
"""语音拟人化 A/B 评估报告生成器。

对比两种语音策略在真实对话样本（data/nlp.jsonl，含 gold intent/emotions）上的表现：
- 策略 A（Baseline，现状）：固定中性 chat_style、无情绪标签、语速恒定、不读人设
- 策略 B（实验，拟人化）：场景情绪识别 + 多情绪标签 + 参数化判别 + 人设偏置

输出 Markdown 报告 evaluation/voice_params_report.md。

用法：
  python evaluation/eval_voice_params.py
"""
import os
import sys
import json
import math

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "core"))
import voice_persona as vp  # noqa: E402

DATA = os.path.join(_ROOT, "data", "nlp.jsonl")
OUT = os.path.join(_ROOT, "evaluation", "voice_params_report.md")

# 现状：voice_config.json 里的固定 chat_style（缺失时用项目默认值）
BASE_STYLE = "用温柔、可爱、像动漫人物，语速中快，亲切自然"
try:
    with open(os.path.join(_ROOT, "assets", "voice_config.json"), encoding="utf-8") as f:
        BASE_STYLE = (json.load(f).get("chat_style") or BASE_STYLE)
except Exception:
    pass

# 覆盖 data/nlp.jsonl 里较少见的场景（安慰/思念/道安/疑问）
BUILTIN = [
    {"text": "今天好开心呀，真的太棒了！", "intent": "other", "emotions": ["开心", "兴奋"], "mtype": "chat"},
    {"text": "呜呜呜我今天好难过，想哭", "intent": "other", "emotions": ["悲伤", "孤独"], "mtype": "chat"},
    {"text": "讲个关于小狐狸的童话吧", "intent": "story", "emotions": ["温柔", "好奇"], "mtype": "chat"},
    {"text": "黑夜里传来诡异的笑声，她害怕得发抖", "intent": "story", "emotions": ["神秘", "恐惧"], "mtype": "story"},
    {"text": "嗯，随便吧，都行", "intent": "other", "emotions": ["平静"], "mtype": "chat"},
    {"text": "想你了，什么时候回来呀", "intent": "other", "emotions": ["思念", "温柔"], "mtype": "chat"},
    {"text": "你能不能认真一点", "intent": "other", "emotions": ["严肃"], "mtype": "chat"},
    {"text": "早上好呀，该起床啦", "intent": "other", "emotions": ["开心"], "mtype": "greeting"},
]


def load_samples():
    out = [dict(x) for x in BUILTIN]
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                text = d.get("text") or ""
                if not text:
                    continue
                intent = d.get("intent") or "other"
                out.append({"text": text, "intent": intent,
                            "emotions": d.get("emotions") or [],
                            "mtype": "story" if intent == "story" else "chat"})
    return out


def norm_gold(labels):
    """把 30 类细粒度 gold 归一到参数模型的 19 类。"""
    p = vp.load_params()
    emo = p.get("emotions") or {}
    alias = p.get("aliases") or {}
    out = set()
    for x in labels or []:
        if x in emo:
            out.add(x)
        elif x in alias:
            out.add(alias[x])
    return out


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def baseline(text, sample):
    return {"labels": [], "style": BASE_STYLE, "speed": 1.0, "scene": "chat",
            "intensity": 0.0, "rules": []}


def experiment(text, sample):
    r = vp.resolve(text, mtype=sample["mtype"])
    if not r:   # 功能被关闭时退化为基线，便于报告如实反映
        return baseline(text, sample)
    return {"labels": [l for l, _ in r.labels], "style": r.style,
            "speed": r.speed_ratio, "scene": r.scene,
            "intensity": r.intensity, "rules": r.signals.get("persona_rules") or []}


def evaluate(samples, fn):
    n = len(samples) or 1
    emo_sum = coverage = persona_hit = story_total = story_hit = rule_hit = 0
    speeds, styles = [], set()
    for s in samples:
        o = fn(s["text"], s)
        gold = norm_gold(s.get("emotions"))
        pred = set(o["labels"])
        emo_sum += jaccard(pred, gold)
        coverage += 1 if pred else 0
        persona_hit += 1 if ("轻松" in o["style"] or "亲昵" in o["style"]) else 0
        rule_hit += 1 if o["rules"] else 0
        if s.get("intent") == "story":
            story_total += 1
            story_hit += 1 if o["scene"] == "story" else 0
        speeds.append(float(o["speed"]))
        styles.add(o["style"])
    mean = sum(speeds) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in speeds) / n)
    return {
        "emo_hit": emo_sum / n,
        "coverage": coverage / n,
        "story_recall": story_hit / story_total if story_total else 0.0,
        "story_total": story_total,
        "persona_hit": persona_hit / n,
        "rule_hit": rule_hit / n,
        "speed_std": std,
        "style_unique": len(styles),
        "n": len(samples),
    }


def main():
    samples = load_samples()
    if not samples:
        print("无样本，退出")
        return
    a = evaluate(samples, baseline)
    b = evaluate(samples, experiment)

    def pct(x):
        return f"{x*100:.1f}%"

    # 逐场景示例（只列内置样例，避免报告里出现本地语料原文）
    demo_rows = []
    for s in BUILTIN:
        o = experiment(s["text"], s)
        demo_rows.append((s["text"], o["scene"], "、".join(o["labels"]) or "—",
                          f"{o['speed']:.2f}", "、".join(o["rules"]) or "—"))

    lines = []
    lines.append("# 语音拟人化 A/B 测试报告\n")
    lines.append(f"- 样本：`data/nlp.jsonl` + 8 条场景补充样例，共 **{b['n']}** 条（含 gold intent/emotions）")
    lines.append("- 评估对象：`core/voice_persona.py` + `assets/voice_emotion_params.json`\n")
    lines.append("## 策略定义\n")
    lines.append(f"- **策略 A（Baseline，现状）**：所有句子用同一句固定风格描述（`{BASE_STYLE}`），无情绪标签、语速恒定、不读人设。")
    lines.append("- **策略 B（实验，拟人化）**：场景识别 → 关键词/模型/场景先验/上下文/人设五路加权打分 → 多情绪标签 → 混合成风格指令与语速倍率，并注入人设语气、共情规则与输出约束。\n")
    lines.append("## 指标对比\n")
    lines.append("| 指标 | 策略 A | 策略 B | 变化 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 情绪命中率（Jaccard，与 gold） | {pct(a['emo_hit'])} | {pct(b['emo_hit'])} | {(b['emo_hit']-a['emo_hit'])*100:+.1f}pp |")
    lines.append(f"| 多情绪标签覆盖率 | {pct(a['coverage'])} | {pct(b['coverage'])} | {(b['coverage']-a['coverage'])*100:+.1f}pp |")
    lines.append(f"| 故事场景召回率（gold=story，共 {b['story_total']} 条） | {pct(a['story_recall'])} | {pct(b['story_recall'])} | {(b['story_recall']-a['story_recall'])*100:+.1f}pp |")
    lines.append(f"| 人设语气注入率 | {pct(a['persona_hit'])} | {pct(b['persona_hit'])} | {(b['persona_hit']-a['persona_hit'])*100:+.1f}pp |")
    lines.append(f"| 人设共情规则命中率 | {pct(a['rule_hit'])} | {pct(b['rule_hit'])} | {(b['rule_hit']-a['rule_hit'])*100:+.1f}pp |")
    lines.append(f"| 语速区分度（标准差） | {a['speed_std']:.3f} | {b['speed_std']:.3f} | {b['speed_std']-a['speed_std']:+.3f} |")
    lines.append(f"| 不同风格指令数 / 样本数 | {a['style_unique']} / {a['n']} | {b['style_unique']} / {b['n']} | {b['style_unique']-a['style_unique']:+d} |")
    lines.append("\n## 逐场景判别示例\n")
    lines.append("| 文本 | 场景 | 情绪标签 | 语速倍率 | 命中人设共情规则 |")
    lines.append("|---|---|---|---|---|")
    for t, scene, labs, spd, rules in demo_rows:
        lines.append(f"| {t} | {scene} | {labs} | {spd} | {rules} |")
    lines.append("\n## 结论\n")
    lines.append(f"- 策略 B 情绪命中率 {pct(b['emo_hit'])}（A 为 {pct(a['emo_hit'])}），多标签覆盖率 {pct(b['coverage'])}，说明参数模型能从文本判别情绪而非一律中性；")
    lines.append(f"- 故事场景召回率 {pct(b['story_recall'])}，意图模型未训练时由正则兜底，场景先验得以下发；")
    lines.append(f"- 人设语气注入率 {pct(b['persona_hit'])}、共情规则命中率 {pct(b['rule_hit'])}，证明 `persona.json` 的「说话风格.语气 / 情感.共情规则 / 禁止」真实参与了语音输出；")
    lines.append(f"- 语速标准差由 {a['speed_std']:.3f} 提升到 {b['speed_std']:.3f}、风格指令数由 {a['style_unique']} 增至 {b['style_unique']}，语音不再千篇一律；")
    lines.append("- 当前 NLP 权重未训练，判别主要靠关键词 + 场景/上下文/人设先验；训练 `emotion_bert.pt` / `nlp_bert.pt` 后，模型那一路会自动提高权重分，指标有望进一步提升。")
    lines.append("\n## 复跑\n")
    lines.append("```bash")
    lines.append("python evaluation/eval_voice_params.py")
    lines.append("```")
    lines.append("改 `assets/voice_emotion_params.json` 的 `decision.weights` / `threshold` / 各情绪 `spd` 后直接重跑即可对比调参效果（配置 mtime 热更新，无需重启）。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"报告已生成：{OUT}")
    print(f"A: emo={a['emo_hit']:.3f} coverage={a['coverage']:.3f} speed_std={a['speed_std']:.3f}")
    print(f"B: emo={b['emo_hit']:.3f} coverage={b['coverage']:.3f} speed_std={b['speed_std']:.3f} "
          f"story_recall={b['story_recall']:.3f} persona={b['persona_hit']:.3f}")


if __name__ == "__main__":
    main()
