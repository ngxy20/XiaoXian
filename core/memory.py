# -*- coding: utf-8 -*-
"""本地记忆（SQLite 后端，零依赖，支持多用户）。

两层记忆：
1) 短期滚动上下文（history 表）：按 context_id（群 openid / 私聊 userid）保存最近若干轮，
   取代「纯靠思知平台服务端上下文、时长不可控」的旧机制，并支撑「主动群内互动」。
2) 长期用户画像（profile / mood 表）：按 person_id（真实的人，通常是消息发送者 openid）保存
   跨会话的偏好、雷点、身份、自定义备注与情绪史，让小忺真正「认识你」。

相比旧的 memory.json（整文件读进内存），SQLite 按需查询、按 user 建索引，用户再多也不爆内存。
"""
import json
import os
import re
import sqlite3
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(_HERE)   # 项目根目录（core/ 的上一级），保证 data/ 位置不随本文件移动而改变
DB_PATH = os.path.join(BASE_DIR, "data", "memory.db")
MAX_TURNS = 20          # 每个 context 保留的最近轮数
MAX_AGE = 6 * 3600      # 单条记忆最大存活时间（秒，仅用于 prompt 注入时不过度拉长）

_conn = None


def _db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _init_schema(_conn)
        _migrate_json(_conn)
    return _conn


def _init_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " context_id TEXT NOT NULL, role TEXT NOT NULL,"
        " name TEXT, text TEXT, ts REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_ctx ON history(context_id, id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profile ("
        " user_id TEXT NOT NULL, field TEXT NOT NULL, value TEXT, ts REAL,"
        " PRIMARY KEY (user_id, field))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mood ("
        " user_id TEXT NOT NULL, mood TEXT, ts REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mood_user ON mood(user_id, ts)")
    conn.commit()


def _migrate_json(conn):
    """首次启动：把旧 memory.json 的滚动历史迁入 SQLite（仅一次）。"""
    json_path = os.path.join(os.path.dirname(DB_PATH), "memory.json")
    if not os.path.exists(json_path):
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            old = json.load(f)
        cur = conn.cursor()
        for ctx, lst in old.items():
            for h in lst:
                cur.execute(
                    "INSERT INTO history(context_id,role,name,text,ts) VALUES(?,?,?,?,?)",
                    (str(ctx), h.get("role", "user"), h.get("name", ""),
                     h.get("text", ""), h.get("ts", time.time())),
                )
        conn.commit()
        os.rename(json_path, json_path + ".migrated")
        print(f"[记忆] 已从 memory.json 迁移历史到 SQLite（旧文件备份为 .migrated）")
    except Exception as e:
        print(f"[记忆] 旧 memory.json 迁移失败（可忽略）：{e}")


# ---------- 短期滚动上下文 ----------
def record(context_id, role, name, text, person_id=None):
    """写入一条对话。context_id 为会话维度（群=群openid/私聊=用户id）；
    person_id 为「人」的维度（发送者 openid），用于抽取长期画像。两者可相同。"""
    if not context_id or not text:
        return
    conn = _db()
    now = time.time()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO history(context_id,role,name,text,ts) VALUES(?,?,?,?,?)",
        (str(context_id), role, name or "", text, now),
    )
    # 每个 context 仅保留最近 MAX_TURNS 条
    cur.execute(
        "DELETE FROM history WHERE context_id=? AND id <= ("
        "SELECT MAX(id) FROM history WHERE context_id=?) - ?",
        (str(context_id), str(context_id), MAX_TURNS),
    )
    conn.commit()
    # 仅对用户发言抽取长期画像 + 情绪
    if role == "user":
        pid = str(person_id or context_id)
        _extract_profile(pid, name, text)
        _extract_mood(pid, text)


def get_history(context_id, max_turns=MAX_TURNS):
    conn = _db()
    rows = conn.execute(
        "SELECT role,name,text,ts FROM history WHERE context_id=? "
        "ORDER BY id DESC LIMIT ?",
        (str(context_id), max_turns),
    ).fetchall()
    rows.reverse()
    return [{"role": r[0], "name": r[1], "text": r[2], "ts": r[3]} for r in rows]


def format_history(context_id, max_turns=MAX_TURNS):
    hist = get_history(context_id, max_turns)
    if not hist:
        return ""
    lines = []
    for h in hist:
        who = h.get("name") or ("忺" if h["role"] == "bot" else "用户")
        lines.append(f"{who}：{h['text']}")
    return "\n".join(lines)


# ---------- 长期用户画像 ----------
_LIKE_RE = re.compile(r"(?:我|俺|咱)(?:最|超|很|挺|也|就)?\s*(?:喜欢|爱|迷|中意|稀饭)([^，。！？!?；;，\n]{1,20})")
_DISLIKE_RE = re.compile(r"(?:我|俺)(?:讨厌|不喜欢|烦|怕|害怕|受不|烦死)([^，。！？!?；;，\n]{1,20})")
_IDENTITY_RE = re.compile(r"^(?:我|俺)(?:是|叫)([^，。！？!?；;，\n]{1,15})")
_ATTR_RE = re.compile(r"我的(.{1,8})是(.{1,20})")

