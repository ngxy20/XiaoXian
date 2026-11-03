# -*- coding: utf-8 -*-
"""统一多任务模型训练：python train_nlp.py
读取 data/nlp.jsonl（每行 {"text","intent","emotions":[...],"entities":[...]}），
共享 bert-base-chinese 编码器，联合训练 意图(单标签) + 多标签情感 + NER(BIO) 三任务，
保存 models/nlp_bert.pt。需 pip install torch transformers。
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
    from transformers import BertTokenizerFast, AdamW
    from nlp_model import MultiTaskBert
except Exception:
    print("请先安装依赖：pip install torch transformers")
    raise

import nlp_understanding as nu


def char_level_bio(text, entities):
    """把实体列表转成字符级 BIO 标签。"""
    n = len(text)
    tags = ["O"] * n
    for ent in (entities or []):
        start = text.find(ent)
        if start == -1:
            continue
        end = start + len(ent)
        tags[start] = "B-TOPIC"
        for i in range(start + 1, end):
            tags[i] = "I-TOPIC"
    return tags


class DS(Dataset):
    def __init__(self, path, tok):
        self.rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                self.rows.append(json.loads(line))
        self.tok = tok

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        d = self.rows[i]
        text = d["text"]
        intent = nu.INTENTS.index(d.get("intent", "other"))
        emo = [1 if e in d.get("emotions", []) else 0 for e in nu.EMOTIONS]
        enc = self.tok(text, return_offsets_mapping=True, truncation=True,
                       padding="max_length", max_length=128, return_tensors="pt")
        offsets = enc["offset_mapping"][0].tolist()
        ctags = char_level_bio(text, d.get("entities", []))
        ner = [(-100 if s == e else nu.NER_TAGS.index(ctags[s]))
               for (s, e) in offsets]
        return ({k: v.squeeze(0) for k, v in enc.items() if k != "offset_mapping"},
                torch.tensor(intent),
                torch.tensor(emo, dtype=torch.float),
                torch.tensor(ner))


def main():
    os.makedirs("models", exist_ok=True)
    tok = BertTokenizerFast.from_pretrained("bert-base-chinese")
    ds = DS("data/nlp.jsonl", tok)
    dl = DataLoader(ds, batch_size=8, shuffle=True)
    model = MultiTaskBert("bert-base-chinese", len(nu.INTENTS),
                          len(nu.EMOTIONS), len(nu.NER_TAGS))
    opt = AdamW(model.parameters(), lr=2e-5)
    model.train()
    for epoch in range(3):
        for batch, intent, emo, ner in dl:
            opt.zero_grad()
            out = model(**{k: v for k, v in batch.items()},
                        intent_labels=intent, emotion_labels=emo, ner_labels=ner)
            out["loss"].backward()
            opt.step()
        print(f"epoch {epoch + 1} done")
    torch.save({"model_name": "bert-base-chinese", "state_dict": model.state_dict()}, nu.WEIGHT)
    print("已保存权重：", nu.WEIGHT)


if __name__ == "__main__":
    main()
