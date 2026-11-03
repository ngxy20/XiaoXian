"""从 kaomojis.jp 抓取颜文字，按情绪归类，输出 kaomoji.json。

用法：python scrape_kaomoji.py
产物：kaomoji.json  结构 {emotions: {情绪: {label: 中文名, items: [[颜文字, 说明], ...]}}}
"""
import html as htmllib
import json
import os
import re
import time

import requests

BASE = "https://kaomojis.jp"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "kaomoji.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PER_EMOTION = 100

# 专题页 slug -> 情绪分组（只抓这里列出的，节省请求）
TOPIC_EMOTION = {
    "happy": "happy", "laughing": "happy", "excited": "happy",
    "funny": "happy", "xd": "happy", "playful": "happy",
    "love": "love", "kiss": "love", "kawaii": "love", "girly": "love",
    "uwu": "love", "angel": "love", "sparkle": "love", "star": "love",
    "flower": "love",
    "shy": "shy", "blushing": "shy", "embarrassed": "shy",
    "awkward": "awkward",
    "confused": "confused", "thinking": "confused", "shrug": "confused",
    "surprised": "surprise", "shocked": "surprise",
    "sad": "sad", "crying": "sad", "lonely": "sad", "disappointed": "sad",
    "pouting": "sad", "dizzy": "sad",
    "frustrated": "worry", "stressed": "worry", "worried": "worry",
    "nervous": "worry", "sick": "worry", "scared": "worry",
    "angry": "angry", "disapproval": "angry", "devil": "angry",
    "sorry": "sorry", "facepalm": "sorry",
    "thankyou": "thanks", "thumbs-up": "thanks",
    "greeting": "greet", "waving": "greet", "farewell": "greet",
    "goodnight": "goodnight",
    "sleepy": "sleepy", "tired": "sleepy", "yawning": "sleepy",
    "calm": "calm", "relaxed": "calm", "cozy": "calm", "dreamy": "calm",
    "cool": "proud", "smug": "proud", "proud": "proud", "determined": "proud",
    "sarcastic": "sarcastic",
    "bored": "bored",
    "hug": "comfort",
    "party": "cheer", "dancing": "cheer", "magic": "cheer", "wink": "cheer",
    "animal": "animal", "cat": "animal", "dog": "animal", "bear": "animal",
    "panda": "animal", "rabbit": "animal", "fox": "animal", "frog": "animal",
    "owl": "animal", "koala": "animal", "penguin": "animal", "unicorn": "animal",
    "hamster": "animal", "dolphin": "animal", "whale": "animal",
    "dragon": "animal", "bird": "animal", "butterfly": "animal",
    "food": "food", "hungry": "food", "cooking": "food", "coffee": "food",
    "weather": "weather",
    "beach": "scene", "nature": "scene", "ocean": "scene",
    "music": "hobby", "gaming": "hobby", "sports": "hobby",
    "exercise": "hobby", "running": "hobby", "studying": "hobby",
}

EMOTION_LABEL = {
    "happy": "开心", "love": "喜欢", "shy": "害羞", "awkward": "尴尬",
    "confused": "困惑", "surprise": "惊讶", "sad": "难过", "worry": "沮丧",
    "angry": "生气", "sorry": "道歉", "thanks": "感谢", "greet": "打招呼",
    "goodnight": "晚安", "sleepy": "困倦", "calm": "平静", "proud": "得意",
    "sarcastic": "调侃", "bored": "无聊", "comfort": "安慰", "cheer": "欢呼",
    "animal": "动物", "food": "食物", "weather": "天气", "scene": "风景",
    "hobby": "兴趣",
}

BTN_RE = re.compile(r'<button[^>]*?data-text="([^"]+)"[^>]*?>(.*?)</button>', re.S)
DIV_RE = re.compile(r"<div[^>]*>(.*?)</div>", re.S)
DIV_KM_RE = re.compile(r'<div class="[^"]*kaomoji-text[^"]*"[^>]*>(.*?)</div>')


def fetch(url, tries=3):
    for _ in range(tries):
        try:
            r = requests.get(url, timeout=25, headers=UA)
            if r.ok:
                return r.content.decode("utf-8", "ignore")
        except Exception:
            pass
        time.sleep(1.5)
    return ""


def _clean(fragment):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse(page_html):
    """专题页结构：<button data-text="颜文字">…描述…</button>"""
    out = []
    for text, inner in BTN_RE.findall(page_html):
        text = htmllib.unescape(text).strip()
        if not text or len(text) > 40:
            continue
        divs = [_clean(d) for d in DIV_RE.findall(inner)]
        desc = divs[1] if len(divs) > 1 else ""
        out.append((text, desc[:30]))
    if not out:  # 兜底：分类页用另一种结构
        for text in DIV_KM_RE.findall(page_html):
            text = htmllib.unescape(text).strip()
            if text and len(text) <= 40:
                out.append((text, ""))
    return out


def main():
    index = fetch(BASE + "/zh")
    slugs = set(re.findall(r'href="/zh/([a-z0-9\-]+)-kaomoji"', index))
    # 主 slug（名字等于情绪 key，如 happy/sad/love）优先抓，保证每类内容更纯粹
    wanted = sorted((s for s in slugs if s in TOPIC_EMOTION),
                    key=lambda s: (0 if s == TOPIC_EMOTION[s] else 1, s))
    print("topic pages found:", len(slugs), "-> to fetch:", len(wanted))

    buckets = {}
    for slug in wanted:
        key = TOPIC_EMOTION[slug]
        items = parse(fetch(f"{BASE}/zh/{slug}-kaomoji"))
        if not items:  # 专题页取不到就退回分类页
            items = parse(fetch(f"{BASE}/zh/category/{slug}"))
        if not items:
            print("  !! empty:", slug)
            continue
        bucket = buckets.setdefault(key, [])
        have = {t for t, _ in bucket}
        added = 0
        for text, desc in items:
            if text in have:
                continue
            have.add(text)
            bucket.append([text, desc])
            added += 1
            if len(bucket) >= PER_EMOTION:
                break
        print(f"  {slug:16s} -> {key:9s} +{added}")
        time.sleep(0.3)

    emotions = {}
    for key, items in buckets.items():
        if items:
            emotions[key] = {"label": EMOTION_LABEL.get(key, key), "items": items}
    data = {
        "source": BASE + "/zh",
        "note": "抓取自 kaomojis.jp，用于聊天回复中的表情符号",
        "emotions": emotions,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    total = sum(len(v["items"]) for v in emotions.values())
    print("emotions:", len(emotions), "total:", total)
    print("saved ->", OUT)


if __name__ == "__main__":
    main()