_MOOD_PATTERNS = [
    (r"开心|高兴|快乐|兴奋|雀跃|好开心|哈哈|嘻嘻|喜欢|爱你|幸福", "开心"),
    (r"难过|伤心|委屈|想哭|崩溃|抑郁|丧|低落|心痛", "难过"),
    (r"生气|愤怒|烦|怒|讨厌|无语|暴躁|气死", "烦躁"),
    (r"累|困|疲惫|想睡|瞌睡|精疲", "疲惫"),
    (r"想你|想我|想死你|想你了|想我了", "想念"),
    (r"无聊|没事干|好闲|发呆", "无聊"),
    (r"平静|还好|还行|不错|一般|凑合", "平静"),
]


def _set_field(pid, field, value):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO profile(user_id,field,value,ts) VALUES(?,?,?,?)",
        (pid, field, value, time.time()),
    )
    conn.commit()


def _append_list(pid, field, item):
    item = item.strip()
    if not item:
        return
    conn = _db()
    row = conn.execute(
        "SELECT value FROM profile WHERE user_id=? AND field=?", (pid, field)
    ).fetchone()
    items = json.loads(row[0]) if row and row[0] else []
    if item not in items:
        items.append(item)
        items = items[-12:]  # 最多保留 12 条，避免无限增长
        conn.execute(
            "INSERT OR REPLACE INTO profile(user_id,field,value,ts) VALUES(?,?,?,?)",
            (pid, field, json.dumps(items, ensure_ascii=False), time.time()),
        )
        conn.commit()


def _extract_profile(pid, name, text):
    if name and not name.startswith("群友") and name != "你":
        _set_field(pid, "nickname", name)
    m = _LIKE_RE.search(text)
    if m:
        _append_list(pid, "likes", m.group(1).strip("的是了啊"))
    m = _DISLIKE_RE.search(text)
    if m:
        _append_list(pid, "dislikes", m.group(1).strip("的是了啊"))
    m = _IDENTITY_RE.match(text.strip())
    if m:
        _set_field(pid, "identity", m.group(1).strip("我是叫"))
    for mm in _ATTR_RE.finditer(text):
        _append_list(pid, "facts", f"TA的{mm.group(1).strip('我的')}是{mm.group(2).strip()}")


def _extract_mood(pid, text):
    for pat, mood in _MOOD_PATTERNS:
        if re.search(pat, text):
            now = time.time()
            _set_field(pid, "mood", mood)
            _set_field(pid, "mood_ts", str(now))
            conn = _db()
            conn.execute(
                "INSERT INTO mood(user_id,mood,ts) VALUES(?,?,?)", (pid, mood, now)
            )
            conn.commit()
            return


def remember_fact(pid, text):
    """显式让小忺长期记住一件事（/记 指令用）。"""
    if pid and text:
        _append_list(str(pid), "facts", text.strip())


def set_profile(pid, field, value):
    _set_field(str(pid), field, value)


def get_profile(pid):
    conn = _db()
    rows = conn.execute(
        "SELECT field,value FROM profile WHERE user_id=?", (str(pid),)
    ).fetchall()
    out = {}
    for field, value in rows:
        try:
            out[field] = json.loads(value)
        except (ValueError, TypeError):
            out[field] = value
    return out


def _ago(ts):
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分钟"
    if s < 86400:
        return f"{s // 3600}小时"
    return f"{s // 86400}天"


def profile_prompt(pid):
    """拼成可读的长期记忆，注入 LLM prompt；无人设记忆则返回空串。"""
    p = get_profile(pid)
    if not p:
        return ""
    lines = []
    nick = p.get("nickname")
    if nick and nick not in ("群友", "你"):
        lines.append(f"TA的称呼：{nick}")
    if p.get("identity"):
        lines.append(f"TA说自己是：{p['identity']}")
    likes = p.get("likes")
    if likes:
        lines.append("TA喜欢：" + "、".join(likes))
    dislikes = p.get("dislikes")
    if dislikes:
        lines.append("TA不喜欢：" + "、".join(dislikes))
    facts = p.get("facts")
    if facts:
        lines.append("关于TA：" + "；".join(facts[-5:]))
    mood = p.get("mood")
    if mood:
        lines.append(f"TA最近情绪：{mood}（约{_ago(p.get('mood_ts'))}前）")
    return "\n".join(lines)


def clear_profile(pid):
    conn = _db()
    conn.execute("DELETE FROM profile WHERE user_id=?", (str(pid),))
    conn.execute("DELETE FROM mood WHERE user_id=?", (str(pid),))
    conn.commit()
