# -*- coding: utf-8 -*-
"""情感分类训练（多标签，标签集可扩展）：python training/train_emotion.py

读取 data/emotions.jsonl（每行 {"text":..., "labels":[...]} 或 {"text":...,"label":"..."}），
微调 bert-base-chinese（problem_type=multi_label_classification，BCE 损失），
保存 core/models/emotion_bert.pt（含 state_dict 与标签顺序 labels）。

标签集 = assets/emotion_labels.json 现有标签（含标注工具加的自定义）∪ 默认 8 类 ∪ 数据集里出现的新标签；
最终标签顺序会写回 assets/emotion_labels.json 并存入权重，保证推理/标注一致。
需先：pip install torch transformers。
"""
import json
import os
import sys as _sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

try:
    import torch
    from torch.utils.data import Dataset, DataLoader
    from transformers import BertTokenizerFast, BertForSequenceClassification, AdamW
except Exception:
    print("请先安装依赖：pip install torch transformers")
    raise

import emotion_model as em


def _collect_labels(path):
    rows = []
    data_labels = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            labels = d.get("labels") or ([d["label"]] if "label" in d else [])
            labels = [x for x in labels if isinstance(x, str)]
            data_labels.update(labels)
            rows.append((d["text"], labels))
    return rows, data_labels


class DS(Dataset):
    def __init__(self, rows, tok, labels):
        self.labels = labels
        self.label_set = set(labels)
        self.rows = rows
        self.tok = tok

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        t, labels = self.rows[i]
        vec = [1 if L in self.label_set else 0 for L in self.labels]
        e = self.tok(t, truncation=True, padding="max_length", max_length=128, return_tensors="pt")
        return {k: v.squeeze(0) for k, v in e.items()}, torch.tensor(vec, dtype=torch.float)


def main():
    path = os.path.join(_ROOT, "data", "emotions.jsonl")
    rows, data_labels = _collect_labels(path)

    # 标签集：文件现有（含标注工具加的）∪ 默认 8 类 ∪ 数据集里出现的新标签
    all_labels = list(em.load_labels())
    for l in em.DEFAULT_LABELS:
        if l not in all_labels:
            all_labels.append(l)
    for l in sorted(data_labels):
        if l not in all_labels:
            all_labels.append(l)
    em.save_labels(all_labels)        # 写回 JSON，保证标注工具/推理与训练一致
    print(f"标签集（{len(all_labels)} 类）：{all_labels}")

    os.makedirs(os.path.dirname(em.WEIGHT), exist_ok=True)
    tok = BertTokenizerFast.from_pretrained("bert-base-chinese")
    ds = DS(rows, tok, all_labels)
    dl = DataLoader(ds, batch_size=8, shuffle=True)
    model = BertForSequenceClassification.from_pretrained(
        "bert-base-chinese", num_labels=len(all_labels), problem_type="multi_label_classification")
    opt = AdamW(model.parameters(), lr=2e-5)
    model.train()
    for epoch in range(3):
        for batch, y in dl:
            opt.zero_grad()
            out = model(**{k: v for k, v in batch.items()}, labels=y)
            out.loss.backward()
            opt.step()
        print(f"epoch {epoch + 1} done")
    torch.save({
        "model_name": "bert-base-chinese",
        "state_dict": model.state_dict(),
        "labels": all_labels,
    }, em.WEIGHT)
    print("已保存权重：", em.WEIGHT)


if __name__ == "__main__":
    main()
