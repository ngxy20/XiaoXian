# -*- coding: utf-8 -*-
"""数据集构建：把聊天/故事语料用大模型自动打标，生成训练用的 .jsonl。

用法：
  1) 实时记录：机器人运行时会自动把群友/私聊消息写入 data/chatlog_raw.jsonl（见 qq_bot_relay._log_raw_message）
     也可手动在 data/chatlog.txt 放语料（每行一条文本，可带「用户:」之类前缀，会自动去掉角色前缀）
  2) 确保 config.json 中 ENABLE_PLATFORM_API=True 且有思知词元额度
  3) 运行：python build_dataset.py
     统一多任务格式追加写入 data/nlp.jsonl（{"text","intent","emotions":[...],"entities":[...]}），
     其中 emotions 为复杂多元化情感（多标签），entities 为题眼实体（用于图片爬虫）。
     同时兼容写入 data/emotions.jsonl（多标签）与 data/intent.jsonl（单标签），供独立训练脚本使用。

打标失败的行自动跳过；本脚本只做「数据生产」，不训练。
"""
import json
import os
import re
import sys as _sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import qq_bot_relay as m

EMO_SET = m.nlp_understanding.EMOTIONS if m.nlp_understanding else \
    ["开心", "温柔", "悲伤", "愤怒", "俏皮", "撒娇", "严肃", "神秘"]
INTENT_SET = m.intent_model.LABELS if m.intent_model else \
    ["story", "image", "anime", "emoticon", "other"]

_ROLE_RE = re.compile(r"^\s*(用户|机器人|助理|system|user|bot|assistant)\s*[:：]\s*")


def _strip_role(line):
    return _ROLE_RE.sub("", line).strip()


def _collect_texts():
    """汇总待打标文本：实时原始日志 data/chatlog_raw.jsonl + 手动语料 data/chatlog.txt，去重。"""
    out, seen = [], set()
    raw = os.path.join(m.BASE_DIR, "data", "chatlog_raw.jsonl")
    if os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = (d.get("text") or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    src = os.path.join(m.BASE_DIR, "data", "chatlog.txt")
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            for line in f:
                line = _strip_role(line.strip())
                if line and line not in seen:
                    seen.add(line)
                    out.append(line)
    return out


def _label_with_llm(text):
    prompt = (
        "请分析下面这段文本，只输出一行 JSON，不要其它内容。\n"
        f"可选情感（可多选，复杂多元化，例如同时包含温柔与悲伤）：{EMO_SET}\n"
        f"可选意图（单选）：{INTENT_SET}\n"
        '格式：{"emotions":[...], "intent":"...", "emotion_desc":"用一句话细腻描述这段文本应有的语气情绪", '
        '"topic":"文中可作为图片搜索关键词的实体，没有则空字符串"}\n'
        "emotions 可含 0 到多个；无明显情绪则为空数组。\n文本：\n" + text[:500]
    )
    try:
        resp = m.answer_from_platform(prompt)
    except Exception as e:
        print(f"[数据集] 调用失败跳过：{e}")
        return None
    if not resp:
        return None
    mm = re.search(r"\{.*\}", resp, re.S)
    if not mm:
        return None
    try:
        d = json.loads(mm.group(0))
    except Exception:
        return None
    emos = [e for e in d.get("emotions", []) if e in EMO_SET]
    it = d.get("intent")
    if it not in INTENT_SET:
        it = "other"
    desc = (d.get("emotion_desc") or "").strip()
    topic = (d.get("topic") or "").strip()
    return emos, it, desc, topic


def main():
    texts = _collect_texts()
    if not texts:
        print("没有待打标语料：请先运行机器人积累 data/chatlog_raw.jsonl，"
              "或在 data/chatlog.txt 放入文本。")
        return
    nlp_path = os.path.join(m.BASE_DIR, "data", "nlp.jsonl")
    emo_path = os.path.join(m.BASE_DIR, "data", "emotions.jsonl")
    int_path = os.path.join(m.BASE_DIR, "data", "intent.jsonl")
    n = 0
    with open(nlp_path, "a", encoding="utf-8") as fn, \
         open(emo_path, "a", encoding="utf-8") as fe, \
         open(int_path, "a", encoding="utf-8") as fi:
        for line in texts:
            r = _label_with_llm(line)
            if not r:
                continue
            emos, it, desc, topic = r
            entities = [topic] if topic else []
            fn.write(json.dumps({"text": line, "intent": it,
                                 "emotions": emos, "entities": entities},
                                ensure_ascii=False) + "\n")
            if emos:
                fe.write(json.dumps({"text": line, "labels": emos},
                                    ensure_ascii=False) + "\n")
            fi.write(json.dumps({"text": line, "label": it},
                                ensure_ascii=False) + "\n")
            n += 1
            print(f"已标注 {n}：{line[:18]}… -> 情感={emos} 意图={it} 题眼={topic}")
    print(f"完成，共 {n} 条 → {nlp_path} / {emo_path} / {int_path}")


if __name__ == "__main__":
    main()
