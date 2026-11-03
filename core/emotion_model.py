# -*- coding: utf-8 -*-
"""文本情感分类（NLP，多标签，标签集可扩展）：基于 bert-base-chinese 微调，产出权重文件 core/models/emotion_bert.pt。

- 支持「一段话多种情感」：多标签（BCE）损失，推理对每类算概率，超阈值即命中。
- 标签集不再写死 8 类：以 assets/emotion_labels.json 为准（标注工具可增删）。训练时把
  「数据集里出现过的标签 ∪ 该文件里的标签 ∪ 默认 8 类」一并作为类别，并把最终标签顺序写进权重文件；
  推理时严格按权重里存的顺序还原，保证维度与训练一致。

torch / transformers 为可选依赖，缺失时回退关键词规则。
"""
import os
import json

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BASE_DIR)
_LABELS_FILE = os.path.join(_ROOT, "assets", "emotion_labels.json")

# 默认 8 类（文件缺失时的兜底；也是标注工具首次启动的初始集）
DEFAULT_LABELS = ["开心", "温柔", "悲伤", "愤怒", "俏皮", "撒娇", "严肃", "神秘"]
# 情绪 -> 朗读特质描述（多标签命中时拼接成一句风格；自定义标签无映射时回退用标签名本身）
_TRAIT = {
    "开心": "欢快活泼", "温柔": "温柔甜美", "悲伤": "悲伤低沉", "愤怒": "愤怒有力",
    "俏皮": "俏皮可爱", "撒娇": "撒娇软萌", "严肃": "严肃正式", "神秘": "神秘低沉",
}
_THRESHOLD = 0.5
WEIGHT = os.path.join(_BASE_DIR, "models", "emotion_bert.pt")

# 运行期标签集：模块导入时从文件载入；load_model 成功后会用权重里存的顺序覆盖，确保与训练一致
def _load_labels_impl():
    try:
        if os.path.exists(_LABELS_FILE):
            with open(_LABELS_FILE, encoding="utf-8") as f:
                lst = json.load(f)
            if isinstance(lst, list) and all(isinstance(x, str) for x in lst) and lst:
                return list(lst)
    except Exception:
        pass
    return list(DEFAULT_LABELS)


def load_labels():
    """外部（训练 / 标注工具）读取当前标签集。"""
    return _load_labels_impl()


def save_labels(labels):
    """把标签集写回 assets/emotion_labels.json（标注工具增删后、训练合并后调用）。"""
    os.makedirs(os.path.dirname(_LABELS_FILE), exist_ok=True)
    with open(_LABELS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(labels), f, ensure_ascii=False, indent=2)


LABELS = _load_labels_impl()


_model = None
_loaded = False


def load_model():
    global _model, _loaded, LABELS
    if _loaded:
        return _model
    _loaded = True
    try:
        import torch
        from transformers import BertTokenizerFast, BertForSequenceClassification
    except Exception as e:
        print(f"[NLP-情感] 未安装 torch/transformers，回退关键词规则（{e}）")
        return None
    if not os.path.exists(WEIGHT):
        print(f"[NLP-情感] 权重文件不存在：{WEIGHT}，回退关键词规则")
        return None
    try:
        ckpt = torch.load(WEIGHT, map_location="cpu")
        name = ckpt.get("model_name", "bert-base-chinese")
        ckpt_labels = ckpt.get("labels") or _load_labels_impl()
        if isinstance(ckpt_labels, list) and ckpt_labels:
            LABELS = list(ckpt_labels)          # 以训练时的标签顺序为准，保证维度对齐
        tok = BertTokenizerFast.from_pretrained(name)
        model = BertForSequenceClassification.from_pretrained(
            name, num_labels=len(LABELS), problem_type="multi_label_classification")
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        _model = (model, tok)
        print(f"[NLP-情感] 权重加载成功（{len(LABELS)} 类），已启用多标签模型选情绪")
    except Exception as e:
        print(f"[NLP-情感] 权重加载失败：{e}")
        _model = None
    return _model


def predict_emotions(text):
    """返回命中的情绪标签列表（多标签）；无模型/无命中返回 []。"""
    m = load_model()
    if not m or not text:
        return []
    model, tok = m
    try:
        import torch
        enc = tok(text, return_tensors="pt", truncation=True, max_length=128)
        with torch.no_grad():
            logits = model(**enc).logits
        probs = torch.sigmoid(logits[0])
        return [LABELS[i] for i, p in enumerate(probs) if p.item() >= _THRESHOLD]
    except Exception as e:
        print(f"[NLP-情感] 推理失败：{e}")
        return []


def predict_emotion_style(text):
    """返回拼接好的朗读风格描述（多标签）；无命中返回 None。"""
    labels = predict_emotions(text)
    traits = [_TRAIT.get(l, l) for l in labels]   # 自定义标签用标签名本身作为语气描述
    if not traits:
        return None
    return "用" + "、".join(traits) + "的语气朗读"
