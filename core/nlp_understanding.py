# -*- coding: utf-8 -*-
"""统一多任务 NLP 理解模块（结合性创新核心）：

共享一个 bert-base-chinese 编码器，一次前向同时完成
  - 意图识别（单标签）：story / image / anime / emoticon / other
  - 多标签情感分析（复杂多元化情感，可同时命中多种，而非单一两字标签）
  - 命名实体识别（序列标注 BIO，聚焦 TOPIC 用于图片爬虫）

推理返回 {intent, emotions[], entities[]}，调用方据此编排「回什么模态 + 用什么情绪念」。
权重文件 models/nlp_bert.pt 缺省时 predict 返回 None，由上层回退既有模块/规则。
torch / transformers 为可选依赖，缺失时本模块不报错。
"""
import os

# ---- 意图（单标签）----
INTENTS = ["story", "image", "anime", "emoticon", "other"]

# ---- 情感（多标签，复杂多元化：可同时多种，不局限于单一两字标签）----
EMOTIONS = [
    "开心", "兴奋", "欢乐", "温柔", "感动", "温馨", "治愈",
    "悲伤", "孤独", "思念", "怀旧", "遗憾", "无奈",
    "愤怒", "愤慨", "仇恨",
    "俏皮", "调皮", "可爱", "撒娇",
    "严肃", "冷静", "正式",
    "神秘", "诡异", "恐怖", "恐惧",
    "平静", "好奇", "惊讶", "嘲讽",
]
# 细粒度情感 -> 8 个语音预设（与 qq_bot_relay._EMOTION_PRESETS 的键对应）
EMOTION_TO_PRESET = {
    "开心": "开心", "兴奋": "开心", "欢乐": "开心", "惊讶": "开心",
    "温柔": "温柔", "感动": "温柔", "温馨": "温柔", "治愈": "温柔",
    "平静": "温柔", "好奇": "温柔",
    "悲伤": "悲伤", "孤独": "悲伤", "思念": "悲伤", "怀旧": "悲伤",
    "遗憾": "悲伤", "无奈": "悲伤",
    "愤怒": "愤怒", "愤慨": "愤怒", "仇恨": "愤怒",
    "俏皮": "俏皮", "调皮": "俏皮", "可爱": "俏皮", "嘲讽": "俏皮",
    "撒娇": "撒娇",
    "严肃": "严肃", "冷静": "严肃", "正式": "严肃",
    "神秘": "神秘", "诡异": "神秘", "恐怖": "神秘", "恐惧": "神秘",
}
# 8 个语音预设对应的简短特质（拼接成风格描述）
_PRESET_TRAIT = {
    "开心": "开心轻快", "温柔": "温柔甜美", "悲伤": "悲伤低沉", "愤怒": "愤怒有力",
    "俏皮": "俏皮可爱", "撒娇": "撒娇软萌", "严肃": "严肃正式", "神秘": "神秘低沉",
}

# ---- NER（序列标注，BIO；聚焦 TOPIC 实体用于图片爬虫）----
NER_TAGS = ["O", "B-TOPIC", "I-TOPIC"]

WEIGHT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "nlp_bert.pt")
_THRESHOLD = 0.5

_model = None
_loaded = False


def load_model():
    global _model, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        import torch
        from transformers import BertTokenizerFast
        from nlp_model import MultiTaskBert
    except Exception as e:
        print(f"[NLP-统一] 未安装 torch/transformers，回退既有模块（{e}）")
        return None
    if not os.path.exists(WEIGHT):
        print(f"[NLP-统一] 权重文件不存在：{WEIGHT}，回退既有模块")
        return None
    try:
        ckpt = torch.load(WEIGHT, map_location="cpu")
        name = ckpt.get("model_name", "bert-base-chinese")
        tok = BertTokenizerFast.from_pretrained(name)
        model = MultiTaskBert(name, len(INTENTS), len(EMOTIONS), len(NER_TAGS))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        _model = (model, tok)
        print("[NLP-统一] 权重加载成功，多任务理解已启用（意图+情感+NER）")
    except Exception as e:
        print(f"[NLP-统一] 权重加载失败：{e}")
        _model = None
    return _model


def _decode_entities(text, offsets, ner_ids):
    """把 BIO 标签还原成实体字符串列表（跳过特殊符/PAD）。"""
    entities, cur = [], []
    for (s, e), t in zip(offsets, ner_ids):
        if s == e:  # 特殊符 / 填充
            if cur:
                entities.append("".join(cur)); cur = []
            continue
        tag = NER_TAGS[t]
        if tag == "B-TOPIC":
            if cur:
                entities.append("".join(cur))
            cur = [text[s:e]]
        elif tag == "I-TOPIC":
            if cur is not None:
                cur.append(text[s:e])
        else:  # O
            if cur:
                entities.append("".join(cur)); cur = []
    if cur:
        entities.append("".join(cur))
    return entities


def predict(text):
    """返回 {intent, emotions[], entities[]}；无模型/无文本返回 None。"""
    m = load_model()
    if not m or not text:
        return None
    import torch
    model, tok = m
    try:
        enc = tok(text, return_offsets_mapping=True, return_tensors="pt",
                  truncation=True, max_length=128)
        offsets = enc["offset_mapping"][0].tolist()
        with torch.no_grad():
            out = model(**{k: v for k, v in enc.items() if k != "offset_mapping"})
        intent = INTENTS[int(out["intent"].argmax(-1))]
        emo_probs = torch.sigmoid(out["emotion"][0])
        emotions = [EMOTIONS[i] for i, p in enumerate(emo_probs) if p.item() >= _THRESHOLD]
        ner_ids = out["ner"][0].argmax(-1).tolist()
        entities = _decode_entities(text, offsets, ner_ids)
        return {"intent": intent, "emotions": emotions, "entities": entities}
    except Exception as e:
        print(f"[NLP-统一] 推理失败：{e}")
        return None


def to_tts_style(result, desc=None):
    """把多任务结果映射成 TTS 风格描述；若有自由文本 desc 则直接用它（最丰富）。"""
    if desc:
        return "用" + desc + "的语气朗读"
    emos = (result or {}).get("emotions") or []
    presets = []
    for e in emos:
        p = EMOTION_TO_PRESET.get(e)
        if p and p not in presets:
            presets.append(p)
    if not presets:
        return None
    return "用" + "、".join(_PRESET_TRAIT[p] for p in presets) + "的语气朗读"
