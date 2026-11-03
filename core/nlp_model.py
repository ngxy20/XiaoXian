# -*- coding: utf-8 -*-
"""统一多任务模型定义（仅训练/推理加载时 import，需 torch）。

共享一个 bert-base-chinese 编码器，同时输出三个任务的 logits：
  - 意图识别（单标签分类）
  - 多标签情感分析（复杂多元化情感，可同时命中多种）
  - 命名实体识别（序列标注，BIO；聚焦 TOPIC 用于图片爬虫）
"""
import torch
import torch.nn as nn
from transformers import BertModel


class MultiTaskBert(nn.Module):
    def __init__(self, model_name, num_intents, num_emotions, num_ner_tags):
        super().__init__()
        self.bert = BertModel.from_pretrained(model_name)
        h = self.bert.config.hidden_size
        self.intent_head = nn.Linear(h, num_intents)        # 单标签
        self.emotion_head = nn.Linear(h, num_emotions)       # 多标签（BCE）
        self.ner_head = nn.Linear(h, num_ner_tags)           # 序列标注

    def forward(self, input_ids, attention_mask, token_type_ids=None,
                intent_labels=None, emotion_labels=None, ner_labels=None):
        out = self.bert(input_ids=input_ids, attention_mask=attention_mask,
                        token_type_ids=token_type_ids)
        seq = out.last_hidden_state          # (B, L, H)
        pooled = out.pooler_output          # (B, H)
        intent_logits = self.intent_head(pooled)
        emotion_logits = self.emotion_head(pooled)
        ner_logits = self.ner_head(seq)     # (B, L, ner)
        loss = None
        if intent_labels is not None:
            loss_i = nn.functional.cross_entropy(intent_logits, intent_labels)
            loss_e = nn.functional.binary_cross_entropy_with_logits(emotion_logits, emotion_labels)
            loss_n = nn.functional.cross_entropy(
                ner_logits.view(-1, ner_logits.size(-1)),
                ner_labels.view(-1), ignore_index=-100)
            loss = loss_i + loss_e + loss_n
        return {"intent": intent_logits, "emotion": emotion_logits,
                "ner": ner_logits, "loss": loss}
