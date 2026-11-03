# -*- coding: utf-8 -*-
"""意图识别训练：python train_intent.py
读取 data/intent.jsonl（每行 {"text":..., "label":...}），微调 bert-base-chinese，
保存 models/intent_bert.pt。类别见 intent_model.LABELS。需 pip install torch transformers。
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

import intent_model as im


class DS(Dataset):
    def __init__(self, path, tok):
        self.rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self.rows.append((d["text"], im.LABELS.index(d["label"])))
        self.tok = tok

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        t, l = self.rows[i]
        e = self.tok(t, truncation=True, padding="max_length", max_length=64, return_tensors="pt")
        return {k: v.squeeze(0) for k, v in e.items()}, torch.tensor(l)


def main():
    os.makedirs("models", exist_ok=True)
    tok = BertTokenizerFast.from_pretrained("bert-base-chinese")
    ds = DS("data/intent.jsonl", tok)
    dl = DataLoader(ds, batch_size=8, shuffle=True)
    model = BertForSequenceClassification.from_pretrained("bert-base-chinese", num_labels=len(im.LABELS))
    opt = AdamW(model.parameters(), lr=2e-5)
    model.train()
    for epoch in range(3):
        for batch, y in dl:
            opt.zero_grad()
            out = model(**{k: v for k, v in batch.items()}, labels=y)
            out.loss.backward()
            opt.step()
        print(f"epoch {epoch + 1} done")
    torch.save({"model_name": "bert-base-chinese", "state_dict": model.state_dict()}, im.WEIGHT)
    print("已保存权重：", im.WEIGHT)


if __name__ == "__main__":
    main()
