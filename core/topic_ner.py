# -*- coding: utf-8 -*-
"""话题词抽取（NLP / NER）：用 jieba 词性标注抽取名词实体作为图片搜索关键词。

未安装 jieba 时返回 None，由调用方回退正则。
"""
try:
    import jieba
    import jieba.posseg as pseg
    _HAS = True
except Exception:
    _HAS = False

_STOP = set(
    "的 了 吗 呢 吧 啊 呀 哦 嘛 你 我 他 她 它 这 那 个 些 们 在 是 有 说 讲 看 听 给 "
    "要 想 觉得 什么 怎么 如何 为什么 一张 一个 来 去 上 下".split()
)


def extract_topic(text, maxlen=12):
    """返回最具体的名词实体作为话题词；无法抽取返回 None。"""
    if not _HAS or not text:
        return None
    try:
        cands = []
        for w, flag in pseg.cut(text):
            if flag.startswith("n") and w not in _STOP and len(w) >= 2:
                cands.append(w)
        if not cands:
            return None
        cands.sort(key=lambda x: -len(x))  # 取最长（最具体）的名词
        return cands[0][:maxlen]
    except Exception:
        return None
