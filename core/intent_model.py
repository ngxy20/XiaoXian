# -*- coding: utf-8 -*-
"""意图识别（NLP）：基于预训练 bert-base-chinese 微调，产出权重文件 models/intent_bert.pt。

类别：story(讲故事) / image(要图) / anime(二次元图) / emoticon(表情包) / other。
推理返回类别或 None（权重缺失时回退正则规则）。
"""
import os

LABELS = ["story", "image", "anime", "emoticon", "other"]
WEIGHT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "intent_bert.pt")

_model = None
_loaded = False


def load_model():
    global _model, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        import torch
        from transformers import BertTokenizerFast, BertForSequenceClassification
    except Exception as e:
        print(f"[NLP-意图] 未安装 torch/transformers，回退正则规则（{e}）")
        return None
    if not os.path.exists(WEIGHT):
        print(f"[NLP-意图] 权重文件不存在：{WEIGHT}，回退正则规则")
        return None
    try:
        ckpt = torch.load(WEIGHT, map_location="cpu")
        name = ckpt.get("model_name", "bert-base-chinese")
        tok = BertTokenizerFast.from_pretrained(name)
        model = BertForSequenceClassification.from_pretrained(name, num_labels=len(LABELS))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        _model = (model, tok)
        print("[NLP-意图] 权重加载成功，已启用模型识别意图")
    except Exception as e:
        print(f"[NLP-意图] 权重加载失败：{e}")
        _model = None
    return _model


def predict_intent(text):
    m = load_model()
    if not m or not text:
        return None
    model, tok = m
    try:
        import torch
        enc = tok(text, return_tensors="pt", truncation=True, max_length=64)
        with torch.no_grad():
            logits = model(**enc).logits
        return LABELS[int(logits.argmax(dim=-1))]
    except Exception as e:
        print(f"[NLP-意图] 推理失败：{e}")
        return None
