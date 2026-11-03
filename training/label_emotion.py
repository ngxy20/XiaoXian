# -*- coding: utf-8 -*-
"""群消息 -> 情绪数据集 的「人工标注」工作流（不依赖 LLM 自动打标）。

用法：
  python label_emotion.py export   # 从 data/chatlog_raw.jsonl 抽取群消息 -> data/emotions_tolabel.jsonl（labels 为空，等你手写标注）
  python label_emotion.py apply    # 读取你标注好的 emotions_tolabel.jsonl，把有 labels 的并入 data/emotions.jsonl（按 text 去重），供 train_emotion.py 使用

标注方式：用任意编辑器打开 data/emotions_tolabel.jsonl，给每行的 "labels" 填数组即可，例如
  {"text": "今天好开心", "labels": ["开心"]}
  {"text": "呜呜呜想哭", "labels": ["悲伤", "温柔"]}
多标签用多个元素（一段话可同时含温柔+悲伤）。留空 [] 表示暂不标注，apply 时会跳过。

合法标签（与 core/emotion_model.LABELS 一致）：开心 / 温柔 / 悲伤 / 愤怒 / 俏皮 / 撒娇 / 严肃 / 神秘
"""
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(_ROOT, "data", "chatlog_raw.jsonl")
TO = os.path.join(_ROOT, "data", "emotions_tolabel.jsonl")
OUT = os.path.join(_ROOT, "data", "emotions.jsonl")
VALID = ["开心", "温柔", "悲伤", "愤怒", "俏皮", "撒娇", "严肃", "神秘"]

_TAG = re.compile(r"<[^>]*>|\[[^\]]*\]")  # 吃掉 <faceType=...> / [QQ:at=...] 等占位


def _clean(t):
    t = _TAG.sub("", t or "")
    return t.strip()


def export():
    seen, n = set(), 0
    with open(RAW, encoding="utf-8") as f, open(TO, "w", encoding="utf-8") as out:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("kind") != "group":
                continue
            text = _clean(d.get("text", ""))
            if len(text) < 4:           # 太短（纯表情/单字）不好标，跳过
                continue
            if text.startswith("/"):     # 指令不算情绪语料
                continue
            if text in seen:
                continue
            seen.add(text)
            out.write(json.dumps({"text": text, "labels": []}, ensure_ascii=False) + "\n")
            n += 1
    print(f"已导出 {n} 条群消息到 {TO}（labels 为空，请手动标注后执行 apply）")


def apply():
    existing = {}   # text -> labels
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            existing[d["text"]] = d.get("labels") or []
    added = 0
    with open(TO, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            labels = [x for x in (d.get("labels") or []) if x in VALID]
            if not labels:          # 没标注的跳过
                continue
            t = d["text"]
            if t in existing and existing[t]:
                continue
            existing[t] = labels
            added += 1
    with open(OUT, "w", encoding="utf-8") as out:
        for t, labs in existing.items():
            out.write(json.dumps({"text": t, "labels": labs}, ensure_ascii=False) + "\n")
    print(f"合并完成：data/emotions.jsonl 现共 {len(existing)} 条（本次新增 {added} 条）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "export"
    if cmd == "export":
        export()
    elif cmd == "apply":
        apply()
    else:
        print("用法: python label_emotion.py [export|apply]")
