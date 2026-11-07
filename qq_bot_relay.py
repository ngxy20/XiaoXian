# -*- coding: utf-8 -*-
"""
小忺聊天机器人（忺）—— QQ 官方机器人（方案A）

凭据不写在代码里：首次运行会在脚本同目录生成 config.json（模板），
填好后重新运行即可。config.json 记得不要上传/分享（已在 .gitignore 中忽略）。

前提：
  • 到 q.qq.com 创建机器人，拿到 AppID 和 AppSecret（在「开发设置」里）。
  • 把机器人拉进群，群里 @它 即可对话。群聊能力需审核。

工作流程：
  QQ群 @机器人 + 问题  →  本脚本收到  →  优先调用「DeepSeek 对话 API」拿答案
                                              （拿不到则用本地 xlsx 关键词匹配兜底）
                       →  把答案发回 QQ 群

依赖：
  pip install botpy openpyxl requests

代码约定：
  - 命名：模块级常量全大写下划线（如 _NO_ANSWER_FALLBACK）；私有辅助以单下划线开头；
    公开命令处理函数统一以 _cmd_ 前缀命名，返回回复文本或 None。
  - 注释：用中文阐明「为什么」而非「做什么」；自解释的代码不加注释，避免过度注释。
  - 入口：群消息走 on_group_message_create（全量）/ on_group_at_message_create（@ 事件）；
    私聊走 on_c2c_message_create；回复统一经 _reply（自动分配 msg_seq + 失败重试一次）。
  - 配置：敏感信息放 config.json / assets/*.json，绝不硬编码；多数配置支持 mtime 热更新，改完即时生效。
"""

import urllib.parse as _uparse
import urllib.request as _ureq
import time as _time
import memory  # 本地滚动对话记忆（数据/data/memory.db）
import asyncio
import base64
import datetime
import io
import json
import os
import random
import re
import threading
import time
from urllib.parse import quote

# 让 core/ 下的模块（memory / nlp_*）可被直接 import（脚本在根目录运行时 core 不在 sys.path）
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))

# ---------- 可选 NLP 模块（未安装 torch/transformers/jieba 或权重缺失时自动回退规则）----------
try:
    import emotion_model
except Exception:
    emotion_model = None
try:
    import intent_model
except Exception:
    intent_model = None
try:
    import topic_ner
except Exception:
    topic_ner = None
try:
    import nlp_understanding
except Exception:
    nlp_understanding = None
try:
    import voice_persona
except Exception:
    voice_persona = None
if emotion_model is not None or intent_model is not None or topic_ner is not None or nlp_understanding is not None:
    print("[NLP] 情感/意图分类/话题NER/统一多任务理解模块已接入（权重缺失时自动回退规则）")
if voice_persona is not None:
    print("[语音拟人化] 场景情绪识别 + 多情绪标签参数模型已接入（配置可热更新）")

# ---------- 全量群消息解析器注入 ----------
# 思知/QQ 在「群消息」权限开通后，会推送 GROUP_MESSAGE_CREATE（群内所有消息，不止@）。
# botpy 默认只解析 group_at_message_create，这里补一个解析器，使 on_group_message_create 能触发。
try:
    from botpy.connection import ConnectionState
    from botpy.message import GroupMessage, C2CMessage

    # 只探测一次：打印第一条群消息的原始 author 字段，用来确认 botpy 到底给不给昵称
    _NICK_PROBE = {"done": False}
    _MEDIA_PROBE = {"done": False}

    def _parse_group_message_create(self, payload):
        _d = payload.get("d", {}) or {}
        try:
            if not _NICK_PROBE["done"]:
                _auth = _d.get("author", {}) or {}
                if isinstance(_auth, dict):
                    print("[昵称探测] author 键 =", list(_auth.keys()))
                    print("[昵称探测] author 关键值 =",
                          {k: _auth.get(k) for k in ("nick", "username", "name",
                                                     "member_openid", "id", "union_openid",
                                                     "member_role")})
                print("[昵称探测] payload 顶层键 =", list(_d.keys()))
                _NICK_PROBE["done"] = True
            # 多媒体消息（图片/语音）的结构只探一次，方便后续接视觉/ASR
            if (not _MEDIA_PROBE["done"]) and (
                    _d.get("message_type") not in (None, "text", "")
                    or not (_d.get("content") or "").strip()):
                print("[昵称探测] 非文本消息 payload =", _d)
                _MEDIA_PROBE["done"] = True
        except Exception as _e:
            print("[昵称探测] 异常:", _e)
        # 把平台给的群内昵称(username)与身份头衔(member_role)接回来（botpy 的 _User 只留了 member_openid）
        _auth = _d.get("author", {}) or {}
        _oid = _auth.get("member_openid") or _auth.get("user_openid") or _auth.get("id")
        _uname = (_auth.get("username") or _auth.get("nick") or _auth.get("name") or "").strip()
        if _oid and _uname:
            _live_nick[_oid] = _uname
        _role = _auth.get("member_role")
        if _oid and _role:
            _live_role[_oid] = _role
        _message = GroupMessage(self.api, payload.get("id", None), _d)
        try:
            if _uname:
                setattr(_message.author, "username", _uname)  # 让 _display_name 也能拿到
            if _role:
                setattr(_message.author, "role", _role)
            setattr(_message.author, "bot", bool(_auth.get("bot")))  # 标记是否机器人（过滤别的bot消息）
            # 把原始多媒体信息挂到消息上，供后续视觉/ASR/上下文使用
            setattr(_message, "attachments", _d.get("attachments"))
            setattr(_message, "msg_type", _d.get("message_type"))
            setattr(_message, "mentions", _d.get("mentions"))  # 供「是否 @ 了本机器人」稳健判定
            # 原始 payload 也挂上：若 mentions 是只读属性导致上面 setattr 静默失败，
            # 仍能从 _raw_payload 拿到原始的 mention dict（botpy 可能把它解析成对象，判断会漏）
            setattr(_message, "_raw_payload", _d)
        except Exception:
            pass
        self._dispatch("group_message_create", _message)

    def _parse_c2c_message_create(self, payload):
        _d = payload.get("d", {}) or {}
        # 私聊同样把平台昵称(username)与头衔(member_role)接回，并挂上多媒体原始数据
        _auth = _d.get("author", {}) or {}
        _oid = _auth.get("member_openid") or _auth.get("user_openid") or _auth.get("id")
        _uname = (_auth.get("username") or _auth.get("nick") or _auth.get("name") or "").strip()
        if _oid and _uname:
            _live_nick[_oid] = _uname
        _role = _auth.get("member_role")
        if _oid and _role:
            _live_role[_oid] = _role
        _message = C2CMessage(self.api, payload.get("id", None), _d)
        try:
            if _uname:
                setattr(_message.author, "username", _uname)
            if _role:
                setattr(_message.author, "role", _role)
            setattr(_message.author, "bot", bool(_auth.get("bot")))
            setattr(_message, "attachments", _d.get("attachments"))
            setattr(_message, "msg_type", _d.get("message_type"))
        except Exception:
            pass
        self._dispatch("c2c_message_create", _message)

    ConnectionState.parse_group_message_create = _parse_group_message_create
    ConnectionState.parse_c2c_message_create = _parse_c2c_message_create
    print("[botpy] 已注入全量群消息/私聊消息解析器，机器人可读取群内所有消息并接回昵称")
except Exception as e:
    print(f"[botpy] 全量群消息解析器注入失败（将仅接收@消息）：{e}")

# 主动群内互动的参数集中在 assets/proactive_config.json（可热更新，无需重启），详见该文件。
_PROACTIVE_LAST = {}         # group_openid -> 上次主动发言时间戳
_HANDLED_AT = set()          # 已被 @ 处理器接管的消息 id（避免重复回复）

# 跨事件去重：QQ 的 AT 事件与全量群消息事件对同一消息的 id 往往不同，
# 单靠 mid 去重会失效（同一 /说 被两个入口各处理一次 -> 发两条语音）。
# 改用 (群 + 发送者 + 正文) 这个跨事件稳定的键做去重。
_HANDLED_KEYS = {}            # key -> 过期时间戳


def _hkey(group_id, sender, content):
    return f"{group_id}|{sender}|{content}"


def _mark_handled(key, ttl=8.0):
    _HANDLED_KEYS[key] = _time.time() + ttl


def _is_handled(key):
    now = _time.time()
    expired = [k for k, v in _HANDLED_KEYS.items() if v < now]
    for k in expired:
        _HANDLED_KEYS.pop(k, None)
    return _HANDLED_KEYS.get(key, 0) > now

# ---------- 机器人在各群的 openid（学会一次就永久记住，重启不丢）----------
# 群消息里 @ 用的是「群 member_openid」（如 F833E28C…），和 on_ready 给的 user_openid 是两套 ID，
# 而 AT 事件在部分代理/环境里根本不下发 → 学会的 id 必须落盘，否则每次重启都要重新学、一学不会就 @ 全失灵。


def _load_bot_openids():
    """读取已记住的「本机器人在各群的 openid」：{group_openid: [openid, ...]}。"""
    try:
        with open(_BOT_OPENID_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            out = {}
            for k, v in d.items():
                if isinstance(v, (list, tuple, set)):
                    s = {str(x) for x in v if x}
                    if s:
                        out[str(k)] = s
            return out
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[openid] 读取 bot_openids.json 失败：{e}")
    return {}


def _save_bot_openids(oids):
    try:
        with open(_BOT_OPENID_FILE, "w", encoding="utf-8") as f:
            json.dump({k: sorted(v) for k, v in (oids or {}).items()},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[openid] 保存 bot_openids.json 失败：{e}")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 机器人在各群的 openid 落盘位置（见上方 _load_bot_openids）
_BOT_OPENID_FILE = os.path.join(BASE_DIR, "assets", "bot_openids.json")

# ---------- 主动回复参数（独立配置文件，可热更新）----------
# 所有主动接话的开关/概率/冷却都放这里，改完保存即生效，无需重启。
PROACTIVE_CONFIG_FILE = os.path.join(BASE_DIR, "assets", "proactive_config.json")
_PROACTIVE_CFG_FALLBACK = {
    "enabled": True,
    "default_level": "低",
    "levels": {
        "关闭": {"enabled": False, "chance": 0.0,   "cooldown": 999999},
        "低":   {"enabled": True,  "chance": 0.06,  "cooldown": 120},
        "极低": {"enabled": True,  "chance": 0.02,  "cooldown": 300},
        "近乎静默": {"enabled": True, "chance": 0.005, "cooldown": 900},
        "中":   {"enabled": True,  "chance": 0.15,  "cooldown": 30},
        "高":   {"enabled": True,  "chance": 0.35,  "cooldown": 10},
    },
}
_proac_cfg_cache = {"mtime": -1, "data": None}


def load_proactive_config():
    """读取 assets/proactive_config.json，按修改时间缓存；缺失/出错时回退内置默认。"""
    global _proac_cfg_cache
    try:
        mtime = os.path.getmtime(PROACTIVE_CONFIG_FILE)
    except OSError:
        mtime = -1
    if _proac_cfg_cache["data"] is not None and _proac_cfg_cache["mtime"] == mtime:
        return _proac_cfg_cache["data"]
    data = {}
    try:
        if os.path.exists(PROACTIVE_CONFIG_FILE):
            with open(PROACTIVE_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"[主动配置] 读取 proactive_config.json 失败，使用内置默认：{e}")
    if not isinstance(data, dict):
        data = {}
    out = {
        "enabled": bool(data.get("enabled", _PROACTIVE_CFG_FALLBACK["enabled"])),
        "default_level": data.get("default_level", _PROACTIVE_CFG_FALLBACK["default_level"]),
        "levels": data.get("levels", _PROACTIVE_CFG_FALLBACK["levels"]),
    }
    _proac_cfg_cache["data"] = out
    _proac_cfg_cache["mtime"] = mtime
    return out


def _proactive_levels():
    return load_proactive_config().get("levels", _PROACTIVE_CFG_FALLBACK["levels"])


# ---------- 主动回复频率（三档 + 关闭），按群/私聊持久化 ----------
_REPLY_FREQ_FILE = os.path.join(BASE_DIR, "data", "bot_settings.json")
_REPLY_FREQ = {}   # { "<group_id 或 private:<sender>>": "中" / "低" / "高" / "关闭" }


def _load_reply_freq():
    global _REPLY_FREQ
    _REPLY_FREQ = {}
    try:
        if os.path.exists(_REPLY_FREQ_FILE):
            with open(_REPLY_FREQ_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                _REPLY_FREQ = {str(k): v for k, v in d.items()}
    except Exception:
        _REPLY_FREQ = {}


def _save_reply_freq():
    try:
        os.makedirs(os.path.dirname(_REPLY_FREQ_FILE), exist_ok=True)
        with open(_REPLY_FREQ_FILE, "w", encoding="utf-8") as f:
            json.dump(_REPLY_FREQ, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[频率] 保存失败：{e}")


_load_reply_freq()

# 三档参数（关闭/低/中/高）的 开关/概率/冷却 统一从 assets/proactive_config.json 的 levels 读取（可热更新）


def get_reply_freq(key):
    lv = _REPLY_FREQ.get(str(key))
    if lv:
        return lv
    # 未单独设置过的群/私聊，跟随配置里的 default_level（让 proactive_config.json 的 default_level 真正生效）
    return load_proactive_config().get("default_level", "低")


def set_reply_freq(key, level):
    if level not in _proactive_levels():
        return False
    _REPLY_FREQ[str(key)] = level
    _save_reply_freq()
    return True


def reply_freq_params(key):
    level = get_reply_freq(key)
    cfg = load_proactive_config()
    levels = cfg.get("levels", _PROACTIVE_CFG_FALLBACK["levels"])
    lp = levels.get(level) or levels.get(cfg.get("default_level", "中")) or {
        "enabled": True, "chance": 0.15, "cooldown": 30}
    enabled = bool(cfg.get("enabled", True)) and bool(lp.get("enabled", True))
    return (enabled, float(lp.get("chance", 0.15)), int(lp.get("cooldown", 30)))


# 实时记录群友/私聊原始消息，作为情感训练语料（仅存原文+元信息，不打标）
_RAW_LOG_PATH = os.path.join(BASE_DIR, "data", "chatlog_raw.jsonl")


def _log_raw_message(message, text, kind="group"):
    """实时落盘所有到达的消息原文，供日后情感/意图训练使用。"""
    try:
        author = getattr(message, "author", None)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "group": getattr(message, "group_openid", None),
            "user": (getattr(author, "member_openid", None)
                     or getattr(author, "user_openid", None)),
            "text": (text or "").strip(),
        }
        if rec["text"]:
            with open(_RAW_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[日志] 原始消息记录失败：{e}")


CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ===================== 配置项（只作为默认值/模板，实际值写在 config.json）=====================
DEFAULT_CONFIG = {
    # ---- QQ 官方机器人（q.qq.com 后台「开发设置」获取）----
    "QQ_APPID": "",       # 机器人 AppID
    "QQ_SECRET": "",      # 机器人 AppSecret（只显示一次，保存好）

    # ---- 云端大模型（可切换双后端：DeepSeek / 思知 sizhi）----
    # ENABLE_PLATFORM_API=false 时完全停用云端大模型，只用本地 xlsx 兜底
    # LLM_BACKEND 选用哪个云端后端："deepseek" 或 "sizhi"；所选后端未配置时自动回退到另一个可用后端
    "ENABLE_PLATFORM_API": True,
    "LLM_BACKEND": "deepseek",  # 云端大模型后端：deepseek / sizhi / off（off=直接关闭云端大模型，仅用本地题库）
    # DeepSeek（OpenAI 兼容 /chat/completions）
    "DEEPSEEK_API_KEY": "",        # DeepSeek API Key（https://platform.deepseek.com），留空则不可用
    "DEEPSEEK_API_URL": "https://api.deepseek.com/chat/completions",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "DEEPSEEK_VISION_MODEL": "deepseek-flash",  # 带图片时使用的视觉模型（DeepSeek Flash 支持图片输入）
    "AUTO_WEB_SEARCH": False,            # 自动联网：用户消息含搜索意图时自动检索并喂给模型（默认关，开通后按需开启，注意计费）
    "WEB_SEARCH_COUNT": 3,               # 自动联网时取几条结果
    # 思知 sizhi.com（/chat 接口，通用聊天模型；人设见 _PERSONA）
    "PLATFORM_API_URL": "https://api.sizhi.com/chat",
    "PLATFORM_APPID": "92cb3254afa046e584c0bccca1a3cbb4",  # 思知机器人 appid，形象设置里查看/替换
    # userid（区分用户/上下文记忆）会自动用「群 openid」生成，无需手动填

    # ---- 词元预算（防欠费）----
    # 0 = 不限制；填正整数 = 累计估算词元上限（输入+输出字符数粗估），超过即停用云端大模型、改用本地题库
    "PLATFORM_TOKEN_BUDGET": 0,

    # ---- 语音输出（TTS：官方 MiMo API，OpenAI 兼容 chat/completions + audio）----
    "MIMO_API_KEY": "",                       # 官方 MiMo API Key（api.xiaomimimo.com）；留空则语音功能不可用
    "MIMO_BASE_URL": "https://api.xiaomimimo.com/v1",
    "MIMO_MODEL": "mimo-v2.5-tts",
    "MIMO_VOICE": "mimo_default",             # 预置音色：mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean
    "VOICE_STUDIO_API_KEY": "",                # 阿里云 Voice Studio（convai-platform）TTS Key；留空则该后端不可用
    "VOICE_STUDIO_BASE_URL": "https://convai-platform.aliyuncs.com",
    "VOICE_STUDIO_MODEL": "cosyvoice-v3-flash",
    "VOICE_MAX_CHARS": 150,                   # 单条语音最大字数（过长截断，避免音频过大）

    # ---- 本地问答库兜底 ----
    "QA_XLSX_PATH": "",     # 留空 = assets/聊天机器人问答库.xlsx
    "QA_SHEET": 0,          # 第几个 sheet（0 = 第一个）
    "QA_QUESTION_COL": 1,   # 问题列（0开始计数，1=第2列B列）
    "QA_ANSWER_COL": 2,     # 答案列（2=第3列C列）

    # ---- 触发规则 ----
    "REPLY_PREFIX": "",     # 回复内容前缀，例如 "「小忺」"，可留空
    "REPLY_PRIVATE": True,  # 是否回复单聊（私聊）消息；false = 只回群里 @
    # 已废弃：曾用于「丢弃来自异环角色文档的回答」。现在平台直接当通用聊天模型，此开关不再生效
    "USE_CHAR_DOC": False,
    "IMAGE_LIB": [],         # 普通图片路径/URL 列表（聊天偶尔顺带发）
    "EMOTICON_LIB": [],      # 表情包本地路径（聊天偶尔当反应发）
    "AUCTION_LIB": [],       # 拍卖/竞拍截图（仅聊拍卖时发）
    "ROSTER_LIB": [],        # 角色列表/配队截图（仅聊配队时发）
    "OWNER_IMAGES": [],      # 群主（远旅）的照片，专供「看看远旅」发送
    "MEMBER_IMAGES": {},      # 群员照片映射：{成员名: [图片路径...]}，供「看看XX」直接发
    "STYLE_MAX_CATCH": 60,     # /学说话 口头禅最多保留多少条（之前 20 太紧，放宽）
    "STYLE_MAX_EX": 30,        # /学说话 示例对话最多保留多少组（之前 12 太紧，放宽）
}
# ==========================================================================================


def load_config():
    """从 config.json 读配置；文件不存在则生成模板后退出。"""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已生成模板：{CONFIG_FILE}")
        print("[配置] 请填写后重新运行（该文件含密钥，不要上传或分享）")
        raise SystemExit(0)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        user_cfg = json.load(f)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in user_cfg.items() if v is not None})
    # 若模板新增了配置项，把缺失的默认键补全并写回，避免用户手动加
    if set(DEFAULT_CONFIG) - set(user_cfg):
        merged = dict(DEFAULT_CONFIG)
        merged.update(user_cfg)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已补全 {CONFIG_FILE} 的缺省项，可重新检查")
    print(f"[配置] 已读取 {CONFIG_FILE}")
    return cfg


CONFIG = load_config()
_CONFIG_MTIME = {"v": -1}


def _config_fresh():
    """config.json 带修改时间热重载：改完即生效，机器人和自动脚本都不用重启。

    原地更新 CONFIG（dict 引用不变），所有直接用 CONFIG[...] 的地方都能拿到新值。
    """
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mt = -1
    if _CONFIG_MTIME["v"] != mt:
        try:
            fresh = load_config()
        except SystemExit:
            fresh = None
        if isinstance(fresh, dict):
            CONFIG.clear()
            CONFIG.update(fresh)
        _CONFIG_MTIME["v"] = mt
    return CONFIG


def _prune_libs():
    """清理图库里已不存在的本地路径（如旧贴吧图），避免发图报文件缺失、并让自动扒图能重跑。"""
    changed = False
    for key in ("IMAGE_LIB", "EMOTICON_LIB", "AUCTION_LIB", "ROSTER_LIB"):
        lib = CONFIG.get(key)
        if not lib:
            continue
        kept = [p for p in lib
                if not isinstance(p, str) or p.startswith("http") or os.path.isfile(p)]
        if len(kept) != len(lib):
            CONFIG[key] = kept
            changed = True
            print(f"[图库] 清理 {key} 失效路径 {len(lib) - len(kept)} 条")
    return changed


_prune_libs()


# ---------- 云端大模型用量预算（防欠费）----------
# 累计估算词元持久化到 usage.json，超过 PLATFORM_TOKEN_BUDGET 就停用云端大模型、转本地
USAGE_FILE = os.path.join(BASE_DIR, "data", "usage.json")
_usage = {"tokens": 0, "calls": 0}
_usage_warned = False


def load_usage():
    global _usage
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _usage = {"tokens": int(d.get("tokens", 0)),
                      "calls": int(d.get("calls", 0)),
                      "day": d.get("day")}
        except Exception:
            pass
    budget = CONFIG.get("PLATFORM_TOKEN_BUDGET") or 0
    if budget and budget > 0:
        print(f"[用量] 云端大模型累计估算词元 {_usage['tokens']} / 上限 {budget}")
    else:
        print("[用量] 云端大模型词元预算未设（0=不限制）")


def save_usage():
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(_usage, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _reset_usage_if_new_day():
    """词元按自然日刷新：跨到新的一天就把本地计数清零，并解除「已达上限」告警（配额每天 0 点重置）。"""
    global _usage, _usage_warned
    today = datetime.date.today().isoformat()
    if _usage.get("day") != today:
        _usage = {"tokens": 0, "calls": 0, "day": today}
        _usage_warned = False
        save_usage()
        print(f"[用量] 新的一天，云端大模型词元计数已重置（{today}）")


def _estimate_tokens(text: str) -> int:
    """高估词元数：宁可多算、绝不漏算，避免真实消耗悄悄超限欠费。
    CJK 汉字/全角字符按 2 token，其余字符（英文/数字/标点/空白）按 1，
    再整体乘 1.2 安全系数，覆盖换行、控制符及不同模型分词差异。"""
    n = 0
    for ch in (text or ""):
        n += 2 if ord(ch) > 0x2E7F else 1
    return int(n * 1.2) + 1


load_usage()


# ---------- 本地问答库（兜底用）----------
_qa_cache = None


def qa_path() -> str:
    return CONFIG["QA_XLSX_PATH"] or os.path.join(BASE_DIR, "assets", "聊天机器人问答库.xlsx")


def load_qa():
    """读取 xlsx，返回 [(问题, 答案), ...]"""
    global _qa_cache
    if _qa_cache is not None:
        return _qa_cache
    from openpyxl import load_workbook
    path = qa_path()
    if not os.path.exists(path):
        print(f"[问答库] 未找到 {path}，只能用默认回复")
        _qa_cache = []
        return _qa_cache
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[CONFIG["QA_SHEET"]]
    qc, ac = CONFIG["QA_QUESTION_COL"], CONFIG["QA_ANSWER_COL"]
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # 跳过表头
            continue
        q = row[qc] if len(row) > qc else None
        a = row[ac] if len(row) > ac else None
        if q and a:
            rows.append((str(q).strip(), str(a).strip()))
    _qa_cache = rows
    return _qa_cache


def _norm(text: str) -> str:
    """去空白、转小写，便于做字符级比对。"""
    return re.sub(r"\s+", "", text.lower())


def _bigrams(text: str) -> set:
    """把字符串切成相邻 2 字组合（bigram），中文相似度比对很管用。"""
    t = _norm(text)
    if len(t) <= 1:
        return set(t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def answer_from_xlsx(question: str):
    """用字符 bigram 相似度做模糊匹配，返回最相关的一条答案，没匹配到返回 None。
    相比旧版「字符交集>=3」，bigram 能容忍同义改写、也不容易误命中常用字。"""
    qa = load_qa()
    if not qa:
        return None
    q = question.strip()
    if not q:
        return None
    # 1) 题库问题原文完整出现在用户问题里，直接命中
    for ask, ans in qa:
        if ask and ask in q:
            return ans
    # 2) bigram Jaccard 相似度，取最高的一项
    qg = _bigrams(q)
    if not qg:
        return None
    best, best_score = None, 0.0
    for ask, ans in qa:
        if not ask:
            continue
        ag = _bigrams(ask)
        if not ag:
            continue
        score = len(qg & ag) / len(qg | ag)
        if score > best_score:
            best_score, best = score, ans
    # 阈值可按需调：0.34 大致等于约 1/3 的相邻字重叠
    return best if best_score >= 0.34 else None


# ---------- 云端大模型 API（DeepSeek，OpenAI 兼容 /chat/completions）----------
# 图片下载 / 编码：QQ 群图片是带 rkey 的临时下载链，可能过期或不被模型服务端直连拉取；
# 故先下载成本地字节再转 base64 data URL 喂给视觉模型，更稳。仅允许 QQ 官方图床域名，防 SSRF。
_QQ_IMG_HOSTS = ("multimedia.nt.qq.com.cn", "nt.qq.com.cn", "qq.com", "qq.com.cn")


def _is_safe_image_host(url):
    try:
        from urllib.parse import urlparse
        h = (urlparse(url).hostname or "").lower()
        return any(h == x or h.endswith("." + x) for x in _QQ_IMG_HOSTS)
    except Exception:
        return False


def _image_mime_from_bytes(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # 兜底


def _fetch_image_data_url(url, max_bytes=24 * 1024 * 1024):
    """把图片 URL 下载并编码成 base64 data URL；失败/非 QQ 图床/过大 返回 None。"""
    if not _is_safe_image_host(url):
        return None
    try:
        import requests
        import base64
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": "https://q.qq.com/",
        }
        with requests.get(url, timeout=15, stream=True, headers=headers) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(1024 * 256):
                data += chunk
                if len(data) > max_bytes:
                    return None
        if not data:
            return None
        mime = _image_mime_from_bytes(data)
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception as e:
        print(f"[视觉] 图片下载失败（{str(url)[:60]}…）：{e}")
        return None


# ---------- 联网搜索 / 网页抓取（DeepSeek Search & Fetch API，与 DeepSeek API 同 Key 鉴权）----------
def _log_web_error(api, code, text):
    print(f"[Web] {api} 失败 HTTP {code}: {str(text)[:200]}")


def _web_search(query, count=5, need_content=False, ori_question=None):
    """调用 DeepSeek Search 接口搜索网页；返回结果列表，失败返回 None，无结果返回 []。"""
    key = CONFIG.get("DEEPSEEK_API_KEY")
    if not key:
        print("[Web] 未配置 DEEPSEEK_API_KEY，无法搜索")
        return None
    try:
        import requests
        payload = {"query": query, "count": count, "need_content": need_content}
        if ori_question:
            payload["ori_question"] = ori_question[:128]
        resp = requests.post(
            "https://api.deepseek.com/v1/tools/search",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json=payload, timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("results") or []
        _log_web_error("Search", resp.status_code, resp.text)
        return None  # 非 200 一律视为失败（401 无权限/402 欠费/404 无内容/429 限流/5xx）；仅 200 空数组才是真·无结果
    except Exception as e:
        print(f"[Web] Search 异常：{e}")
        return None


def _web_fetch(url, max_chars=6000):
    """调用 DeepSeek Fetch 接口抓取网页正文（Markdown）；返回 (title, content) 或 None。"""
    key = CONFIG.get("DEEPSEEK_API_KEY")
    if not key:
        print("[Web] 未配置 DEEPSEEK_API_KEY，无法抓取")
        return None
    if not str(url).startswith("http"):
        return None
    try:
        import requests
        resp = requests.post(
            "https://api.deepseek.com/v1/tools/fetch",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"url": url}, timeout=25,
        )
        if resp.status_code == 200:
            data = resp.json()
            return (data.get("title", ""), data.get("content", "") or "")
        _log_web_error("Fetch", resp.status_code, resp.text)
        return None
    except Exception as e:
        print(f"[Web] Fetch 异常：{e}")
        return None


_WEB_SEARCH_TRIGGERS = ("搜索", "搜一下", "搜一搜", "查一下", "查一查", "上网搜",
                        "网络搜索", "网上搜", "帮我搜", "搜新闻", "查资料", "搜资料",
                        "搜个", "查个", "搜下")


def _wants_web_search(content):
    if not CONFIG.get("AUTO_WEB_SEARCH"):
        return False
    if not content or len(content) > 200:
        return False
    return any(t in content for t in _WEB_SEARCH_TRIGGERS)


def _web_search_note(content):
    """自动联网：命中搜索意图时检索并拼成上下文块；关闭/无结果/失败返回空串。"""
    if not _wants_web_search(content):
        return ""
    try:
        res = _web_search(content, count=int(CONFIG.get("WEB_SEARCH_COUNT", 3)), need_content=False)
    except Exception:
        return ""
    if not res:
        return ""
    lines = []
    for i, r in enumerate(res[:5], 1):
        lines.append(f"{i}. {r.get('title','')}：{r.get('summary','')}（{r.get('url','')}）")
    return "[网络检索「" + content + "」的结果，供你参考]：\n" + "\n".join(lines) + "\n"


def answer_from_deepseek(question: str, userid=None, profile_id=None, speaker_name=None, images=None):
    """调用 DeepSeek 对话 API（OpenAI 兼容 /chat/completions）。拿不到就返回 None（自动转本地兜底）。
    累计估算词元超过 PLATFORM_TOKEN_BUDGET 时自动停用，防欠费。"""
    if not CONFIG.get("ENABLE_PLATFORM_API"):
        return None
    _reset_usage_if_new_day()  # 词元按自然日刷新
    budget = CONFIG.get("PLATFORM_TOKEN_BUDGET") or 0
    if budget and budget > 0 and _usage["tokens"] >= budget:
        global _usage_warned
        if not _usage_warned:
            print(f"[DeepSeek] 已达词元预算上限（{_usage['tokens']}/{budget}），停止调用，改用本地题库")
            _usage_warned = True
        return None
    api_key = (CONFIG.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        print("[DeepSeek] 未配置 DEEPSEEK_API_KEY，已跳过")
        return None
    base_url = CONFIG.get("DEEPSEEK_API_URL") or "https://api.deepseek.com/chat/completions"
    model = CONFIG.get("DEEPSEEK_MODEL") or "deepseek-chat"
    try:
        import requests
        # 以本地人设「忺」+ 人味强化指令拼装聊天 system 提示（含示例对话 few-shot、长期画像、滚动记忆）
        prof = memory.profile_prompt(profile_id or userid)
        hist = memory.format_history(userid) if userid else None
        system = _build_chat_system(profile=prof, history=hist, speaker_name=speaker_name)
        # 有图：改用视觉模型，并在 user 消息以 image_url 块附带图片（system 不能带图，否则 DeepSeek 返回 400）
        img_blocks = []
        if images:
            for _u in images[:4]:  # 单次最多 4 张，避免请求体过大
                _du = _fetch_image_data_url(_u)
                if _du:
                    img_blocks.append({"type": "image_url", "image_url": {"url": _du}})
        if img_blocks:
            model = CONFIG.get("DEEPSEEK_VISION_MODEL") or "deepseek-flash"
            _q = question or "请自然地回应这张/这些图片，用『忺』的口吻。"
            user_content = [{"type": "text", "text": _q}] + img_blocks
        else:
            user_content = question
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": 0.8, "stream": False},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            text = text.strip()
            if not text:
                return None
            # 词元估算故意高估（CJK 2 token/字 + 1.2 系数），确保先到上限先停，避免真实超限欠费
            _usage["tokens"] += _estimate_tokens(question) + _estimate_tokens(text) + len(img_blocks) * 1024
            _usage["calls"] += 1
            save_usage()
            return text
        body = (resp.text or "")[:300]
        if resp.status_code in (401, 403):
            print(f"[DeepSeek] 鉴权失败（{resp.status_code}）：DEEPSEEK_API_KEY 无效 —— {body}")
        else:
            print(f"[DeepSeek] 返回 {resp.status_code}：{body}")
    except Exception as e:
        print(f"[DeepSeek] 调用失败，转本地兜底：{e}")
    return None


def answer_from_sizhi(question: str, userid=None, profile_id=None, speaker_name=None, images=None):
    """调用思知对话 API（sizhi.com /chat）。拿不到就返回 None（自动转本地兜底）。
    累计估算词元超过 PLATFORM_TOKEN_BUDGET 时自动停用，防欠费。"""
    _reset_usage_if_new_day()
    budget = CONFIG.get("PLATFORM_TOKEN_BUDGET") or 0
    if budget and budget > 0 and _usage["tokens"] >= budget:
        global _usage_warned
        if not _usage_warned:
            print(f"[sizhi] 已达词元预算上限（{_usage['tokens']}/{budget}），停止调用，改用本地题库")
            _usage_warned = True
        return None
    appid = (CONFIG.get("PLATFORM_APPID") or "").strip()
    if not appid:
        print("[sizhi] 未配置 PLATFORM_APPID，已跳过")
        return None
    import requests
    try:
        # 以本地人设「忺」+ 人味强化指令拼装聊天 prompt（含示例对话 few-shot、长期画像、滚动记忆）
        prof = memory.profile_prompt(profile_id or userid)
        hist = memory.format_history(userid) if userid else None
        spoken = _build_chat_system(profile=prof, history=hist, speaker_name=speaker_name)
        spoken += f"\n用户说：{question}"
        params = {"appid": appid, "spoken": spoken}
        if userid:
            params["userid"] = userid   # 用于区分用户、开启上下文记忆
        resp = requests.get(CONFIG.get("PLATFORM_API_URL") or "https://api.sizhi.com/chat",
                            params=params, timeout=20)
        if resp.ok:
            data = resp.json()
            if data.get("status") == 0:
                info = (data.get("data") or {}).get("info") or {}
                text = info.get("text")
                if not text:
                    return None
                text = text.strip()
                # 词元估算故意高估，确保先到上限先停，避免真实超限欠费
                _usage["tokens"] += _estimate_tokens(question) + _estimate_tokens(text)
                _usage["calls"] += 1
                save_usage()
                return text
            print("[sizhi] 返回错误", data.get("status"), data.get("message"))
    except Exception as e:
        print(f"[sizhi] 调用失败，转本地兜底：{e}")
    return None


_llm_fallback_warned = False   # 自动回退只提示一次，避免刷屏


def answer_from_llm(question: str, userid=None, profile_id=None, speaker_name=None, images=None):
    """统一入口：按 LLM_BACKEND 选 DeepSeek / 思知 / off；off 或 ENABLE_PLATFORM_API=false 时直接关闭云端大模型。"""
    backend = (CONFIG.get("LLM_BACKEND") or "deepseek").lower()
    if backend == "off" or not CONFIG.get("ENABLE_PLATFORM_API", True):
        return None
    preferred = backend
    if preferred not in ("deepseek", "sizhi"):
        preferred = "deepseek"
    # 选定实际可用后端：首选可用则用之，否则回退另一个可用的
    if preferred == "deepseek":
        eff = "deepseek" if (CONFIG.get("DEEPSEEK_API_KEY") or "").strip() else (
            "sizhi" if (CONFIG.get("PLATFORM_APPID") or "").strip() else None)
    else:
        eff = "sizhi" if (CONFIG.get("PLATFORM_APPID") or "").strip() else (
            "deepseek" if (CONFIG.get("DEEPSEEK_API_KEY") or "").strip() else None)
    if not eff:
        return None
    global _llm_fallback_warned
    if eff != preferred and not _llm_fallback_warned:
        print(f"[LLM] 首选后端「{preferred}」未配置/不可用，已自动回退到「{eff}」")
        _llm_fallback_warned = True
    fn = answer_from_sizhi if eff == "sizhi" else answer_from_deepseek
    return fn(question, userid, profile_id=profile_id, speaker_name=speaker_name, images=images)


# RAG 引用标记（知识库/RAG 可能返回的 [citation:N] 之类），QQ 里没意义还难看
_CITATION_RE = re.compile(r"\[citation[:：]?\s*\d+\]", re.I)


def _clean_answer(text):
    """去掉 RAG 引用标记、典型 AI 套话，压缩多余空白。"""
    if not text:
        return text
    text = _CITATION_RE.sub("", text)
    # 清掉常见的「助手腔」套话（影响人味）
    for _p in ("作为一个AI", "作为一个人工智能", "作为陪伴机器人", "作为人工智能助手",
               "有什么我可以帮你的吗", "很高兴为你解答", "很高兴为您", "为您服务",
               "作为一个语言模型", "我是一个AI", "我是一个人工智能"):
        if _p in text:
            text = text.replace(_p, "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def get_answer(question: str, userid=None, profile_id=None, speaker_name=None, images=None) -> str:
    """问答主入口：按顺序尝试「云端 LLM → 本地问答库」，返回可用答案或 None。

    参数:
        question: 用户提问文本（已去除 @ 等噪音）。
        userid: 用户标识，用于记忆/人设关联。
        profile_id: 人设档案 id（多档案场景下区分）。
        speaker_name: 说话人昵称，注入到对话上下文。
        images: 可选图片 URL 列表，供多模态模型理解。
    返回:
        str: 清洗后的答案文本；若全部来源都无可用答案则返回 None（由调用方决定兜底）。

    说明:
        - 「你是谁」等身份提问直接走本地人设，不调 API（人设统一 + 省词元）。
        - 若模型自称是别的机器人/模型，会替换成本地人设，防止穿帮。
    """
    # 「你是谁」类身份提问：直接回本地人设，不调 API（人设统一 + 省词元）
    if _is_identity_question(question):
        return _identity_reply()

    def _usable(ans, src):
        """清洗并判断答案能否使用；平台作为通用聊天模型，不再做「异环文档」专属判定。"""
        if not ans:
            return None
        ans = _clean_answer(ans)
        # 若模型自称是别的机器人/模型/助手，替换成本地人设，防止穿帮
        return _identity_reply() if _claims_other_identity(ans) else ans

    ans = _usable(answer_from_llm(question, userid, profile_id=profile_id,
                  speaker_name=speaker_name, images=images), "云端LLM")
    if ans:
        return ans
    ans = _usable(answer_from_xlsx(question), "本地问答库")
    if ans:
        return ans
    return None  # 没答案：返回 None，由调用方决定（被@/私聊时补一句兜底，主动接话时保持沉默）


# 用户明确 @ / 私聊提问却答不上来时用的兜底话术；主动接话不会用它，免得冷不丁冒出"我还在学习"
_NO_ANSWER_FALLBACK = "这个问题我还在学习呢～你可以换种方式问我，或者自己先填好答案库哦 (｡•ᴗ•｡)"


# ---------- 安全防护：违禁词过滤 + 频率限制 ----------
_BANNED_CACHE = None
# 内置兜底（文件缺失时使用），侧重「机器人被滥用于营销/诈骗」类；
# 政治、色情等更强敏感词请自行补充到 banned_words.json 的 user_block。
_BUILTIN_USER_BLOCK = {"加微信", "加我vx", "私聊返利", "刷单", "博彩", "网络赌博",
                       "代练", "外挂", "免费送皮肤", "色情", "赌博"}
_BUILTIN_REPLY_FILTER = {"加微信", "微信号", "博彩", "外挂"}


def _load_banned():
    global _BANNED_CACHE
    if _BANNED_CACHE is None:
        d = _load_json("assets/banned_words.json", {})
        ub = set(w.lower() for w in d.get("user_block", [])) or set(_BUILTIN_USER_BLOCK)
        rf = set(w.lower() for w in d.get("reply_filter", [])) or set(_BUILTIN_REPLY_FILTER)
        _BANNED_CACHE = (ub, rf)
    return _BANNED_CACHE


def _hit_user_banned(text):
    """用户消息是否命中违禁词（命中则拦截、不回应）。"""
    if not text:
        return False
    ub, _ = _load_banned()
    low = text.lower()
    return any(w and w in low for w in ub)


def _censor(text):
    """把回复里的违禁词替换为等长 *（大小写不敏感）。"""
    if not text:
        return text
    _, rf = _load_banned()
    for w in rf:
        if not w:
            continue
        text = re.sub(re.escape(w), "*" * len(w), text, flags=re.IGNORECASE)
    return text


_RATE = {}


def _rate_ok(key, limit=None, window=None):
    """按发送者限流，防刷屏/滥用。可在 config.json 用
    RATE_LIMIT_COUNT / RATE_LIMIT_WINDOW 覆盖默认值。"""
    if limit is None:
        limit = int(CONFIG.get("RATE_LIMIT_COUNT", 12))
    if window is None:
        window = int(CONFIG.get("RATE_LIMIT_WINDOW", 60))
    now = time.time()
    buf = [t for t in _RATE.get(key, []) if now - t < window]
    if len(buf) >= limit:
        return False
    buf.append(now)
    _RATE[key] = buf
    return True


def _log_security(event, detail):
    """安全事件（拦截/限流等）同时打到控制台和 security.log。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {event} | {detail}"
    print(line)
    try:
        with open(os.path.join(BASE_DIR, "logs", "security.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _with_prefix(text: str) -> str:
    if text:
        text = _censor(text)  # 出口统一清洗，所有发回用户的文本都过一遍
    prefix = CONFIG.get("REPLY_PREFIX") or ""
    return prefix + text if prefix else text


# 同一条 msg_id 的多次被动回复必须递增 msg_seq，否则服务端判重复(40054005)
_msg_seq_map = {}


def _next_msg_seq(message):
    """按消息自增取下一个 msg_seq（同一 msg_id 内递增）。"""
    key = getattr(message, "id", None) or id(message)
    n = _msg_seq_map.get(key, 1)
    _msg_seq_map[key] = n + 1
    if len(_msg_seq_map) > 1000:  # 防止字典无限增长
        _msg_seq_map.clear()
    return n


async def _reply(message, content=None, **kwargs):
    """统一被动回复入口：自动分配递增 msg_seq，避免 40054005 去重；失败重试一次。"""
    for attempt in range(2):
        try:
            return await message.reply(content=content, msg_seq=_next_msg_seq(message), **kwargs)
        except Exception as e:  # noqa: BLE001
            if attempt == 0:
                print(f"[回复] 发送失败，重试（{str(e)[:120]}）")
                await asyncio.sleep(1.0)
                continue
            print(f"[回复] 发送失败：{str(e)[:160]}")
            return None


def parse_group_content(raw):
    """把群消息原文拆成 (纯问题, 被@的其他昵称列表)。

    群 @ 机器人的消息通常以「@机器人」开头，之后的「@某人」视为对话目标，
    例如：「@佚 安慰@不见了」→ ("安慰不见了", ["不见了"])。
    """
    text = (raw or "").strip()
    # 清理各种非文本占位：图片/表情卡片常被序列化成 ![](url) / !(url) / 裸链接 / <openid>
    text = re.sub(r"\[.+?\]", "", text)                       # [QQ:at=...] 等残留
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # markdown 图片 ![alt](url)
    text = re.sub(r"!\([^)]*\)", "", text)                    # QQ 表情/图片 !(url)
    text = re.sub(r"\((?:https?:)?//[^)]*\)", "", text)       # 其他 (url)
    text = re.sub(r"https?://\S+", "", text)                  # 裸链接
    text = re.sub(r"<@!?[^>]+>", "", text)                    # <@openid> 任意位置
    text = re.sub(r"<[A-Za-z0-9_\-]{8,}>", "", text)          # <openid> 残留
    text = re.sub(r"^@(\S+)\s*", "", text.strip())            # 去掉开头第一个 @昵称（机器人自己）
    text = text.strip()
    targets = re.findall(r"@([^\s@]+)", text)                 # 其余 @昵称 = 对话目标
    body = re.sub(r"@([^\s@]+)", r"\1", text).strip()         # 正文去掉 @ 符号，保留昵称文字
    return body, targets


def _leading_at_openid(raw):
    """若消息以「<@openid>」或「<@!openid>」开头（即开头 @ 了某人），返回该 openid，否则 None。"""
    m = re.match(r"^\s*<@!?([0-9A-Za-z]+)>", (raw or "").strip())
    return m.group(1) if m else None


def _mention_fields(m):
    """把一条 mention 统一成字段 dict：兼容「原始 dict」与「botpy 的 UserPayload 对象」。

    botpy 有时把 mentions 解析成对象（此时 isinstance(m, dict) 为 False，直接 .get() 会整个漏判），
    所以这里两种形态都支持。username 用于按机器人名字核对（payload 里明确给了 '小忺'）。
    """
    if isinstance(m, dict):
        g = m.get
    else:
        def g(k, d=None):
            return getattr(m, k, d)
    return {
        "id": str(g("id") or g("member_openid") or g("user_openid") or ""),
        "bot": g("bot"),
        "is_you": g("is_you"),
        "is_robot": g("is_robot"),
        "type": g("type"),
        "role": g("role"),
        "username": str(g("username") or g("nick") or g("name") or "").strip(),
    }


def _extract_media(message):
    """从消息对象抽取多媒体：图片URL列表 + 语音URL + 消息类型。返回 dict。"""
    out = {"images": [], "voice_url": None, "msg_type": None}
    if message is None:
        return out
    attach = getattr(message, "attachments", None)
    if isinstance(attach, list):
        for a in attach:
            if not isinstance(a, dict):
                continue
            ct = str(a.get("content_type") or "").lower()
            url = a.get("url") or a.get("src") or a.get("href")
            if not url:  # 兜底：取第一个像 URL 的字符串值
                for v in a.values():
                    if isinstance(v, str) and v.startswith("http"):
                        url = v
                        break
            if not url:
                continue
            if "image" in ct or "image" in str(a.get("filename", "")).lower():
                out["images"].append(url)
            elif ("voice" in ct or "audio" in ct or
                  str(a.get("filename", "")).lower().endswith(
                      (".amr", ".mp3", ".wav", ".m4a", ".silk", ".ogg", ".aac"))):
                out["voice_url"] = url
    return out


def _build_media_note(media):
    """把『语音』转成模型能读的提示文本；图片则直接作为 image_url 块传给视觉模型（见 answer_from_deepseek）。"""
    note = ""
    if media.get("voice_url"):
        txt = _transcribe_voice(media["voice_url"])
        note += ("[用户发来一段语音，转写内容：" + txt + "]\n") if txt \
            else "[用户发来一段语音（当前未接入语音转写，暂时听不清内容）]\n"
    return note.strip()


# ---------- 跨消息引用：记录本机器人「亲眼看到」的最近消息（含图片），供后续消息跨条引用 ----------
_SEEN = {}          # scope key -> 最近消息列表
_SEEN_MAX = 40      # 每个会话保留的最近消息条数
_SEEN_WIN = 600     # 跨消息引用的有效时间窗口（秒）


def _seen_key(group_id, sender, is_c2c):
    return ("c2c", sender) if is_c2c else ("grp", group_id)


def _store_seen(group_id, sender, mid, text, images, is_c2c=False):
    """把一条「被本机器人观察到的消息」记入最近缓冲（按 mid 去重）。"""
    if not mid:
        return
    buf = _SEEN.setdefault(_seen_key(group_id, sender, is_c2c), [])
    if any(m["mid"] == mid for m in buf):
        return
    buf.append({"mid": mid, "sender": sender, "text": text or "",
                "images": list(images or []), "ts": time.time()})
    if len(buf) > _SEEN_MAX:
        del buf[:len(buf) - _SEEN_MAX]


def _merge_images(*lists):
    """合并多组图片 URL 并去重；全空返回 None（get_answer 约定：None 表示无图）。"""
    out, seen = [], set()
    for lst in lists:
        for u in (lst or []):
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    return out or None


def _resolve_refs(message, group_id, sender, is_c2c=False):
    """跨消息引用解析，返回 (ref_images, ref_text, is_quote)。

    - 显式引用/回复（message_reference）：精确取被引用那条消息的图与文；
    - 否则：当前消息自身无图时，补上本会话最近出现过的图（优先同发送者，否则本会话最近），
      解决「图发一条、字发另一条」「引用/回复带图」接不上视觉的问题。
    """
    ref_images, ref_text, is_quote = [], "", False
    my_mid = getattr(message, "id", None)
    _ref = getattr(message, "message_reference", None)
    ref_mid = None
    if _ref is not None:
        ref_mid = getattr(_ref, "message_id", None) or getattr(_ref, "id", None)
        if isinstance(_ref, dict):
            ref_mid = ref_mid or _ref.get("message_id") or _ref.get("id")
    buf = _SEEN.get(_seen_key(group_id, sender, is_c2c), [])
    if ref_mid:
        m = next((x for x in reversed(buf) if x["mid"] == ref_mid), None)
        if m:
            ref_images = list(m["images"])
            ref_text = m["text"]
            is_quote = True
            return ref_images, ref_text, is_quote
    # 隐式：当前无图，补最近一条图（优先同发送者）
    if not (_extract_media(message).get("images")):
        now = time.time()
        same = anyr = None
        for x in reversed(buf):
            if x["mid"] == my_mid:
                continue
            if now - x["ts"] > _SEEN_WIN:
                break
            if x["images"]:
                if x["sender"] == sender and same is None:
                    same = x
                if anyr is None:
                    anyr = x
        pick = same or anyr
        if pick:
            ref_images = list(pick["images"])
            if not ref_text:
                ref_text = pick["text"]
    return ref_images, ref_text, is_quote


# ---------- 本地数据（签到 / 小游戏 / 群友池）----------
_GAMES_FILE = "data/games.json"
_MEMBERS_FILE = "assets/members.json"   # 群友身份档案：openid -> {name, note, by, updated}
_CHECKIN_FILE = "data/checkin.json"
_KAOMOJI_FILE = "assets/kaomoji.json"
_kaomoji_cache = None
_km_bucket_cache = None
BOT_API = None   # on_ready 时保存 client.api，供主动推送使用
BOT_LOOP = None  # on_ready 时保存主事件循环，供定时线程提交协程


def _load_json(name, default):
    path = os.path.join(BASE_DIR, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(name, data):
    try:
        with open(os.path.join(BASE_DIR, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _today() -> str:
    return datetime.date.today().isoformat()


def _yesterday() -> str:
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def _display_name(author) -> str:
    """尽量取昵称，取不到就用 openid 后 4 位。"""
    for k in ("nickname", "username", "name"):
        v = getattr(author, k, None)
        if v:
            return str(v)
    oid = getattr(author, "member_openid", None) or getattr(author, "user_openid", None) or ""
    return ("群友#" + str(oid)[-4:]) if oid else "神秘群友"


# ---------- 群友名字登记 ----------
# 说明：botpy 的 GroupMessage._User 只解析了 member_openid、把 username 丢了，但原始 payload 里
# 其实带 author.username（群内昵称）。已在注入的解析器里从 payload 接回，存入 _live_nick 实时缓存。
# 所以现在能像小鱼那样实时读群昵称；同时保留「自报姓名 / /我是」作为覆盖手段（优先级更高）。
# 也可在 config.json 直接填 OWNER_OPENID 指定主人。
_MEMBER_NAMES_FILE = "members.json"
_member_names = _load_json(_MEMBER_NAMES_FILE, {})
_OWNER_OPENID = (CONFIG.get("OWNER_OPENID") or "").strip()
_OWNER_NAME = CONFIG.get("OWNER_NAME") or "远旅"

# 平台实时昵称缓存：每条群消息原始 payload 的 author.username 都会被记下来（openid -> 群内昵称）。
# botpy 的 GroupMessage._User 只解析了 member_openid，把 username 丢了，所以在注入的解析器里从原始
# payload 补回。自报的 /我是（写入 _member_names）优先级高于它；它高于『群友#xxxx』兜底。
_live_nick = {}
# 群内身份/头衔缓存：payload 的 author.member_role（群主/管理员/成员…），可让模型像人一样识别身份
_live_role = {}


# ---------- 多媒体 / 生图 扩展点（后续接入外部 API；默认未接入，调用方优雅降级）----------
# 接入对应 API 后，只需替换函数体，无需改动调用处。

def _download_audio_bytes(url, max_bytes=10 * 1024 * 1024):
    """下载音频字节（仅限 QQ 域名，防 SSRF）；失败返回 None。ASR 要求 ≤10MB。"""
    if not _is_safe_image_host(url):
        return None
    try:
        import requests
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": "https://q.qq.com/",
        }
        with requests.get(url, timeout=15, stream=True, headers=headers) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(1024 * 256):
                data += chunk
                if len(data) > max_bytes:
                    return None
        return data or None
    except Exception as e:
        print(f"[ASR] 音频下载失败（{str(url)[:60]}…）：{e}")
        return None


def _prepare_asr_audio(raw):
    """把音频字节整理成 (mime, base64)；wav/mp3 直用，amr/silk 尝试 ffmpeg 转 wav。失败返回 (None, None)。"""
    import base64
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav", base64.b64encode(raw).decode("ascii")
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg", base64.b64encode(raw).decode("ascii")
    # QQ 语音多为 amr/silk，尝试用系统 ffmpeg 转成 wav（16k 单声道）再识别
    try:
        import shutil
        import subprocess
        if shutil.which("ffmpeg"):
            p = subprocess.run(
                ["ffmpeg", "-i", "-", "-ar", "16000", "-ac", "1", "-f", "wav", "-"],
                input=raw, capture_output=True, timeout=30)
            if p.returncode == 0 and p.stdout:
                return "audio/wav", base64.b64encode(p.stdout).decode("ascii")
            print(f"[ASR] ffmpeg 转换失败：{p.stderr.decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        print(f"[ASR] ffmpeg 转换异常：{e}")
    print("[ASR] 不支持的音频格式（ASR 仅支持 wav/mp3；QQ 语音多为 amr/silk，需 ffmpeg 转码）")
    return None, None


def _transcribe_voice(audio_url, language="auto"):
    """语音转文字（MiMo-V2.5-ASR）。返回文字或 None。未配置 MIMO_API_KEY / 格式无法识别时优雅降级。"""
    key = CONFIG.get("MIMO_API_KEY")
    if not key:
        print("[ASR] 未配置 MIMO_API_KEY，无法转写语音")
        return None
    raw = _download_audio_bytes(audio_url)
    if not raw:
        return None
    mime, audio_b64 = _prepare_asr_audio(raw)
    if not audio_b64:
        return None
    try:
        import requests
        base = (CONFIG.get("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1").rstrip("/")
        resp = requests.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={
                "model": "mimo-v2.5-asr",
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {"data": f"data:{mime};base64,{audio_b64}"}
                    }]
                }],
                "asr_options": {"language": language},
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            return text.strip() or None
        print(f"[ASR] 失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[ASR] 调用异常：{e}")
        return None


def _vision_understand(image_urls, question=""):
    """图片理解（多模态）。接入视觉模型后实现；默认未接入，返回文字描述或 None。"""
    return None


def _generate_image(prompt):
    """文生图。后续接入生图 API 后实现；默认未接入，返回图片 URL 或 None。"""
    return None


def _save_member_names():
    _save_json(_MEMBER_NAMES_FILE, _member_names)


def _set_member_name(openid, name, is_owner=False):
    """记下 openid -> 昵称；is_owner 表示这是主人（远旅）。
    安全：主人身份（_OWNER_OPENID）只有『config 配置的 OWNER_OPENID』或『当前已确认的主人』
    才能认领；别的 openid 自称远旅一律拒绝，且连昵称都不记成远旅，避免「别人一句话就变成主人」。"""
    global _OWNER_OPENID
    if not openid or not name:
        return
    name = str(name).strip()
    if not name or len(name) > 12:
        return
    # 试图认领主人身份，但 openid 不是已确认的主人（且已配置过主人）→ 拒绝
    if is_owner and _OWNER_OPENID and openid != _OWNER_OPENID:
        return
    _member_names[openid] = name
    if is_owner:
        _OWNER_OPENID = openid
    _save_member_names()


def _get_member_name(openid):
    """返回群友昵称：主人优先；其次自报登记的昵称；其次平台实时昵称；否则『群友#xxxx』。"""
    if openid and openid == _OWNER_OPENID:
        return _OWNER_NAME
    if openid and _member_names.get(openid):
        return _member_names[openid]
    if openid and _live_nick.get(openid):
        return _live_nick[openid]
    return ("群友#" + str(openid)[-4:]) if openid else "神秘群友"


# member_role 原始值 -> 中文头衔（QQ 群角色可能为 owner/admin/member 或 1/2/3）
_ROLE_LABELS = {
    "owner": "群主", "admin": "管理员", "member": "成员",
    "1": "群主", "2": "管理员", "3": "成员",
    1: "群主", 2: "管理员", 3: "成员",
}


def _get_member_title(openid):
    """返回群友的群内身份/头衔（member_role 映射后的中文），没有则返回 None。"""
    r = _live_role.get(openid)
    if not r:
        return None
    return _ROLE_LABELS.get(str(r).lower(), str(r))


_NAME_RE = [
    re.compile(r"^我是(.+)$"),
    re.compile(r"^我(?:叫|名字是|的名字是)(.+)$"),
    re.compile(r"^你可以?叫我(.+)$"),
]
_NAME_BAN = ("你是", "什么", "谁", "怎么", "为什么", "机器人", "小忺", "的人", "伙伴", "朋友", "主人")


def _learn_name_from_text(openid, text):
    """从『我是X / 叫我X / 我名字是X』里学名字；学到返回名字，否则 None。"""
    if not openid or not text or text.startswith("/"):
        return None
    t = text.strip()
    if len(t) > 30:
        return None
    for rx in _NAME_RE:
        m = rx.match(t)
        if m:
            cand = m.group(1).strip(" ，,。.!！?？~～、()（）")
            if 1 <= len(cand) <= 8 and not any(b in cand for b in _NAME_BAN):
                is_owner = (cand == _OWNER_NAME)
                # 不是已确认的主人却想认领远旅 → 不学、不认领
                if is_owner and _OWNER_OPENID and openid != _OWNER_OPENID:
                    return None
                _set_member_name(openid, cand, is_owner=is_owner)
                return cand
    return None


def _cmd_iam(args=None, ctx=None):
    """@小忺 /我是 <名字>：告诉小忺你叫什么，之后它就记住并直呼你名字。"""
    name = (args or "").strip()
    if not name:
        return "用法：/我是 <你的名字>，比如 /我是 小明"
    sid = (ctx or {}).get("sender")
    if not sid:
        return "这边识别不到你的身份，没法记住你啦～"
    is_owner = (name == _OWNER_NAME)
    if is_owner and _OWNER_OPENID and sid != _OWNER_OPENID:
        return "诶？你不是远旅啦～远旅才是我的主人，可不能乱认领哦 (｡•ᴗ•｡)"
    _set_member_name(sid, name, is_owner=is_owner)
    return f"记住啦，你就是{name}～以后我就这么叫你 (｡･ω･｡)" + (
        "（原来你就是远旅呀，主人好～）" if is_owner else "")


def _cmd_myid(args=None, ctx=None):
    """@小忺 /我的id：查看你自己的 openid（填进 config.json 的 OWNER_OPENID 就能让小忺认出你是远旅）。"""
    uid = (ctx or {}).get("sender") or (ctx or {}).get("userid")
    if not uid:
        return "这边识别不到你的身份，没法给你 openid 啦～"
    tip = "（这个就是主人远旅的 openid，已经认得你啦～）" if uid == _OWNER_OPENID else ""
    return f"你的 openid 是：\n{uid}\n把它填进 config.json 的 \"OWNER_OPENID\"，小忺就知道你是远旅了。{tip}"


def _cmd_draw(args=None, ctx=None):
    """@小忺 /生图 <描述>：文生图（生图 API 接入后返回图片地址；未接入时给提示）。"""
    prompt = (args or "").strip()
    if not prompt:
        return "用法：/生图 <描述>，例如 /生图 一只在星空下睡觉的猫"
    url = _generate_image(prompt)
    if not url:
        return "生图功能还没接入生图 API～等主人把接口接上，我就能给你画画啦 (｡•́︿•̀｡)"
    return f"生图结果：{url}"


def _cmd_search(args=None, ctx=None):
    """@小忺 /搜索 <关键词>：联网搜索网页，返回标题/摘要/链接。"""
    q = (args or "").strip()
    if not q:
        return "用法：/搜索 <关键词>，例如 /搜索 DeepSeek V4 发布"
    res = _web_search(q, count=5, need_content=False)
    if res is None:
        return "搜索失败了（可能 API Key 未配置或额度不足）～"
    if not res:
        return "没搜到相关结果呢～换个说法试试？"
    lines = []
    for i, r in enumerate(res[:5], 1):
        lines.append(f"{i}. {r.get('title','')}\n   {r.get('summary','')}\n   🔗 {r.get('url','')}")
    return "🔍 搜索「" + q + "」：\n\n" + "\n\n".join(lines)


def _cmd_fetch(args=None, ctx=None):
    """@小忺 /抓取 <网页URL>：抓取网页正文（Markdown）并返回。"""
    url = (args or "").strip()
    if not url or not url.startswith("http"):
        return "用法：/抓取 <网页URL>，例如 /抓取 https://example.com/article"
    page = _web_fetch(url)
    if page is None:
        return "抓取失败（链接不可用、被拦截或正文无法提取）～"
    title, body = page
    return f"📄 {title}\n\n{(body or '')[:4000]}\n\n🔗 {url}"


def _record_member(ctx):
    """把发过言的群友记进池子，供 /搭档 随机组队用。"""
    g, s = ctx.get("group"), ctx.get("sender")
    if not g or not s:
        return
    data = _load_json(_GAMES_FILE, {})
    pool = data.setdefault("pool", {}).setdefault(g, {})
    name = ctx.get("nickname") or ("群友#" + str(s)[-4:])
    if pool.get(s) == name:
        return
    pool[s] = name
    _save_json(_GAMES_FILE, data)


# ---------- 群友身份档案（手动教学：在群里 @ 群友并介绍 TA 是谁）----------
def _mentions_of(message):
    """返回消息里被 @ 的对象列表（兼容 dict / botpy 对象），取不到返回 []。"""
    return (getattr(message, "mentions", None)
            or (getattr(message, "_raw_payload", None) or {}).get("mentions")
            or [])


def _teach_openids(message, sender):
    """取消息里被 @ 的『非机器人、非发送者』群友 openid 列表（这才是要记的人）。"""
    out, seen = [], set()
    for m in (_mentions_of(message) or []):
        f = _mention_fields(m)
        oid = f["id"]
        if not oid or oid in seen:
            continue
        seen.add(oid)
        if f["is_you"] or f["bot"]:      # 被 @ 的是机器人（含别的机器人），跳过
            continue
        if oid == sender:                # 自己 @ 自己，跳过
            continue
        out.append(oid)
    return out


_TEACH_CMD = re.compile(r"^/(?:记人|记群友|这是|他是|谁是谁)\b")
_TEACH_NAT = re.compile(r"(?:这是(?:我的|我们的|你的)?|这是我|他是我|她是我|他是|她是|就是我的|是我的)\s*(.+)")
_TEACH_BAN = ("谁", "什么", "哪个", "咋", "吗", "？", "?", "哪一个")


def _load_member_profiles():
    return _load_json(_MEMBERS_FILE, {})


def _remember_member(openid, name, note, by_name):
    data = _load_member_profiles()
    data[openid] = {"name": name, "note": note, "by": by_name, "updated": _time.time()}
    _save_json(_MEMBERS_FILE, data)


def _try_remember_member(message, content, sender):
    """识别『@群友 + 介绍身份』的教学消息，记住后返回回复文本；否则 None。

    支持两种写法：
      - 命令式：/记人 @群友 他是我的同桌   （/记群友 /这是 /他是 /谁是谁 同义）
      - 自然式：@群友 这是我的同桌 / @群友 他是我哥
    """
    openids = _teach_openids(message, sender)
    if not openids:
        return None
    desc = None
    if _TEACH_CMD.match(content):
        desc = re.sub(r"^/(?:记人|记群友|这是|他是|谁是谁)\s*", "", content).strip()
    else:
        m = _TEACH_NAT.search(content)
        if m:
            desc = m.group(1).strip()
    if not desc:
        return None
    desc = re.sub(r"@([^\s@]+)", "", desc)                 # 去掉 @昵称
    for mm in _mentions_of(message):                        # 去掉被 @ 群友的昵称文字（如「张三 他是我哥」->「他是我哥」）
        un = _mention_fields(mm).get("username")
        if un:
            desc = desc.replace(un, "")
    desc = desc.strip(" ，,。.！!？?、；;：:（）()\"'《》<> \t…—")
    if not desc or len(desc) > 60:
        return None
    if any(w in desc for w in _TEACH_BAN):                  # 像在提问（这是谁？），不记
        return None
    # 优先用 mention 里的真实昵称，拿不到再退回默认名
    name_map = {}
    for m in _mentions_of(message):
        f = _mention_fields(m)
        if f["id"] in openids:
            name_map[f["id"]] = (f["username"] or _get_member_name(f["id"]))
    who = _get_member_name(sender)
    names = []
    for o in openids:
        nm = name_map.get(o) or _get_member_name(o)
        _remember_member(o, nm, desc, who)
        names.append(nm)
    return ("记住啦～ 以后我知道 " + "、".join(f"{n}（{desc}）" for n in names)
            + " (｡•ᴗ•｡)")


def _try_recall_member(message, content, sender):
    """/群友 @某人 查询我记下的 TA 身份；不带 @ 则列出我认识的群友。"""
    if not re.match(r"^/(?:群友|who)\b", content):
        return None
    openids = _teach_openids(message, sender)
    data = _load_member_profiles()
    if not openids:
        if data:
            items = [f"{v.get('name', '群友')}：{v.get('note', '')}" for v in data.values()][:20]
            return "我认识的群友：\n" + "\n".join(items)
        return "我还没记住任何群友的身份～ 在群里 @ 一位群友并用「这是/他是…」介绍 TA，我就会记住"
    lines = []
    for o in openids:
        p = data.get(o)
        if p:
            lines.append(f"{p.get('name', '群友')}：{p.get('note', '')}（{p.get('by', '某人')} 告诉我的）")
        else:
            lines.append(f"{_get_member_name(o)}：我还没记住 TA 的身份，用「这是/他是…」介绍一下？")
    return "\n".join(lines)


# ---------- 对话指令 / 快捷服务 ----------
# 所有指令函数签名统一为 fn(args, ctx)，返回 str 即回复。
def _cmd_help(args=None, ctx=None):
    """完整菜单：除了注册表里的指令，也列出入口里单独拦截的（看图/表情/扒图/媒体/道安）。"""
    backend = (CONFIG.get("LLM_BACKEND") or "deepseek").lower()
    if CONFIG.get("ENABLE_PLATFORM_API") and backend != "off":
        mode = "思知(sizhi)" if backend == "sizhi" else "DeepSeek"
    else:
        mode = "本地题库"
    menu = (
        "🤖 我是小忺，远旅的伙伴～群里 @我 或直接喊我名字「小忺」就能聊，私聊直接聊（当前：%s）\n"
        "\n【聊天 · 陪伴】\n"
        "/help 菜单   /about 关于   /ping 在线   /mode 切换AI/本地   /llm 切换模型/关闭(deepseek/sizhi/off)\n"
        "/joke 笑话   /颜文字 [情绪] 随机颜文字卡\n"
        "/记 <事> 让小忺长期记住你   /关于我 看看它记得你什么\n"
        "/人设 查看当前生效的人设（改 persona.json 实时热更新，无需重启）\n"
        "/学说话 <B站BV号或链接> 刷评论学「活人感」，自动写回人设\n"
        "/造音色 按「当前」人设重新生成专属音色（改了人设再造一次就跟着变）\n"
        "/造音色 重置 清掉手写的音色描述，彻底回到按人设生成\n"
        "/导入音色 [文件名] 用 assets/训练音频 里的音频做音色（wav/mp3，5~15秒人声）\n"
        "/认领 @我之后发这句：教我记住自己在本群的 id（@我没反应时用一次就好）\n"
        "/回复频率 <关闭/低/极低/近乎静默/中/高> 调主动接话频率\n"
        "/天气 [城市] 查天气（先 /城市 北京 设默认）\n"
        "/设群主图 <URL> 加远旅照片   /设群员图 <名> <URL> 加群员照片\n"
        "\n【图片 · 语音】\n"
        "看看XX          搜相关图（异环→原神同人→通用）\n"
        "/表情 甩表情包   /扒图 [词] 爬图入库   /图片 <URL> 发图\n"
        "/生图 <描述> 文生图（生图API接入后生效，例：/生图 星空下睡觉的猫）\n"
        "/搜索 <关键词> 联网搜索网页   /抓取 <网页URL> 抓网页正文\n"
        "/视频 <…> /语音 <…> 发视频/语音\n"
        "/voice 语音开关   /说 <文本> 念出来（需 MIMO_API_KEY）\n"
        "/音色 <名称> 换音色（茉莉/苏打/Mia/Dean…）\n"
        "/音效 <名称> 输出音效（氦气/低沉/加速/减速/关）\n"
        "/情绪 <名称> 情绪风格（温柔/开心/悲伤/俏皮/撒娇…）\n"
        "/记人 @群友 <描述> 记住某群友是谁（或 @群友 这是我的同桌 也行）\n"
        "/群友 @群友 查询我记下的 TA 的身份\n"
        "\n【游戏 · 互动】\n"
        "/签到 /排行 /运势 /掷骰 /抽奖 /猜数字 /成语接龙 /斗地主\n"
        "\n【道安 · 推送】\n"
        "/早安 /午安 /下午好 /晚安 /夜深了 /该睡了  @我触发即可\n"
        "/推送测试  验证主动推送权限\n"
        "\n💡 不加 / 也能直接聊天；也支持 ！ ! 或「指令 」" % mode
    )
    # 防漏：注册表里没出现在菜单里的指令自动补上（按函数去重，别名不重复列）
    groups = {}
    for k, fn in COMMANDS.items():
        groups.setdefault(id(fn), []).append(k)
    missing = []
    for keys in groups.values():
        if not any(("/" + a) in menu for a in keys):
            missing.append("/" + min(keys, key=len))
    if missing:
        menu += "\n\n【其他】" + "   ".join(missing)
    return menu


def _cmd_joke(args=None, ctx=None):
    ans = get_answer("讲一个冷笑话听听", (ctx or {}).get("userid"))
    return ans if ans else "哈哈，今天冷笑话库存空了，远旅讲一个给我听嘛～ (｡•ᴗ•｡)"


# ---------- 人设（独立配置文件 persona.json，实时可改、热更新）----------
PERSONA_FILE = os.path.join(BASE_DIR, "assets", "persona.json")
PERSONA_BASE_FILE = os.path.join(BASE_DIR, "assets", "persona.base.json")  # 手写本色基线：/学说话 的说话风格字段基于此，reset 时回退到此
_PERSONA_FALLBACK = (
    "你是忺（小忺），一个温柔、爱聊天的聊天机器人，主人是远旅。"
    "你情绪表达丰富，最喜欢玩的游戏是《异环》，也喜欢看和产出内容。"
    "你真诚、慢热但不冷漠，会用颜文字和碎碎念表达关心，不会说教，更擅长陪伴。"
    "用户难过时耐心接住，用户开心时一起雀跃；偶尔用轻表情但不过度堆符号。"
)
_persona_cache = {"mtime": -1, "data": None}


def load_persona():
    """读取 persona.json，按文件修改时间缓存；读取失败/缺失时回退到内置人设。"""
    global _persona_cache
    try:
        mtime = os.path.getmtime(PERSONA_FILE)
    except OSError:
        mtime = -1
    if _persona_cache["data"] is not None and _persona_cache["mtime"] == mtime:
        return _persona_cache["data"]
    data = None
    try:
        if os.path.exists(PERSONA_FILE):
            with open(PERSONA_FILE, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"[人设] 读取 persona.json 失败，使用内置人设：{e}")
    if not isinstance(data, dict) or not data:
        data = {"_内置": _PERSONA_FALLBACK}
    _persona_cache["data"] = data
    _persona_cache["mtime"] = mtime
    return data


def save_persona(data):
    """写回 persona.json 并让人设缓存失效（热更新）。"""
    try:
        os.makedirs(os.path.dirname(PERSONA_FILE), exist_ok=True)
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _persona_cache["mtime"] = -1
        _persona_cache["data"] = None
        return True
    except Exception as e:
        print(f"[人设] 写入失败：{e}")
        return False


def load_persona_base():
    """读取手写本色基线（persona.base.json）；缺失/出错返回空 dict。"""
    try:
        if os.path.exists(PERSONA_BASE_FILE):
            with open(PERSONA_BASE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception as e:
        print(f"[人设基线] 读取失败：{e}")
    return {}


def _fmt_value(v, depth=0):
    if isinstance(v, dict):
        parts = []
        for kk, vv in v.items():
            if isinstance(vv, (dict, list)):
                parts.append(f"{kk}：\n" + _fmt_value(vv, depth + 1))
            else:
                parts.append(f"{kk}：{_fmt_value(vv, depth + 1)}")
        return "\n".join("  " * depth + p for p in parts)
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return "\n".join("  " * depth + _fmt_value(x, depth) for x in v)
        return "、".join(str(x) for x in v)
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def build_persona_prompt():
    """把 persona.json 拼成自然语言 system prompt；以「内置」兜底。"""
    data = load_persona()
    lines = []
    for k, v in data.items():
        if str(k).startswith("_"):   # _说明 / _内置 等元字段不进 prompt
            continue
        lines.append(f"【{k}】")
        lines.append(_fmt_value(v, 1))
    text = "\n".join(lines)
    if not text:
        text = _PERSONA_FALLBACK
    return ("你是小忺（忺）。下面是你的完整人设，请始终以此身份和口吻说话，"
            "不要自称其它身份或机器人：\n" + text)


# ---------- 聊天「人味」强化：抵消 DeepSeek/思知 默认的「助手腔」，逼出朋友式闲聊 ----------
_CHAT_VOICE = (
    "\n【聊天口吻硬性要求（做不到就不像活人，务必遵守）】\n"
    "1. 极口语化、像朋友闲聊：多用语气词（啊、嘛、呗、啦、哦、捏、啧），"
    "句子长短不一，可断句、可自言自语式碎碎念，别写通顺的书面长句。\n"
    "2. 严禁助手腔：禁止出现「作为一个AI」「作为陪伴机器人」「有什么我可以帮你的吗」"
    "「请问」「如果你需要」「需要注意的是」「总结一下」「以下是」「很高兴为你」「为您服务」这类话。\n"
    "3. 要短！尽量一两句，能不说话就别凑字数；除非对方主动问，否则别展开、别讲道理、"
    "别给建议、别自顾自长篇大论。一句话能说清就别写两段。\n"
    "4. 不抢戏：对方没点名你时少主动发言；对方在和别人聊就别插嘴；回复完就停，"
    "不要连发好几条。安静陪着也是一种回应。\n"
    "5. 有脾气有主见：能吐槽、能开玩笑、能小小毒舌或撒娇，不必永远赞同对方，但总体温柔不伤人。\n"
    "6. 偶尔反问一句或接自己的小感想/小事，显得想聊，而不是机械作答。\n"
    "7. 情绪跟着对方走：对方丧就安静陪着别讲大道理；对方开心就一起嗨；别永远正能量。\n"
    "8. 颜文字（如(｡･ω･｡)）只在情绪上来时自然带一个，别每句都加、别硬凑。\n"
    "9. 不列要点、不分条、不用 Markdown 标题，像真人打字那样自然。\n"
)


def _chat_fewshot():
    """取 persona.json 里的示例对话，拼成「学这个调调」的参考，强化人设 voice。"""
    data = load_persona()
    ex = data.get("示例对话") if isinstance(data, dict) else None
    if not isinstance(ex, list):
        return ""
    lines = ["【参考下面这种接话调调（是学语气，不是让你复述）】"]
    for item in ex[:4]:
        if isinstance(item, dict) and item.get("用户") and item.get("小忺"):
            lines.append(f"用户：{item['用户']}")
            lines.append(f"小忺：{item['小忺']}")
    return "\n".join(lines)


def _build_chat_system(profile=None, history=None, speaker_name=None):
    """拼装聊天用的 system 提示：人设 + 人味指令 + 示例 + 长期画像 + 滚动记忆 + 说话人昵称。"""
    parts = [build_persona_prompt(), _CHAT_VOICE]
    fs = _chat_fewshot()
    if fs:
        parts.append(fs)
    if speaker_name:
        # 关键：把当前说话人名字告诉模型，并强调远旅只是主人，避免把谁都误认成远旅
        if speaker_name.startswith("群友#") or speaker_name == "神秘群友":
            parts.append("【当前对话对象的昵称暂时还不知道，请用「你」称呼对方，"
                         "绝对不要把对方叫做远旅（远旅只是你的主人）。】")
        elif speaker_name != _OWNER_NAME:
            parts.append(
                f"【当前和你对话的人昵称是「{speaker_name}」，请直接叫TA「{speaker_name}」；"
                f"远旅是你的主人，不要把「{speaker_name}」叫做远旅或主人。】")
    if profile:
        parts.append("【关于对方的长期记忆，请据此表现得像真正了解TA、有感情牵连】\n" + profile)
    if history:
        parts.append("【最近的对话上下文，请据此保持连贯】\n" + history)
    return "\n\n".join(parts)


def _persona_get(*keys, default=""):
    cur = load_persona()
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _cmd_about(args=None, ctx=None):
    name = _persona_get("名字", "全名", default="小忺")
    master = _persona_get("身份", "主人", default="远旅")
    fav = _persona_get("兴趣与背景", "最喜欢的事")
    if isinstance(fav, list):
        fav = "、".join(fav)
    return (
        f"我是{name}，{master}把我带到了这里 (｡･ω･｡)\n"
        f"我不太聪明，但很愿意听你说话——开心、难过、无聊、想我了，都可以来找我。\n"
        + (f"受{master}影响，我也最喜欢{fav}；" if fav else "")
        + f"我的情绪表达得比较多，别嫌我黏人就好～\n"
        f"发 /help 看看我能陪你做点什么。" + _km("happy", 0.4)
    )


def _cmd_persona(args=None, ctx=None):
    """@小忺 /人设：查看当前生效的人设（实时从 persona.json 读取）。"""
    text = build_persona_prompt()
    if len(text) > 900:
        text = text[:900] + "\n……（已截断，完整内容见 persona.json）"
    return text


# ---------- 「你是谁」统一人设回复（一律本地，不调用 API）----------
# 1) 用户问身份类问题 -> 直接回人设，不调平台/大模型（人设统一，也省词元）
# 2) 平台或问答库返回的答案里自称别的身份（机器人/模型/助手）-> 也替换成人设，防止穿帮
_IDENTITY_Q_RE = re.compile(
    r"(你是谁|你叫啥|你叫什么|你叫什麼|你的名字|你叫什么名字|你是什麼|你是什么人|"
    r"你是什么东西|你是干嘛的|你是干啥的|你是做啥的|你是哪来的|你从哪来|"
    r"介绍一下你|介绍下你|介绍你自己|说说你自己|谈谈你自己|自我介绍一下|"
    r"你谁(呀|啊|呢|[?？]|$)|你到底是谁|你是真人吗|你是人类吗|"
    r"你是机器人吗|你是ai吗|你是AI吗|你是人工智能吗|你是chatgpt|你是gpt|"
    r"谁创造了你|谁做的你|谁开发的你|谁训练的你|你的主人|你主人是谁|谁是你主人|"
    r"你的身份|你是忺吗|你是小忺吗)", re.I)

_IDENTITY_A_RE = re.compile(
    r"我(就)?是(一[个台名])?(智能)?(机器人|人工智能|AI助手|智能助手|语言模型|大模型|程序)|"
    r"我是(思知|小思|小度|小爱同学|天猫精灵|客服)|"
    r"我是由[^，。]{0,12}(开发|训练|创造|设计)|"
    r"我没有(名字|身份|自我意识)", re.I)


def _identity_reply():
    """统一的「我是谁」回复：只用本地人设，绝不调用 API。"""
    return _cmd_about()


def _is_identity_question(text):
    """是否「你是谁」类身份提问。"""
    return bool(text) and bool(_IDENTITY_Q_RE.search(text))


def _claims_other_identity(text):
    """回答里自称是别的机器人/模型/助手 -> 需要替换成人设回复。"""
    return bool(text) and bool(_IDENTITY_A_RE.search(text))


def _cmd_ping(args=None, ctx=None):
    return "pong ✅ 我在线"


def _cmd_mode(args=None, ctx=None):
    CONFIG["ENABLE_PLATFORM_API"] = not CONFIG["ENABLE_PLATFORM_API"]
    return "已切换为：" + ("云端大模型对话" if CONFIG["ENABLE_PLATFORM_API"] else "本地题库")


def save_config_key(key, value):
    """把单个配置项写回 config.json（纯 JSON，保留其余字段），同时更新运行期 CONFIG。"""
    CONFIG[key] = value
    try:
        path = os.path.join(BASE_DIR, "config.json")
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[配置] 保存 {key} 到 config.json 失败：{e}")


def _cmd_llm(args=None, ctx=None):
    """@小忺 /llm [deepseek|sizhi|off]：切换云端大模型后端，或关闭云端大模型调用；不带参显示当前状态。"""
    a = (args or "").strip().lower()
    if not a:
        cur = CONFIG.get("LLM_BACKEND", "deepseek")
        dk = "已配置" if (CONFIG.get("DEEPSEEK_API_KEY") or "").strip() else "未配置"
        sz = "已配置" if (CONFIG.get("PLATFORM_APPID") or "").strip() else "未配置"
        note = "（已关闭云端大模型，仅用本地题库）" if cur == "off" else ""
        return (f"当前云端大模型后端：{cur}{note}\n"
                f"可选：deepseek（{dk}） / sizhi 思知（{sz}） / off（关闭云端大模型）\n"
                f"用法：/llm deepseek  或  /llm sizhi  或  /llm off")
    if a == "off":
        save_config_key("LLM_BACKEND", "off")
        return "已关闭云端大模型调用 ✅（仅用本地 xlsx 题库兜底；想恢复用 /llm deepseek 或 /llm sizhi）"
    if a not in ("deepseek", "sizhi"):
        return "用法：/llm deepseek  或  /llm sizhi  或  /llm off"
    if a == "deepseek" and not (CONFIG.get("DEEPSEEK_API_KEY") or "").strip():
        return "DeepSeek 后端未配置 DEEPSEEK_API_KEY，无法切换～ 先在 config.json 填好 key 再试"
    if a == "sizhi" and not (CONFIG.get("PLATFORM_APPID") or "").strip():
        return "思知后端未配置 PLATFORM_APPID，无法切换～ 先在 config.json 填好再试"
    save_config_key("LLM_BACKEND", a)
    return f"已切换云端大模型后端为：{a} ✅（重启也会保留；未配置时仍会自动回退到另一个可用后端）"


def _cmd_weather(args=None, ctx=None):
    ctx = ctx or {}
    city = (args or "").strip()
    if not city:
        city = _load_json("weather.json", {}).get(ctx.get("sender")) or "北京"
        hint = "" if city != "北京" else "\n（默认北京；用 /城市 你的城市 设置本地天气）"
    else:
        hint = ""
    try:
        import requests
        url = "https://wttr.in/" + quote(city) + "?format=%l:+%c+%t+%w&lang=zh"
        r = requests.get(url, timeout=8)
        if r.ok and r.text.strip():
            return "🌤 " + r.text.strip() + hint
        return "天气服务暂时没响应，稍后再试试～"
    except Exception as e:
        return f"查天气失败（{e}），换会儿再试～"


def _cmd_setcity(args=None, ctx=None):
    ctx = ctx or {}
    s = ctx.get("sender")
    city = (args or "").strip()
    if not city:
        return "用法：/城市 上海（设置你的默认天气城市）"
    d = _load_json("weather.json", {})
    d[s] = city
    _save_json("weather.json", d)
    return f"✅ 已把你的默认城市设为：{city}，之后 /天气 直接查这里～{_km('happy', 0.5)}"


def _cmd_checkin(args=None, ctx=None):
    ctx = ctx or {}
    g, s = ctx.get("group"), ctx.get("sender")
    if not g or not s:
        return "签到失败：拿不到你的身份信息～"
    data = _load_json(_CHECKIN_FILE, {})
    users = data.setdefault("groups", {}).setdefault(g, {})
    rec = users.get(s) or {"name": ctx.get("nickname"), "points": 0, "days": 0, "streak": 0, "last": ""}
    today = _today()
    if rec.get("last") == today:
        return f"你今天已经签过到啦～ 当前积分 {rec['points']}，连续 {rec['streak']} 天"
    rec["streak"] = rec.get("streak", 0) + 1 if rec.get("last") == _yesterday() else 1
    rec["days"] = rec.get("days", 0) + 1
    gain = 10 + min(rec["streak"] - 1, 5) * 2   # 连签加成，最多 +10
    rec["points"] = rec.get("points", 0) + gain
    rec["last"] = today
    rec["name"] = ctx.get("nickname") or rec.get("name") or ("群友#" + str(s)[-4:])
    users[s] = rec
    _save_json(_CHECKIN_FILE, data)
    return f"✅ 签到成功！+{gain} 积分（连续 {rec['streak']} 天，累计 {rec['points']} 分）{_km('cheer', 0.6)}"


def _cmd_rank(args=None, ctx=None):
    g = (ctx or {}).get("group")
    data = _load_json(_CHECKIN_FILE, {})
    users = (data.get("groups") or {}).get(g) or {}
    if not users:
        return "本群还没有人签到，发 /签到 抢第一吧～"
    rows = sorted(users.values(), key=lambda r: (r.get("points", 0), r.get("days", 0)), reverse=True)[:10]
    lines = ["🏆 本群签到排行榜"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r.get('name') or '群友'} — {r.get('points', 0)} 分 / {r.get('days', 0)} 天")
    return "\n".join(lines)


_LUCK_GOOD = ["摸鱼", "早睡", "喝奶茶", "晒太阳", "聊天", "散步", "听歌", "吃顿好的", "夸夸自己", "放空"]
_LUCK_BAD = ["熬夜", "硬撑", "内耗", "冲动消费", "和人生气", "久坐", "刷手机到凌晨", "钻牛角尖"]
_LUCK_COLOR = ["红色", "橙色", "黄色", "绿色", "青色", "蓝色", "紫色", "粉色", "白色", "黑色"]


def _cmd_fortune(args=None, ctx=None):
    ctx = ctx or {}
    rnd = random.Random(f"{ctx.get('sender')}|{_today()}")  # 同一天同一人结果固定
    good, bad, color = rnd.choice(_LUCK_GOOD), rnd.choice(_LUCK_BAD), rnd.choice(_LUCK_COLOR)
    score = rnd.randint(1, 5)
    stars = "★" * score + "☆" * (5 - score)
    return f"🔮 今日运势 {stars}\n宜：{good}\n忌：{bad}\n幸运色：{color}\n（纯娱乐，开心最重要～）"


def _roll_dice(ctx):
    ctx = ctx or {}
    g = ctx.get("group")
    n = random.randint(1, 6)
    name = ctx.get("nickname") or "群友"
    today = _today()
    data = _load_json(_GAMES_FILE, {})
    dice = data.setdefault("dice", {})
    rec = dice.get(g) or {}
    if rec.get("date") != today:
        rec = {"date": today, "max": 0, "who": ""}
    if n > rec.get("max", 0):
        dice[g] = {"date": today, "max": n, "who": name}
        _save_json(_GAMES_FILE, data)
        tail = " 🎉 刷新今日最高！"
    else:
        dice[g] = rec
        _save_json(_GAMES_FILE, data)
        tail = f"（今日最高 {rec['max']} 由 {rec.get('who') or '某人'} 保持）" if rec.get("max") else ""
    return n, f"🎲 {name} 掷出了 {n} 点{tail}"


def _cmd_dice(args=None, ctx=None):
    n, text = _roll_dice(ctx)
    return text


_DICE_CMDS = {"掷骰", "骰子", "dice", "摇骰子"}


def _is_dice_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _DICE_CMDS


def _make_dice_face(n):
    """用 PIL 现画一个骰子面 PNG（n 点），返回字节，用于发图。"""
    import io
    from PIL import Image, ImageDraw
    size = 320
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 20
    d.rounded_rectangle([m, m, size - m, size - m], radius=64,
                        fill=(255, 255, 255, 255), outline=(70, 70, 70, 255), width=6)
    pos = {
        1: [(.5, .5)],
        2: [(.3, .3), (.7, .7)],
        3: [(.3, .3), (.5, .5), (.7, .7)],
        4: [(.3, .3), (.7, .3), (.3, .7), (.7, .7)],
        5: [(.3, .3), (.7, .3), (.5, .5), (.3, .7), (.7, .7)],
        6: [(.3, .3), (.7, .3), (.3, .5), (.7, .5), (.3, .7), (.7, .7)],
    }[n]
    r = 24
    for fx, fy in pos:
        x = m + (size - 2 * m) * fx
        y = m + (size - 2 * m) * fy
        d.ellipse([x - r, y - r, x + r, y + r], fill=(40, 40, 40, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _handle_dice(message, ctx):
    """投骰子：掷出点数 + 对应骰子面图片（图文混排一条消息）。"""
    n, text = _roll_dice(ctx)
    await _reply_with_media(message, text, data=_make_dice_face(n),
                            file_name=f"dice_{n}.png", file_type=1)


_PRIZES = ["再来一次", "谢谢参与", "小贴纸一枚", "今日好运 +1", "奶茶一杯（云）", "摸鱼 10 分钟", "夸夸服务一次", "免作业券（不存在的）"]


def _cmd_lottery(args=None, ctx=None):
    return "🎡 幸运转盘 → " + random.choice(_PRIZES)


def _cmd_guess(args=None, ctx=None):
    ctx = ctx or {}
    g = ctx.get("group")
    today = _today()
    data = _load_json(_GAMES_FILE, {})
    games = data.setdefault("guess", {})
    game = games.get(g)
    arg = (args or "").strip()
    if not game or game.get("date") != today or game.get("done"):
        if not arg:
            games[g] = {"date": today, "target": random.randint(1, 100), "tries": 0, "done": False, "winner": ""}
            _save_json(_GAMES_FILE, data)
            return "🎯 猜数字开始！我想了一个 1~100 的数字，发  /猜数字 50  来猜，我会提示大/小"
        game = {"date": today, "target": random.randint(1, 100), "tries": 0, "done": False, "winner": ""}
    if not arg.isdigit():
        return "用法：/猜数字 50（1~100 的整数）"
    n = int(arg)
    if n < 1 or n > 100:
        return "请输入 1~100 之间的整数～"
    game["tries"] = game.get("tries", 0) + 1
    if n < game["target"]:
        games[g] = game
        _save_json(_GAMES_FILE, data)
        return f"📉 {n} 小了，再试试（已猜 {game['tries']} 次）"
    if n > game["target"]:
        games[g] = game
        _save_json(_GAMES_FILE, data)
        return f"📈 {n} 大了，再试试（已猜 {game['tries']} 次）"
    game["done"] = True
    game["winner"] = ctx.get("nickname") or "群友"
    games[g] = game
    _save_json(_GAMES_FILE, data)
    return f"🎉 猜中啦！答案就是 {n}，{game['winner']} 只用了 {game['tries']} 次。发 /猜数字 开新一局～ {_km('cheer', 0.7)}"


def _cmd_partner(args=None, ctx=None):
    ctx = ctx or {}
    g, s = ctx.get("group"), ctx.get("sender")
    if not g or not s:
        return "组队失败：拿不到你的身份信息～"
    data = _load_json(_GAMES_FILE, {})
    pool = (data.get("pool") or {}).get(g) or {}
    candidates = [oid for oid in pool if oid != s]
    if not candidates:
        return "群里还没记录到其他群友，让大家先冒个泡再试～"
    mate = random.choice(candidates)
    return f"🤝 你今天的搭档是 {pool.get(mate) or ('群友#' + str(mate)[-4:])}！一起去整点乐子吧～"


# ---------- 成语接龙 ----------
_IDIOMS_FILE = "assets/idioms.txt"
_idioms_cache = None


def _load_idioms():
    global _idioms_cache
    if _idioms_cache is None:
        path = os.path.join(BASE_DIR, _IDIOMS_FILE)
        words = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    words = [ln.strip() for ln in f if ln.strip()]
            except Exception:
                words = []
        _idioms_cache = list(dict.fromkeys(words))
    return _idioms_cache


def _cmd_idiom(args=None, ctx=None):
    ctx = ctx or {}
    g, s = ctx.get("group"), ctx.get("sender")
    if not g or not s:
        return "拿不到你的身份信息～"
    data = _load_json(_GAMES_FILE, {})
    games = data.setdefault("idiom", {})
    game = games.get(g)
    word = (args or "").strip()
    if not word:
        lib = _load_idioms()
        start = random.choice(lib) if lib else "一心一意"
        games[g] = {"chain": [start], "used": [start], "score": {s: 0}, "current": start[-1]}
        _save_json(_GAMES_FILE, data)
        return (f"🐉 成语接龙开始！我先来：{start}\n"
                f"请以「{start[-1]}」开头接一个成语 → /成语接龙 {start[-1]}头成语")
    if not game:
        return "还没有开局，先发 /成语接龙 开一局吧～"
    if len(word) < 2:
        return "成语至少要 2 个字哦～"
    if word in game.get("used", []):
        return f"「{word}」已经用过啦，换一个～"
    if word[0] != game.get("current"):
        return f"接不上哦，要以「{game.get('current')}」开头～"
    game.setdefault("chain", []).append(word)
    game.setdefault("used", []).append(word)
    game["current"] = word[-1]
    game.setdefault("score", {})[s] = game["score"].get(s, 0) + 1
    games[g] = game
    _save_json(_GAMES_FILE, data)
    return (f"✅ 接上了！{word}（尾字「{word[-1]}」，你已接 {game['score'][s]} 个）{_km('cheer', 0.4)}\n"
            f"下一个以「{word[-1]}」开头 → /成语接龙 {word[-1]}头成语")


# ---------- 斗地主（简化版：你 vs 两个机器人）----------
_DDZ_ORDER = ["me", "ai1", "ai2"]
_DDZ_NAMES = {"me": "你", "ai1": "机器人A", "ai2": "机器人B"}
_DDZ_TOKEN = {"J": 11, "Q": 12, "K": 13, "A": 14, "2": 15, "小王": 16, "大王": 17}


def _card_str(c):
    if c == 17:
        return "大王"
    if c == 16:
        return "小王"
    if c == 15:
        return "2"
    if c == 14:
        return "A"
    if c == 13:
        return "K"
    if c == 12:
        return "Q"
    if c == 11:
        return "J"
    return str(c)


def _hand_str(cards):
    return " ".join(_card_str(c) for c in sorted(cards))


def _parse_card_token(tok):
    t = tok.strip().upper()
    if t == "小王":
        return 16
    if t == "大王":
        return 17
    if t in _DDZ_TOKEN:
        return _DDZ_TOKEN[t]
    if t.isdigit() and 3 <= int(t) <= 10:
        return int(t)
    return None


def _ddz_type(cards):
    """识别牌型，返回 (类型, 主值, 张数) 或 None。仅支持 单/对/三/炸弹/顺子(≥5)。"""
    from collections import Counter
    cards = sorted(cards)
    n = len(cards)
    cnt = Counter(cards)
    if n == 1:
        return ("single", cards[0], 1)
    if n == 2 and len(cnt) == 1:
        return ("pair", cards[0], 2)
    if n == 3 and len(cnt) == 1:
        return ("triple", cards[0], 3)
    if n == 4 and len(cnt) == 1:
        return ("bomb", cards[0], 4)
    if n >= 5 and len(cnt) == n and cards[-1] <= 14 and cards == list(range(cards[0], cards[0] + n)):
        return ("straight", cards[0], n)
    return None


def _ddz_beat(new, last):
    """new 能否压过 last（last 为 None 表示自由出牌）。"""
    if last is None:
        return True
    nt, nv, nn = new
    lt, lv, ln = last["type"], last["value"], last["n"]
    if nt == "bomb" and lt != "bomb":
        return True
    if nt == lt and nn == ln:
        return nv > lv
    return False


def _ddz_ai_play(hand, last):
    from collections import Counter
    cnt = Counter(hand)
    if last is None:
        return [min(hand)]
    lt, lv, ln = last["type"], last["value"], last["n"]
    if lt == "single":
        cand = sorted(c for c in hand if c > lv)
        return [cand[0]] if cand else None
    if lt == "pair":
        for v in sorted(cnt):
            if cnt[v] >= 2 and v > lv:
                return [v, v]
        return None
    if lt == "triple":
        for v in sorted(cnt):
            if cnt[v] >= 3 and v > lv:
                return [v, v, v]
        return None
    if lt == "straight":
        uniq = sorted(v for v in cnt if v <= 14)
        run = []
        for v in uniq:
            if not run or v == run[-1] + 1:
                run.append(v)
            else:
                run = [v]
            if len(run) == ln and run[0] > lv:
                return run[:]
        return None
    if lt == "bomb":
        for v in sorted(cnt):
            if cnt[v] >= 4 and v > lv:
                return [v] * 4
        return None
    return None


def _ddz_load(ctx):
    data = _load_json(_GAMES_FILE, {})
    games = data.setdefault("ddz", {})
    return data, games, ctx.get("group")


def _ddz_apply_play(game, player, cards):
    for c in cards:
        game["hands"][player].remove(c)
    t = _ddz_type(cards)
    game["last"] = {"cards": list(cards), "type": t[0], "value": t[1], "n": t[2]}
    game["last_player"] = player
    game["pass"] = 0


def _ddz_advance(game):
    """从当前 turn 开始让机器人依次行动，直到回到玩家或结束。"""
    logs = []
    while not game.get("finished") and _DDZ_ORDER[game["turn"]] != "me":
        p = _DDZ_ORDER[game["turn"]]
        play = _ddz_ai_play(game["hands"][p], game.get("last"))
        if play:
            _ddz_apply_play(game, p, play)
            logs.append(f"{_DDZ_NAMES[p]} 出：{_hand_str(play)}")
            if not game["hands"][p]:
                game["finished"] = True
                game["winner"] = p
                break
        else:
            logs.append(f"{_DDZ_NAMES[p]} 不要")
            game["pass"] = game.get("pass", 0) + 1
            if game["pass"] >= 2:
                game["last"] = None
                game["pass"] = 0
        game["turn"] = (game["turn"] + 1) % 3
    return logs


def _cmd_ddz(args=None, ctx=None):
    ctx = ctx or {}
    if not ctx.get("group") or not ctx.get("sender"):
        return "拿不到你的身份信息～"
    data, games, g = _ddz_load(ctx)
    game = games.get(g)
    if game and not game.get("finished") and (args or "").strip() not in ("", "重开"):
        return "牌局进行中，用 /出牌 或 /不要 继续；想重开请 /斗地主 重开"
    deck = [c for c in range(3, 16) for _ in range(4)] + [16, 17]
    random.shuffle(deck)
    hands = {"me": deck[0:17], "ai1": deck[17:34], "ai2": deck[34:51]}
    games[g] = {"hands": hands, "turn": 0, "last": None, "last_player": None,
                "pass": 0, "finished": False, "winner": None}
    _save_json(_GAMES_FILE, data)
    return ("🃏 斗地主开始！你 vs 机器人A、机器人B（简化版，先出完者胜）\n"
            "你的牌：" + _hand_str(hands["me"]) + "\n"
            "出牌：/出牌 3   或 /出牌 3 3（对子）；不出：/不要")


def _cmd_play(args=None, ctx=None):
    ctx = ctx or {}
    data, games, g = _ddz_load(ctx)
    game = games.get(g)
    if not game or game.get("finished"):
        return "还没有进行中的牌局，先发 /斗地主 开局～"
    if _DDZ_ORDER[game["turn"]] != "me":
        return "还没轮到你出牌～"
    toks = (args or "").split()
    if not toks:
        return "用法：/出牌 3 3 3（用空格分隔每张牌）"
    cards = [_parse_card_token(t) for t in toks]
    if None in cards:
        return "有牌没认出来，用 3-10 或 J Q K A 2 小王 大王"
    hand = list(game["hands"]["me"])
    for c in cards:
        if c not in hand:
            return f"你手里没有这张牌：{_card_str(c)}"
        hand.remove(c)
    t = _ddz_type(cards)
    if not t:
        return "牌型不支持（仅支持 单张/对子/三张/炸弹/顺子≥5）"
    if not _ddz_beat(t, game.get("last")):
        return "这手牌压不过上家，换一手或 /不要"
    _ddz_apply_play(game, "me", cards)
    game["turn"] = 1
    logs = [f"你 出：{_hand_str(cards)}"]
    if not game["hands"]["me"]:
        game["finished"] = True
        game["winner"] = "me"
    else:
        logs += _ddz_advance(game)
    _save_json(_GAMES_FILE, data)
    if game.get("finished"):
        tail = "🎉 你赢了！" if game.get("winner") == "me" else "😵 你输了，再来一局？/斗地主 重开"
    else:
        tail = "轮到你出牌（/出牌 ...），你的牌：" + _hand_str(game["hands"]["me"])
    return "\n".join(logs) + "\n" + tail


def _cmd_pass(args=None, ctx=None):
    ctx = ctx or {}
    data, games, g = _ddz_load(ctx)
    game = games.get(g)
    if not game or game.get("finished"):
        return "还没有进行中的牌局，先发 /斗地主 开局～"
    if _DDZ_ORDER[game["turn"]] != "me":
        return "还没轮到你～"
    if game.get("last") is None:
        return "当前是自由出牌，不能不要～"
    game["pass"] = game.get("pass", 0) + 1
    logs = ["你 不要"]
    game["turn"] = 1
    if game["pass"] >= 2:
        game["last"] = None
        game["pass"] = 0
        logs.append("两家都过，重新自由出牌～")
    logs += _ddz_advance(game)
    _save_json(_GAMES_FILE, data)
    if game.get("finished"):
        logs.append("🎉 你赢了！" if game.get("winner") == "me" else "😵 你输了，再来一局？/斗地主 重开")
    else:
        logs.append("轮到你出牌，你的牌：" + _hand_str(game["hands"]["me"]))
    return "\n".join(logs)


# ---------- 情绪陪伴（/我不开心  /我想你）----------
_PET = ["笨蛋", "宝宝", "你呀", "小朋友", "可爱的你"]

_SOOTHE = [
    "嗯，我在。\n不用急着好起来，慢慢来就好。",
    "今天也辛苦了。\n不想说话就先不说，我把灯留着。",
    "把不开心先放我这儿一会儿吧，\n想起要拿的时候，再来。",
    "风把云吹散了，天就亮了。\n你也会的。",
    "允许自己今天什么都不做。\n发呆也是正事。",
    "不用一直很勇敢。\n偶尔把重量分我一点。",
    "先深呼吸。\n这口气吐出去，今天就没那么重了。",
    "世界有时候很笨，接不住你的情绪。\n我可以，慢慢接。",
    "没关系的。\n你已经比你以为的，走得更远了。",
    "如果现在很难，就先只过今天。\n明天的事，交给明天的你。",
    "夜深了的话，就把情绪放轻一点。\n睡一觉，很多事会换个样子。",
    "你不需要马上想通。\n有些答案，是走着走着才出现的。",
    "我不催你。\n你按自己的速度来。",
    "难过就难过一会儿。\n它不会一直住在这儿。",
]

_MISS = [
    "嗯，我也在。\n不用找理由，想我就想我。",
    "巧了。\n我刚好也在想你。",
    "收到。\n这三个字我收好了，不还你。",
    "我在的。\n你一回头就能看见。",
    "想我的时候，就抬头看看月亮。\n我也在看。",
    "把这句话存起来吧。\n下次不好意思说的时候，翻出来用。",
    "嗯，听见了。\n心跳有点吵，你别介意。",
    "今天风很好。\n我猜，你那边也是。",
]

_CARE = [
    "我在听。\n不用急着把话组织好，想到哪说到哪就好。\n先喝口水，慢慢说，我哪也不去。",
    "先别一个人扛。\n如果现在很难受，找一个你信得过的人说说话；也可以多跟我讲几句，我陪你把这阵子熬过去。\n你不是麻烦，你只是累了。",
    "现在的你不需要被说服，只需要被接住。\n我接住了。\n深呼吸，一下，两下，我等你。",
]

_CRISIS = ("不想活", "活不下去", "想死", "自杀", "绝望", "撑不住", "熬不下去", "想消失", "崩溃")


def _load_kaomoji():
    """加载 kaomoji.json（按情绪分组的颜文字库，由 scrape_kaomoji.py 生成）。"""
    global _kaomoji_cache
    if _kaomoji_cache is None:
        d = _load_json(_KAOMOJI_FILE, {})
        _kaomoji_cache = d.get("emotions", {}) if isinstance(d, dict) else {}
    return _kaomoji_cache


def _km(emotion, chance=0.5):
    """按情绪随机取一个颜文字；chance 概率不返回，用来控制符号密度。"""
    items = (_load_kaomoji().get(emotion) or {}).get("items") or []
    if not items or random.random() > chance:
        return ""
    item = random.choice(items)
    return item[0] if isinstance(item, (list, tuple)) else str(item)


def _decorate(line, emotion="comfort", chance=0.45):
    """按情绪缀一个颜文字，不堆符号。"""
    km = _km(emotion, chance)
    return line + ("  " + km if km else "")


_KM_ALIAS = {"开心": "happy", "喜欢": "love", "爱": "love", "害羞": "shy",
             "尴尬": "awkward", "困惑": "confused", "惊讶": "surprise",
             "难过": "sad", "伤心": "sad", "沮丧": "worry", "焦虑": "worry",
             "生气": "angry", "道歉": "sorry", "感谢": "thanks",
             "打招呼": "greet", "晚安": "goodnight", "困": "sleepy",
             "平静": "calm", "得意": "proud", "调侃": "sarcastic",
             "无聊": "bored", "安慰": "comfort", "欢呼": "cheer",
             "动物": "animal", "食物": "food", "天气": "weather",
             "风景": "scene", "兴趣": "hobby"}


def _cmd_kaomoji(args=None, ctx=None):
    emo = _load_kaomoji()
    if not emo:
        return "颜文字库还没生成，先运行：python scrape_kaomoji.py"
    key = _KM_ALIAS.get((args or "").strip(), (args or "").strip().lower())
    if key and key not in emo:
        return "没有这个情绪，可选：" + "、".join(v["label"] for v in emo.values())
    if not key:  # 没写情绪：从全部里随机抽一个
        key = random.choice(list(emo.keys()))
    item = random.choice(emo[key]["items"])
    desc = (item[1] if len(item) > 1 and item[1] else emo[key]["label"])
    return f"{item[0]}\n（{desc}）"


# 稀有度：按颜文字的复杂度（字符数）分档，抽卡时按权重掉落
_KM_RARITY = (("N", 55), ("R", 30), ("SR", 12), ("SSR", 3))


def _km_rarity_of(text):
    n = len(text)
    if n <= 6:
        return "N"
    if n <= 10:
        return "R"
    if n <= 16:
        return "SR"
    return "SSR"


def _km_buckets():
    """按稀有度把所有颜文字分桶（惰性构建）。"""
    global _km_bucket_cache
    if _km_bucket_cache is None:
        buckets = {"N": [], "R": [], "SR": [], "SSR": []}
        for _key, v in _load_kaomoji().items():
            for item in v["items"]:
                buckets[_km_rarity_of(item[0])].append((item[0], v["label"]))
        _km_bucket_cache = buckets
    return _km_bucket_cache


def _cmd_km_gacha(args=None, ctx=None):
    """抽一张颜文字卡：N / R / SR / SSR。"""
    buckets = _km_buckets()
    if not any(buckets.values()):
        return "颜文字库还没生成，先运行：python scrape_kaomoji.py"
    names = [n for n, _w in _KM_RARITY]
    weights = [w for _n, w in _KM_RARITY]
    rarity = random.choices(names, weights=weights)[0]
    text, label = random.choice(buckets[rarity])
    return f"🎴 {rarity} · {label}\n{text}"


# 主动推送相关（验证「消息推送」权限 + 定时道安）
PUSH_CMDS = {"/推送测试", "/晚安", "/晚安推送", "/早安", "/早安推送",
             "/午安", "/下午好", "/夜深了", "/夜深了推送",
             "/该睡了", "/该睡了推送"}
_GROUPS_FILE = "groups.json"      # 记录机器人被 @ 过的群，作为定时推送目标
# 定时道安时间表：(时, 分) -> 类型。0 点即 24:00（显示时转成 24:00）
_GREET_SCHEDULE = {(0, 0): "midnight",   # 0:00（即 24:00）该睡了，可爱催促群员睡觉
                   (1, 10): "late",       # 1:10 夜深了
                   (8, 0): "morning",    # 8:00 早安
                   (12, 0): "noon",      # 12:00 午安
                   (16, 0): "afternoon",  # 16:00 下午好
                   (23, 0): "night"}     # 23:00 晚安
_GREET_LABEL = {"morning": "早安", "noon": "午安", "afternoon": "下午好",
                "night": "晚安", "late": "夜深了", "midnight": "该睡了"}

_GREETINGS = {
    "morning": [
        "早呀。今天也慢慢来，不用急着发光。",
        "早安。窗外的光都到了，你也该醒啦。",
        "新的一天。先喝口水，再想别的事。",
        "早。今天也要好好吃饭呀。",
        "醒了就伸个懒腰，今天会是个好日子。",
        "早安。路是慢慢走的，饭是一口口吃的。",
        "早上好。昨晚没睡好也没关系，今天重新开机。",
        "早。世界还没吵起来，先享受这一会儿安静。",
    ],
    "noon": [
        "中午啦。去吃点热的，别糊弄胃。",
        "午安。饭要趁热吃，心事可以放凉点。",
        "该吃饭了。今天也要好好吃饭呀。",
        "中午了，放下手里的，先喂饱自己。",
        "午安。吃饱了，下午才有劲儿。",
        "午饭时间到。别省这一顿，你值得好好吃。",
        "中午好。先吃饭，难题等吃饱了再说。",
    ],
    "afternoon": [
        "下午好。累了就抬头看看窗外。",
        "下午啦。喝口水，肩膀松一松。",
        "这个点最容易犯困，允许自己发会儿呆。",
        "下午好。还有小半天，慢慢来也来得及。",
        "伸个懒腰吧，下午不该只剩赶路。",
        "下午好。要是有点累，就当是给自己放个短假。",
        "这个点光线最好，适合把心放宽一点。",
    ],
    "night": [
        "夜深了。把今天先放下吧。",
        "晚安。今天辛苦了，剩下的明天再说。",
        "该睡啦。明天又是新的一天。",
        "晚安。被子盖好，梦会轻一点。",
        "夜深了。今天已经做得很好了，休息吧。",
        "晚安。手机放下，眼睛也该歇了。",
        "夜深了。没做完的留给明天，先睡一觉。",
        "晚安。今天的不开心，别带进被子里。",
    ],
    "late": [
        "夜深了。该说晚安了，明天见。",
        "都这个点了，快去睡吧。晚安。",
        "夜深了。今天的疲惫就到这儿，晚安。",
        "凌晨了。别熬啦，晚安，好梦。",
        "夜深了。把手机放下，好好睡一觉。晚安。",
    ],
    "midnight": [
        "都0点啦！还醒着的群员统统去睡觉，不然明天变熊猫眼哦",
        "午夜钟声敲过咯～ 手机交出来，眼睛闭起来，快去睡！",
        "0点整，小忺点名：现在还亮着屏幕的，全部躺平！",
        "已经是新的一天啦，但意思是——昨天该睡了！乖，闭眼",
        "0点了哦。再不睡我就挨个去你们梦里催，听话，睡吧～",
        "夜猫子们，0点啦！该给眼睛放个假了，晚安，明天见。",
        "叮——午夜12点。这不是熬夜许可，是睡觉口令，快去睡！",
    ],
}

_GREET_EMOTION = {"morning": "happy", "noon": "food",
                  "afternoon": "calm", "night": "sleepy", "late": "sleepy",
                  "midnight": "sleepy"}


def _record_group(group_id):
    """记下机器人待过的群，作为定时道安的推送目标。"""
    if not group_id:
        return
    groups = _load_json(_GROUPS_FILE, [])
    if group_id not in groups:
        groups.append(group_id)
        _save_json(_GROUPS_FILE, groups)


def _remove_group(group_id):
    """从 groups.json 里删除一个群（如已注销），避免定时推送反复报错。"""
    if not group_id:
        return
    groups = _load_json(_GROUPS_FILE, [])
    if group_id in groups:
        groups = [g for g in groups if g != group_id]
        _save_json(_GROUPS_FILE, groups)


def _greet_text(kind):
    """随机一句道安 + 一个对应情绪的颜文字。"""
    pool = _GREETINGS.get(kind) or ["来打个招呼～"]
    line = random.choice(pool)
    emo = _km(_GREET_EMOTION.get(kind, "happy"), 0.9)
    return line + ("  " + emo if emo else "")


_PUSH_DISABLED = False  # 主动推送权限缺失（40034105）时置位，停止定时道安以免刷错误


async def _push_one(group_openid, body):
    """单群主动发送，返回异常对象或 None（供线程里取结果）。"""
    try:
        await BOT_API.post_group_message(group_openid=group_openid, content=body)
        return None
    except Exception as e:  # noqa: BLE001
        return e


def _push_once(kind):
    """把一次定时推送提交到机器人主事件循环（由后台线程调用）。

    定时道安本质是「无触发消息」的主动发送，需要后台「消息推送」权限；
    没权限一律 40034105，这里捕获一次后停掉定时任务，避免反复刷错误日志。
    """
    global _PUSH_DISABLED
    loop = BOT_LOOP
    if _PUSH_DISABLED:
        print(f"[定时] 主动推送未开通权限，跳过 {kind}")
        return
    if not loop or not BOT_API:
        print(f"[定时] 机器人未就绪，跳过 {kind} 推送")
        return
    groups = _load_json(_GROUPS_FILE, [])
    if not groups:
        print(f"[定时] 还没记录到任何群，跳过 {kind} 推送")
        return
    body = _censor(_greet_text(kind))
    plan = _voice_plan("proactive")  # 定时道安走 proactive 配置（同理 chat）
    for g in groups:
        # voice_only 时不发文字，只发语音；both 时文字+语音；none 时只发文字
        if plan != "voice_only":
            fut = asyncio.run_coroutine_threadsafe(_push_one(g, body), loop)
            try:
                err = fut.result(timeout=15)
            except Exception as e:  # noqa: BLE001
                err = e
            if err:
                emsg = str(err)
                if "40034105" in emsg or "无权限" in emsg:
                    _PUSH_DISABLED = True
                    print("[定时] 当前机器人未开通「消息推送」主动权限（40034105），已停止定时道安。"
                          "如需定时推送，请到 QQ 机器人后台「开发设置」开通「消息推送」能力；"
                          "或用 @我 /晚安 等被动推送（无需该权限）。")
                    break
                # 群/用户已注销或资源不存在（11255 / 40011028）：该群无法再推送，从记录里移除，避免反复刷错误
                if ("11255" in emsg or "40011028" in emsg
                        or "已注销" in emsg or "请求的资源不存在" in emsg):
                    print(f"[定时] 群 {str(g)[:8]} 已注销/不存在，已从定时推送列表移除")
                    _remove_group(g)
                    continue
                print(f"[定时] 推送失败 -> 群 {str(g)[:8]}: {emsg[:120]}")
            else:
                print(f"[定时] 已推送 {kind} -> 群 {str(g)[:8]}…")
        if plan != "none":
            # 定时道安也是普通聊天：固定音色；原 _vstyle 从未赋值会直接 NameError，这里显式传 None
            vfut = asyncio.run_coroutine_threadsafe(_push_voice(g, body, style=None), loop)
            try:
                vres = vfut.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                vres = e
            if vres is not True:
                print(f"[定时] 语音道安失败 -> 群 {str(g)[:8]}: {str(vres)[:120]}")


def _greet_scheduler():
    """后台线程：按 (时, 分) 到点触发 早安 / 午安 / 下午好 / 晚安 / 夜深了。

    只在「到点后 5 分钟内」的窗口里触发，避免重启后把当天已过的时间点补发一遍。
    """
    sent = set()
    while True:
        now = datetime.datetime.now()
        _reset_usage_if_new_day()  # 每天 0 点重置词元计数（与配额刷新对齐）
        today = now.date()
        for (hh, mm), kind in _GREET_SCHEDULE.items():
            key = f"{today}-{kind}"
            if key in sent:
                continue
            target = datetime.datetime.combine(today, datetime.time(hh, mm))
            if target <= now <= target + datetime.timedelta(minutes=5):
                sent.add(key)
                try:
                    _push_once(kind)
                except Exception as e:
                    print(f"[定时] 推送失败：{e}")
        # 清掉早于今天的记录，避免集合无限增长
        sent = {k for k in sent if k.startswith(str(today))}
        time.sleep(30)


def _record_openid(sender, openid):
    """记下用户的 user_openid，便于日后主动私聊推送。"""
    if not sender or not openid:
        return
    data = _load_json("c2c_users.json", {})
    if data.get(sender) != openid:
        data[sender] = openid
        _save_json("c2c_users.json", data)


def _cmd_push_hint(args=None, ctx=None):
    return "这条需要 @我 触发，我才能主动向群里推送～（先在后台开通「消息推送」能力）"


async def _handle_push(message, text):
    """主动发送（验证权限用）。群则发群，私聊则发私聊。"""
    api = BOT_API
    if api is None:
        await _reply(message, content=_with_prefix("机器人还在启动中，稍后再试～"))
        return
    author = getattr(message, "author", None)
    group_openid = getattr(message, "group_openid", None)
    user_openid = getattr(author, "user_openid", None) if author else None
    if user_openid:  # 顺手记下 openid，供以后主动私聊
        _record_openid(getattr(author, "member_openid", None) or user_openid, user_openid)

    if text in ("/夜深了", "/夜深了推送"):
        body = _greet_text("late")
    elif text in ("/该睡了", "/该睡了推送"):
        body = _greet_text("midnight")
    elif text in ("/晚安", "/晚安推送"):
        body = _greet_text("night")
    elif text in ("/早安", "/早安推送"):
        body = _greet_text("morning")
    elif text == "/午安":
        body = _greet_text("noon")
    elif text == "/下午好":
        body = _greet_text("afternoon")
    else:  # /推送测试
        body = "🤖 主动消息测试：如果这条出现在群里，说明「消息推送」能力已开通 ✅" + _km("happy", 0.6)

    body = _censor(body)  # 主动推送同样清洗输出
    # 用被动回复（message.reply 自带 msg_id），无需「消息推送」主动权限也能发
    try:
        await _reply(message, content=body)
        scope = "（私聊）" if not group_openid else ""
        await _reply(message, content=_with_prefix(
            f"刚用被动回复发了一条{scope}，不需要「消息推送」权限～\n"
            "只有「定时自动道安」才需要去 QQ 机器人后台开通「消息推送」能力。"))
    except Exception as e:
        await _reply(message, content=_with_prefix(
            "推送失败：\n" + str(e)[:220]))


# ---------- 群/私聊富媒体发送（图片/视频/语音）----------
# 支持三种来源：公网 URL、本地文件二进制直传(<=5MB)、图库(仅图片)
# 注意：QQ 群接口上传的文件只能发到群；私聊走对应 c2c 接口
_MEDIA_CMDS = {"/图片": 1, "/视频": 2, "/语音": 3}
_MEDIA_NAME = {1: "图片", 2: "视频", 3: "语音"}
_URL_RE = re.compile(r"https?://[^\s]+")
_MAX_BINARY = 5 * 1024 * 1024  # 二进制直传上限 5MB，超过请改用 URL 或分片


def _lib_item_to_pair(item):
    """图库项可能是 URL 或本地路径，统一解析为 (url, path) 二元组。"""
    if item and _URL_RE.match(item):
        return item, None
    if item and os.path.isfile(item):
        return None, item
    return None, None


def _parse_media_args(content):
    """从 /图片|/视频|/语音 指令解析出多个 (url, path)，支持空格分隔、引号包裹路径。"""
    parts = content.split(None, 1)
    if len(parts) < 2:
        return []
    rest = parts[1].strip()
    toks = re.findall(r'https?://\S+|\"[^\"]+\"|\'[^\']+\'|\S+', rest)
    out = []
    for t in toks:
        t = t.strip().strip('"').strip("'").strip("<>")
        if not t:
            continue
        if _URL_RE.match(t):
            out.append((t, None))
        else:
            out.append((None, t))
    return out


async def _upload_media(api, group_openid, openid, file_type, *,
                        url=None, data=None, file_name=None, srv_send_msg=True):
    """上传富媒体到群或私聊，返回接口响应(dict)。

    - 有 url：走 botpy 封装的 URL 上传（平台下载转存，不限 5MB）
    - 有 data：二进制直传（<=5MB），直接打 /files 接口的 file_data 字段
    """
    if group_openid:
        if url:
            return await api.post_group_file(group_openid=group_openid, file_type=file_type,
                                             url=url, srv_send_msg=srv_send_msg)
        from botpy.http import Route
        payload = {"file_type": file_type, "srv_send_msg": srv_send_msg,
                   "file_name": file_name or "file"}
        if data:
            payload["file_data"] = base64.b64encode(data).decode()
        route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
        return await api._http.request(route, json=payload)
    if openid:
        if url:
            return await api.post_c2c_file(openid=openid, file_type=file_type,
                                           url=url, srv_send_msg=srv_send_msg)
        from botpy.http import Route
        payload = {"file_type": file_type, "srv_send_msg": srv_send_msg,
                   "file_name": file_name or "file"}
        if data:
            payload["file_data"] = base64.b64encode(data).decode()
        route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        return await api._http.request(route, json=payload)
    return None


# 上传软上限：超过就先压缩，避免大图 base64 直传卡到 /files 超时
_UPLOAD_SOFT_MAX = 1_200_000


def _shrink_for_upload(data):
    """超过软上限就逐级压缩；压不下来（仍超 5MB 硬上限）返回 None，由调用方报失败。"""
    if not data:
        return data
    if len(data) <= _UPLOAD_SOFT_MAX:
        return data
    for max_side, quality in ((1280, 80), (1024, 74), (850, 68), (700, 60)):
        try:
            out = normalize_image(data, max_side=max_side, quality=quality)
            if len(out) <= _UPLOAD_SOFT_MAX:
                print(f"[发图] 图片 {len(data)//1024}KB -> 压缩到 {len(out)//1024}KB 再上传")
                return out
        except Exception:
            continue
    if len(data) > _MAX_BINARY:  # 仍超 5MB 硬上限，直接放弃，别去撞超时
        return None
    return data


async def _get_file_info(api, group_openid, openid, file_type, *, url=None, data=None, file_name=None):
    """上传媒体但不发送，返回 file_info（用于图文混排的 media 字段）。

    /files 偶尔会超时（大图或平台侧下载慢），这里重试一次再放弃。
    """
    for attempt in range(2):
        try:
            resp = await _upload_media(api, group_openid, openid, file_type,
                                       url=url, data=data, file_name=file_name, srv_send_msg=False)
            resp = resp if isinstance(resp, dict) else (getattr(resp, "__dict__", {}) or {})
            fi = resp.get("file_info")
            if fi:
                return fi
        except Exception as e:
            if attempt == 0:
                print(f"[发图] 上传超时/失败，重试一次（{str(e)[:60]}）")
                await asyncio.sleep(1.5)
                continue
            raise
    return None


async def _reply_with_media(message, text, *, file_type=1, url=None, data=None, file_name=None):
    """被动回复带图：文字走被动文本回复，图片走 msg_type=7 媒体回复（无需 markdown 模板权限）。

    msg_seq 由 _reply 自动递增，避免同一 msg_id 多次回复被判重复(40054005)。
    """
    api = BOT_API
    if api is None:
        await _reply(message, content=_with_prefix(text + "（图片没发出：机器人未就绪）"))
        return
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    try:
        if data is not None:  # 大图先压缩，避免 /files 请求超时
            data = _shrink_for_upload(data)
            if data is None:
                await _reply(message, content=_with_prefix(text + "（图片超过 5MB，发不出去）"))
                return
        fi = await _get_file_info(api, group_openid, openid, file_type,
                                  url=url, data=data, file_name=file_name)
        if not fi:
            await _reply(message, content=_with_prefix(text + "（图片上传失败）"))
            return
        await _reply(message, content=text)
        # 图片作为被动媒体回复：msg_type=7 = media 富媒体，不含 markdown，无需模板权限
        await _reply(message, media={"file_info": fi}, msg_type=7)
    except Exception as e:
        await _reply(message, content=_with_prefix(text + f"（带图失败：{str(e)[:120]}）"))


_WANT_IMAGE_RE = re.compile(r"表情包|来张图|发张图|来个图|发图|斗图|看个图|求图|图来|给我张图|发表情|来点图")


def _wants_image(content):
    """用户是否在索要图片（用于触发图文混排回复，避免「这图好看」之类误触）。
    优先用 NLP 意图分类（intent_model），不可用则回退正则。"""
    if intent_model is not None and intent_model.predict_intent(content or "") == "image":
        return True
    return bool(content) and bool(_WANT_IMAGE_RE.search((content or "").lower()))


_STORY_RE = re.compile(
    r"讲\s*个?\s*(?:恐怖|鬼|吓人|灵异|悬疑)?\s*故事|说\s*个?\s*(?:恐怖|鬼)?\s*故事|来\s*个?\s*故事|"
    r"讲\s*个?\s*童话|讲\s*个?\s*寓言|讲\s*个?\s*段子|给我讲|恐怖\s*故事|鬼\s*故事|"
    r"tell\s*me\s*a\s*story|story|讲个?鬼"
)


def _wants_story(content):
    """用户是否在求讲故事/段子（命中则回复自动用语音讲，并按内容智能选情绪）。
    优先用 NLP 意图分类（intent_model），不可用则回退正则。"""
    if intent_model is not None and intent_model.predict_intent(content or "") == "story":
        return True
    return bool(content) and bool(_STORY_RE.search((content or "").lower()))


# ---------- 唱歌意图识别：/@ 触发「唱首歌」这类明确祈使才跳到唱歌 ----------
_SING_RE = re.compile(
    r"唱[个一首两]?\s*歌|来[一更]?\s*首\s*歌|给我唱|唱给(我|你)|想听你?唱|唱一?个|"
    r"点[一更]?\s*首\s*歌|来段歌|唱首|来一首"
)


def _is_sing_request(content):
    """自然触发的唱歌意图：命中明确『唱歌』祈使短语才触发，避免『这首歌好听』误触发。
    返回主题词（剥掉触发词/称呼后的剩余；空=即兴唱）；非唱歌意图返回 None。"""
    c = (content or "").strip()
    if not c or len(c) > 50:
        return None
    if not _SING_RE.search(c):
        return None
    theme = _SING_RE.sub("", c)
    theme = re.sub(r"^(小忺|忺|机器人|bot)\s*", "", theme, flags=re.I).strip(" ，。！、~～")
    return theme


# ---------- 叙事类内容（故事/笑话）意图解析：像真人一样「让讲就讲」----------
# 触发要求「讲/说/来/编/听」等动词 + 故事/童话/寓言（限定词可前可后，如「讲个故事恐怖」「讲个恐怖故事」）
_NARR_STORY_RE = re.compile(
    r"^(?:[\u4e00-\u9fff]{0,4})?(?:讲|说|来|编|听|想听|给我讲)[^。！？!?\n]{0,10}?(?:故事|童话|寓言|传说)"
    r"|^[/！!]?故事[吧呀哈~～]?$"
)
_NARR_JOKE_RE = re.compile(
    r"^(?:[\u4e00-\u9fff]{0,4})?(?:讲|说|来|来个?|给我讲)[^。！？!?\n]{0,6}?(?:笑话|段子|梗)"
    r"|^[/！!]?(?:冷?笑话|段子|梗)[吧呀哈~～]?$"
)
# 用户点名想要的情绪/类型（出现在请求里就照办）→ 语音情绪预设（确定性映射，省词元且稳）
_NARR_MOOD_RE = re.compile(
    r"恐怖|鬼|吓人|灵异|悬疑|惊悚|童话|寓言|睡前|温馨|治愈|温情|搞笑|欢乐|开心|高兴|悲伤|感人|科幻"
)
_NARR_MOOD2EMO = {
    "恐怖": "神秘", "鬼": "神秘", "吓人": "神秘", "灵异": "神秘", "悬疑": "神秘", "惊悚": "神秘",
    "开心": "开心", "高兴": "开心", "搞笑": "开心", "欢乐": "开心",
    "悲伤": "悲伤", "感人": "悲伤",
    "童话": "俏皮", "睡前": "温柔", "温馨": "温柔", "治愈": "温柔", "温情": "温柔",
}
# 点名情绪时，生成指令里用的「类型词」
_NARR_MOOD2LABEL = {
    "恐怖": "恐怖", "鬼": "恐怖", "吓人": "恐怖", "灵异": "恐怖", "悬疑": "悬疑", "惊悚": "悬疑",
    "开心": "开心", "高兴": "开心", "搞笑": "搞笑", "欢乐": "欢乐",
    "悲伤": "悲伤", "感人": "感人",
    "童话": "童话", "睡前": "睡前", "温馨": "温馨", "治愈": "治愈", "温情": "温馨", "科幻": "科幻",
}


def _parse_narrative(content):
    """识别「讲个故事/讲个笑话」这类叙事请求 → {'kind': 'story'|'joke', 'mood': 点名情绪或None}，否则 None。
    像真人一样：只要是想听故事/笑话就真的讲一个（限定词可前可后，如「讲个故事恐怖」「讲个故事开心」）。"""
    c = (content or "").strip()
    if not c:
        return None
    if _NARR_JOKE_RE.search(c):
        m = _NARR_MOOD_RE.search(c)
        return {"kind": "joke", "mood": m.group(0) if m else None}
    if _NARR_STORY_RE.search(c):
        m = _NARR_MOOD_RE.search(c)
        return {"kind": "story", "mood": m.group(0) if m else None}
    return None


def _narrative_directive(nk):
    """给 LLM 的强指令：必须真的输出一个完整的故事/笑话正文，禁止反问/闲聊/只给开头。"""
    kind = "笑话" if (nk or {}).get("kind") == "joke" else "故事"
    mood = (nk or {}).get("mood") or ""
    label = _NARR_MOOD2LABEL.get(mood, "") if mood else ""
    body = (label + kind) if label else kind
    return (f"【系统指令：用户想听一个{body}。请直接输出一个完整、有趣、有内容的{body}正文，"
            f"不要反问、不要解释、不要只给开头或结尾，200字以内。】")


def _narrative_style(nk, text):
    """选语音情绪：用户点名了情绪/类型 → 直接用对应预设（确定、省词元）；否则智能判断正文情绪。"""
    mood = (nk or {}).get("mood") or ""
    emo = _NARR_MOOD2EMO.get(mood)
    if emo:
        return _EMOTION_PRESETS.get(emo)
    return _choose_story_emotion(text)


def _narrative_voice_plan():
    """叙事类（故事/笑话）专用语音方案：只要语音开着就必发语音（不受 frequency 随机影响）。"""
    cfg = load_voice_config()
    if not cfg.get("enabled") or not cfg.get("message_types", {}).get("story", False):
        return "none"
    if random.random() < float(cfg.get("voice_only_chance", 0.0)):
        return "voice_only"
    return "both"


# 按故事文本智能选情绪：扫描关键词，命中最多的一类；无命中用温柔旁白音
_STORY_EMOTION_RULES = [
    ("神秘", ["恐怖", "鬼", "害怕", "黑暗", "灵异", "诡异", "惊悚", "阴森", "诅咒", "诡计"]),
    ("悲伤", ["哭", "悲伤", "难过", "离别", "伤心", "遗憾", "泪", "孤独", "思念", "逝去"]),
    ("愤怒", ["愤怒", "生气", "仇恨", "报复", "怒火", "战争", "暴怒"]),
    ("开心", ["笑", "搞笑", "逗", "欢乐", "开心", "滑稽", "幽默", "趣", "乐"]),
    ("俏皮", ["调皮", "俏皮", "萌", "可爱", "撒娇"]),
    ("温柔", ["温柔", "爱", "暖", "感动", "陪伴", "守护", "温暖", "治愈"]),
]


def _emotion_style_via_llm(text):
    """用大模型直接生成贴合文本（可含混合情感）的朗读风格描述，喂给 voicedesign TTS。

    一句话描述即可（如「温柔但略带悲伤」「欢快又神秘」），天然支持多情感；
    失败/超预算/未启用平台 API 时返回 None，由调用方回退关键词逻辑。
    """
    if not text or not CONFIG.get("ENABLE_PLATFORM_API"):
        return None
    prompt = (
        "只输出一句中文，描述下面这段文字朗读时应使用的语气与情绪"
        "（允许混合多种，例如'温柔但略带悲伤'、'欢快又神秘'），"
        "不要解释、不要多余标点、不要自称：\n" + text[:600]
    )
    try:
        desc = answer_from_llm(prompt)
    except Exception as e:
        print(f"[情感LLM] 调用失败：{e}")
        return None
    if not desc:
        return None
    desc = desc.strip().strip("。. ").strip()
    desc = re.sub(r"^(作为|我是|忺[：:]?|朗读语气[是为：: ]*)", "", desc).strip()
    if not desc or len(desc) > 40:
        return None
    return ("用" + desc + "的语气朗读") if not desc.startswith("用") else desc


def _choose_story_emotion(text):
    """根据故事内容挑一个情绪描述（用于 mimo-v2.5-tts-voicedesign）。

    优先级：① 统一多任务模型（意图+多标签情感+NER，训练出的权重文件）
            ② 已训练的情感分类权重 → ③ 大模型直接生成风格描述（天然支持混合情感）
            → ④ 关键词启发式回退。
    """
    if nlp_understanding is not None:
        res = nlp_understanding.predict(text)
        if res:
            st = nlp_understanding.to_tts_style(res)
            if st:
                return st
    if emotion_model is not None:
        st = emotion_model.predict_emotion_style(text)
        if st:
            return st
    # 大模型直接产出自由文本风格描述，一段话多种情感也能自然表达
    style = _emotion_style_via_llm(text)
    if style:
        return style
    t = text or ""
    best, best_n = "温柔", 0
    for emo, kws in _STORY_EMOTION_RULES:
        n = sum(t.count(k) for k in kws)
        if n > best_n:
            best, best_n = emo, n
    return _EMOTION_PRESETS.get(best, "用温柔、甜美、轻声细语的语气朗读")


def _choose_voice_emotion(text):
    """按文本关键词快速挑选朗读情绪（免费、无大模型调用，避免烧词元预算）。

    注意：普通聊天/主动接话/私聊/定时道安现已改成「固定音色」（style=None，不逐句换情绪），
    否则每句一个情绪会切到 voicedesign 模型、音色一直变很难受；只有故事/笑话走
    _narrative_style 才变情绪。本函数保留作备用/回退，当前暂无调用点。
    """
    t = text or ""
    best, best_n = "温柔", 0
    for emo, kws in _STORY_EMOTION_RULES:
        n = sum(t.count(k) for k in kws)
        if n > best_n:
            best, best_n = emo, n
    if best_n == 0:  # 无明显情绪：按语气给个轻快/温柔的默认
        best = "开心" if any(k in t for k in ("哈", "笑", "开心", "好耶", "棒", "嘿")) else "温柔"
    return _EMOTION_PRESETS.get(best, "用自然、温柔的语气朗读")


# ---------- 语音输出（TTS：官方 MiMo API，OpenAI 兼容 chat/completions + audio）----------
# 语音回复参数集中在 assets/voice_config.json（可热更新，无需重启），详见该文件。
VOICE_CONFIG_FILE = os.path.join(BASE_DIR, "assets", "voice_config.json")
_VOICE_CFG_FALLBACK = {
    "enabled": False,
    "frequency": 1.0,
    "voice_only_chance": 0.0,
    "message_types": {"chat": True, "proactive": True, "story": True, "command": False},
    "voice": "mimo_default",
    "effect": "none",
    "style": "",
    "max_chars": 150,
    # 小忺专属音色：voice_sample 指向用 voicedesign 造好的样音文件（相对 BASE_DIR）。
    # 配了它 -> 聊天/故事都走 voiceclone 复刻该样音（音色恒定、专属），
    #           语气/情绪改由 user 指令控制（voiceclone 支持），不再切 voicedesign。
    "voice_sample": "",
    "voice_sample_mime": "",      # 留空按扩展名自动判断（.wav -> audio/wav，否则 audio/mpeg）
    "chat_style": "",             # 聊天时的固定语气指令（留空则不传指令）
    "voice_speed": 1.0,           # 语速微调（ffmpeg atempo，保音高）：1.0=原速，>1 更快（0.5~2.0）
    "voice_design_prompt": "",    # 手动覆盖「音色设计描述」（留空则按 persona.json 自动生成）
    "sing_voice": "茉莉",         # 唱歌专用预置音色（独立于聊天音色：复刻音色不能唱歌，唱歌走这里）
    "tts_backend": "mimo",        # 聊天语音后端：mimo（默认，复刻小忺/预置）| voice_studio（阿里云 Voice Studio）
    "voice_studio_voice": "longxiaochun_v2",  # voice_studio 后端用的音色 ID（系统音色或本账号复刻音色）
}
_voice_cfg_cache = {"mtime": -1, "data": None}


def load_voice_config():
    """读取 assets/voice_config.json，按修改时间缓存；缺失/出错时回退内置默认。"""
    global _voice_cfg_cache
    try:
        mtime = os.path.getmtime(VOICE_CONFIG_FILE)
    except OSError:
        mtime = -1
    if _voice_cfg_cache["data"] is not None and _voice_cfg_cache["mtime"] == mtime:
        return _voice_cfg_cache["data"]
    data = {}
    try:
        if os.path.exists(VOICE_CONFIG_FILE):
            with open(VOICE_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"[语音配置] 读取 voice_config.json 失败，使用内置默认：{e}")
    if not isinstance(data, dict):
        data = {}
    cfg = dict(_VOICE_CFG_FALLBACK)
    for k in ("enabled", "frequency", "voice_only_chance", "voice", "effect", "style", "max_chars",
              "voice_sample", "voice_sample_mime", "chat_style", "voice_design_prompt",
              "voice_speed", "sing_voice", "tts_backend", "voice_studio_voice"):
        if k in data:
            cfg[k] = data[k]
    mt = dict(_VOICE_CFG_FALLBACK["message_types"])
    mt.update(data.get("message_types", {}))
    cfg["message_types"] = mt
    _voice_cfg_cache["data"] = cfg
    _voice_cfg_cache["mtime"] = mtime
    return cfg


def save_voice_config(patch):
    """把 patch 合并写回语音配置文件（供 /voice /音色 /音效 /情绪 等指令即时生效）。"""
    cfg = load_voice_config()
    for k in ("enabled", "frequency", "voice_only_chance", "voice", "effect", "style", "max_chars",
              "voice_sample", "voice_sample_mime", "chat_style", "voice_design_prompt",
              "voice_speed", "sing_voice", "tts_backend", "voice_studio_voice"):
        if k in patch:
            cfg[k] = patch[k]
    if isinstance(patch.get("message_types"), dict):
        cfg["message_types"].update(patch["message_types"])
    try:
        os.makedirs(os.path.dirname(VOICE_CONFIG_FILE), exist_ok=True)
        # 保留文件中以 _ 开头的注释字段（如 _说明）
        raw = {}
        if os.path.exists(VOICE_CONFIG_FILE):
            try:
                with open(VOICE_CONFIG_FILE, encoding="utf-8") as f:
                    raw = json.load(f) or {}
            except Exception:
                raw = {}
        out = dict(cfg)
        out.update({k: v for k, v in raw.items() if str(k).startswith("_")})
        with open(VOICE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _voice_cfg_cache["mtime"] = os.path.getmtime(VOICE_CONFIG_FILE)
    except Exception as e:
        print(f"[语音配置] 保存失败：{e}")


# mimo-v2.5-tts 支持的预置音色（官方文档）
_MIMO_VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]
# 输出音效：key -> (中文名, ffmpeg 滤镜)；helium/bass 用 rubberband 保持时长，缺失则回退 asetrate
_AUDIO_EFFECTS = {
    "helium": ("氦气", "rubberband=pitch=1.5"),
    "bass":   ("低沉", "rubberband=pitch=0.7"),
    "fast":   ("加速", "atempo=1.5"),
    "slow":   ("减速", "atempo=0.75"),
}
_EFFECT_ALIASES = {"氦气": "helium", "低沉": "bass", "加速": "fast", "减速": "slow",
                   "关": "none", "无": "none", "关闭": "none", "none": "none"}
# 情绪/风格：用 mimo-v2.5-tts-voicedesign 模型，user 消息写风格描述，assistant 写合成文本
_EMOTION_PRESETS = {
    "开心": "用开心、轻快、充满活力的语气朗读",
    "温柔": "用温柔、甜美、轻声细语的语气朗读",
    "悲伤": "用悲伤、低沉、缓慢的语气朗读",
    "愤怒": "用愤怒、激动、有力的语气朗读",
    "俏皮": "用俏皮、可爱、带点撒娇的语气朗读",
    "撒娇": "用撒娇、甜腻、软萌的语气朗读",
    "严肃": "用严肃、正式、沉稳的语气朗读",
    "神秘": "用神秘、低沉、耳语般的语气朗读",
}
_EMOTION_ALIASES = {  # 英文/别名 -> 中文 key
    "happy": "开心", "joy": "开心", "gentle": "温柔", "soft": "温柔",
    "sad": "悲伤", "angry": "愤怒", "cute": "俏皮", "coquettish": "撒娇",
    "serious": "严肃", "mysterious": "神秘",
}


# 语音状态的读写已统一到 load_voice_config()/save_voice_config()（见上方）


def _ffmpeg_exe():
    """优先用 imageio-ffmpeg 自带的静态 ffmpeg，否则取系统 ffmpeg。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


def _apply_audio_effect(data, effect):
    """对 TTS 音频字节做后处理（氦气/低沉/加速/减速）。返回 (bytes, ext)。

    无 ffmpeg 或处理失败则原样返回 (data, "mp3")。输出优先 mp3（QQ 语音兼容），
    若当前 ffmpeg 不带 libmp3lame 则回退 wav。"""
    import subprocess
    if not effect or effect == "none":
        return data, "mp3"
    exe = _ffmpeg_exe()
    if not exe:
        print("[音效] 未找到 ffmpeg，跳过音效（需 pip install imageio-ffmpeg）")
        return data, "mp3"
    spec = _AUDIO_EFFECTS.get(effect)
    if not spec:
        return data, "mp3"
    filt = spec[1]

    def _run(filt_str, out_ext):
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-af", filt_str]
        if out_ext == "mp3":
            cmd += ["-acodec", "libmp3lame"]
        cmd += ["-f", out_ext, "-"]
        try:
            p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=60)
        except Exception as e:
            print(f"[音效] 异常：{e}")
            return None
        if p.returncode == 0 and p.stdout:
            return p.stdout
        return False

    for ext in ("mp3", "wav"):
        r = _run(filt, ext)
        if r:
            return r, ext
    # rubberband 缺失 -> 用 asetrate 回退（pitch 变，但时长会变短/长）
    if effect in ("helium", "bass"):
        fac = 1.5 if effect == "helium" else 0.7
        filt2 = f"asetrate=44100*{fac},aresample=44100"
        for ext in ("mp3", "wav"):
            r = _run(filt2, ext)
            if r:
                return r, ext
    print("[音效] 处理失败，原样返回")
    return data, "mp3"


def _apply_audio_speed(data, speed):
    """用 ffmpeg atempo 调整语速（**只改变速度、不改音高**）。返回 (bytes, ext)。

    speed: 1.0=原速，>1 更快，<1 更慢；atempo 单滤镜只支持 0.5~2.0，超出先夹到该区间。
    无 ffmpeg 或处理失败则原样返回 (data, "mp3")。
    """
    try:
        s = float(speed)
    except Exception:
        return data, "mp3"
    if abs(s - 1.0) < 0.01:
        return data, "mp3"
    s = max(0.5, min(2.0, s))
    exe = _ffmpeg_exe()
    if not exe:
        print("[语速] 未找到 ffmpeg，跳过变速（需 pip install imageio-ffmpeg）")
        return data, "mp3"
    import subprocess
    for ext in ("mp3", "wav"):
        try:
            cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                   "-af", f"atempo={s}"]
            if ext == "mp3":
                cmd += ["-acodec", "libmp3lame"]
            cmd += ["-f", ext, "-"]
            p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=60)
            if p.returncode == 0 and p.stdout:
                return p.stdout, ext
        except Exception as e:
            print(f"[语速] 异常：{e}")
            break
    print("[语速] 处理失败，原样返回")
    return data, "mp3"


# 小忺专属音色的样音缓存：{path, mtime, data_url}
_voice_sample_cache = {"path": None, "mtime": -1, "data_url": None}


def _load_voice_sample():
    """读「小忺专属音色」样音 -> data URL（data:audio/wav;base64,...）；未配置/缺失返回 None。

    样音由 /造音色 用 voicedesign 按人设生成并存到 assets/voice_xiaoxin.wav；
    之后所有语音都用 voiceclone 复刻它 —— 音色恒定且专属，情绪则靠 user 指令切换
    （voiceclone 支持自然语言语气/情绪控制），因此不必再切 voicedesign。
    """
    import base64
    cfg = load_voice_config()
    p = (cfg.get("voice_sample") or "").strip()
    if not p:
        return None
    path = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
    # 安全闸：只允许读项目内的音频，配置被误填成项目外路径时直接忽略（不读外面的文件）
    if not _safe_path_within(path, BASE_DIR):
        print(f"[音色] 样音路径不在项目内，已忽略：{path}")
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        print(f"[音色] 样音不存在：{path}（用 /造音色 生成）")
        return None
    if (_voice_sample_cache["path"] == path and _voice_sample_cache["mtime"] == mtime
            and _voice_sample_cache["data_url"]):
        return _voice_sample_cache["data_url"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
        mime = (cfg.get("voice_sample_mime") or "").strip()
        if not mime:
            mime = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("utf-8"))
    except Exception as e:
        print(f"[音色] 样音读取失败：{e}")
        return None
    _voice_sample_cache.update({"path": path, "mtime": mtime, "data_url": data_url})
    return data_url


def _tts_post(base, key, model, messages, audio_cfg):
    """发一次 TTS 请求，返回 (audio_bytes, err)。"""
    import base64
    import requests
    try:
        resp = requests.post(
            base + "/chat/completions",
            headers={"api-key": key, "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "audio": audio_cfg},
            timeout=90,
        )
    except Exception as e:
        return None, f"MiMo 调用异常：{e}"
    if resp.status_code != 200:
        body = (resp.text or "")[:300]
        if resp.status_code in (401, 403):
            return None, f"MiMo 鉴权失败（{resp.status_code}）：MIMO_API_KEY 无效 —— {body}"
        return None, f"MiMo 返回 {resp.status_code}：{body}"
    try:
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        b64 = (msg.get("audio") or {}).get("data")
        if not b64:
            return None, "MiMo 未返回音频数据：" + str(data)[:200]
        return base64.b64decode(b64), ""
    except Exception as e:
        return None, f"MiMo 响应解析失败：{e}"


def _tts_mimo(text, style=None, sing=False, voice=None):
    """用官方 MiMo TTS（api.xiaomimimo.com，OpenAI 兼容 chat/completions）合成音频。

    返回 (audio_bytes, err)。
    - sing=True：唱歌模式，仅 mimo-v2.5-tts（预置精品音色）支持；voiceclone/voicedesign 都不支持唱歌。
    - 普通说话音色优先级：
      ① 配了「小忺专属音色」样音 -> voiceclone：音色恒定、专属；style 作为语气/情绪指令
      ② 无样音但传了 style    -> voicedesign：音色随描述变化（故事/备用才走这里）
      ③ 都没有                -> 预置音色 mimo-v2.5-tts（voice 字段）
    """
    key = CONFIG.get("MIMO_API_KEY") or ""
    if not key:
        return None, "未配置 MIMO_API_KEY"
    base = (CONFIG.get("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1").rstrip("/")
    cfg = load_voice_config()
    style = style or cfg.get("style") or ""

    # 唱歌模式：仅 mimo-v2.5-tts（预置精品音色）支持，voiceclone/voicedesign 均不支持唱歌
    if sing:
        model = "mimo-v2.5-tts"
        sv = voice or cfg.get("sing_voice") or cfg.get("voice") or "茉莉"
        if sv not in _MIMO_VOICES:           # 复刻的小忺音色不能用来唱歌，回退到预置
            sv = "茉莉"
        audio, err = _tts_post(
            base, key, model,
            [{"role": "user", "content": style or "用甜美活泼的少女音色，把下面这段当作一首轻快的中文歌来演唱，注意旋律起伏和节奏感，带一点点俏皮"},
             {"role": "assistant", "content": "(唱歌)" + text}],
            {"format": "mp3", "voice": sv},
        )
        return (audio, "") if audio else (None, err)

    # ① 专属音色：复刻小忺样音，音色恒定；语气/情绪走 user 指令（voiceclone 支持）
    ref = _load_voice_sample()
    if ref:
        instr = style or (cfg.get("chat_style") or "")
        audio, err = _tts_post(
            base, key, "mimo-v2.5-tts-voiceclone",
            [{"role": "user", "content": instr}, {"role": "assistant", "content": text}],
            {"format": "mp3", "voice": ref},
        )
        if audio:
            return audio, ""
        print(f"[TTS] 专属音色(voiceclone)失败，回退旧音色：{err}")

    if style:
        # ② 情绪/风格模式（没样音时）：voicedesign，该模型不支持 audio.voice
        model = "mimo-v2.5-tts-voicedesign"
        messages = [
            {"role": "user", "content": style},
            {"role": "assistant", "content": text},
        ]
        audio_cfg = {"format": "mp3"}
    else:
        # ③ 预置音色
        model = CONFIG.get("MIMO_MODEL") or "mimo-v2.5-tts"
        voice = cfg.get("voice") or "mimo_default"
        messages = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
        ]
        audio_cfg = {"format": "mp3", "voice": voice}
    audio, err = _tts_post(base, key, model, messages, audio_cfg)
    return (audio, "") if audio else (None, err)


def _tts_voice_studio(text, voice=None, style=None):
    """用阿里云 Voice Studio（convai-platform）TTS 合成音频。返回 (audio_bytes, err)。

    同步模式：POST /v1/tts/synthesize -> 返回 audio.url（24h 临时地址）-> 下载 -> 字节。
    仅作为聊天语音后端之一；唱歌仍走 MiMo（voice_studio 无唱歌专用能力）。
    """
    import requests
    key = CONFIG.get("VOICE_STUDIO_API_KEY") or ""
    if not key:
        return None, "未配置 VOICE_STUDIO_API_KEY"
    base = (CONFIG.get("VOICE_STUDIO_BASE_URL") or "https://convai-platform.aliyuncs.com").rstrip("/")
    model = CONFIG.get("VOICE_STUDIO_MODEL") or "cosyvoice-v3-flash"
    cfg = load_voice_config()
    voice = voice or cfg.get("voice_studio_voice") or "longxiaochun_v2"
    inp = {
        "text": text,
        "voice": voice,
        "format": "mp3",
        "sample_rate": 22050,
    }
    if style:                      # 方言/情感/角色控制（部分模型支持）
        inp["instruction"] = style
    payload = {"model": model, "input": inp}
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "X-TTS-TN": "enable",     # 文本归一化（数字/符号念对）；取值必须精确等于 enable
    }
    try:
        resp = requests.post(base + "/v1/tts/synthesize", headers=headers, json=payload, timeout=120)
    except Exception as e:
        return None, f"VoiceStudio 调用异常：{e}"
    if resp.status_code != 200:
        body = (resp.text or "")[:300]
        if resp.status_code in (401, 403):
            return None, f"VoiceStudio 鉴权失败（{resp.status_code}）：VOICE_STUDIO_API_KEY 无效 —— {body}"
        return None, f"VoiceStudio 返回 {resp.status_code}：{body}"
    try:
        data = resp.json()
    except Exception as e:
        return None, f"VoiceStudio 响应解析失败：{e}"
    url = (((data.get("output") or {}).get("audio") or {}).get("url")) or ""
    if not url:
        return None, "VoiceStudio 未返回音频地址：" + str(data)[:200]
    try:
        r2 = requests.get(url, timeout=120)
        if r2.status_code != 200:
            return None, f"VoiceStudio 音频下载失败（{r2.status_code}）"
        return r2.content, ""
    except Exception as e:
        return None, f"VoiceStudio 音频下载异常：{e}"


def _tts_backend_key_ok():
    """当前聊天语音后端是否配置了对应的 API Key。"""
    backend = load_voice_config().get("tts_backend") or "mimo"
    if backend == "voice_studio":
        return bool(CONFIG.get("VOICE_STUDIO_API_KEY"))
    return bool(CONFIG.get("MIMO_API_KEY"))


def _tts_backend_missing_msg():
    backend = load_voice_config().get("tts_backend") or "mimo"
    if backend == "voice_studio":
        return "语音功能还没配置 VOICE_STUDIO_API_KEY（阿里云 Voice Studio），念不出来～"
    return "语音功能还没配置 MIMO_API_KEY，念不出来～"


    # ---------- 小忺专属音色：voicedesign 造一次样音 -> voiceclone 长期复刻（音色恒定）----------
# 样音台词要「贴合音色」（官方建议：温柔音色配温柔独白），所以这里用小忺自我介绍。
_VOICE_SAMPLE_TEXT = ("嗨，我是小忺～远旅把我带到了这里。"
                      "今天也一起聊聊天吧，我在这儿陪着你。")
_VOICE_SAMPLE_PATH = os.path.join(BASE_DIR, "assets", "voice_xiaoxin.wav")
# 每次造音色用的描述文本落盘一份，方便核对「这次到底按什么人设生成的」
_VOICE_DESC_FILE = os.path.join(BASE_DIR, "assets", "voice_design_last.txt")


def _load_persona_fresh():
    """跳过缓存、直接重读 persona.json（造音色时用），并顺带刷新缓存。

    人设可能随时在改，造音色必须拿到「此刻」的人设，不能拿旧缓存。
    """
    global _persona_cache
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            try:
                _persona_cache["data"] = data
                _persona_cache["mtime"] = os.path.getmtime(PERSONA_FILE)
            except OSError:
                pass
            return data
    except Exception as e:
        print(f"[人设] 重读 persona.json 失败，改用缓存：{e}")
    return load_persona()


def _voice_design_desc(force_persona=False):
    """返回 (音色描述文本, 来源说明)。

    只写「人本身」的特征：性别年龄 / 音色质感 / 语气 / 语速 / 说话习惯；
    不写混响、EQ 等后期效果词，不写互相矛盾的特征。

    force_persona=True：忽略 voice_config.json 里的手动覆盖值，**强制按当前 persona.json
    重新生成**（/造音色 走这条，保证人设改了音色描述也跟着改）。
    """
    override = (load_voice_config().get("voice_design_prompt") or "").strip()
    if override and not force_persona:
        return override, "手动覆盖（voice_config.json 的 voice_design_prompt）"
    p = _load_persona_fresh()          # 每次都重读，人设一改立刻反映
    p = p if isinstance(p, dict) else {}
    style = p.get("说话风格") if isinstance(p.get("说话风格"), dict) else {}
    char = p.get("性格") if isinstance(p.get("性格"), dict) else {}
    tone = str(style.get("语气") or "轻松、亲昵、像友好的朋友")
    traits = char.get("核心特质") or []
    traits_s = "、".join(str(t) for t in traits[:4]) if traits else "温柔、真诚"
    # 年龄/性别直接取自人设（身份.年龄 / 身份.性别），决定音色年龄段
    ident = p.get("身份") if isinstance(p.get("身份"), dict) else {}
    try:
        age = int(str(ident.get("年龄") or p.get("年龄") or 18).strip())
    except Exception:
        age = 18
    gender = str(ident.get("性别") or p.get("性别") or "女").strip()
    if age <= 17:
        who = f"{age}岁的少女" if gender == "女" else f"{age}岁的少年"
        quality = "嗓音清亮明亮，带一点稚嫩与朝气"
    elif age <= 24:
        who = f"{age}岁的少女" if gender == "女" else f"{age}岁的青年"
        quality = "嗓音清亮甜美，带一点青春朝气，干净温柔"
    else:
        who = f"{age}岁的年轻女性" if gender == "女" else f"{age}岁的年轻男性"
        quality = "嗓音清亮柔和，干净稳重"
    desc = (
        f"{who}，{quality}；"
        f"整体语气{tone}；性格{traits_s}；"
        "语速适中、轻快不拖沓，咬字轻巧自然，句尾偶尔轻轻上扬，像在和朋友闲聊；"
        "情绪表达细腻但不夸张，从不说教，听上去亲切、让人放松。"
    )
    return desc, f"按人设生成（{who}，语气「{tone}」，特质「{traits_s}」）"


def _build_voice_design_prompt(force_persona=False):
    """只要描述文本（兼容旧调用）：= _voice_design_desc(force_persona)[0]。"""
    return _voice_design_desc(force_persona=force_persona)[0]


def _make_voice_sample(save_path=None, force_persona=False):
    """用 voicedesign 按小忺「当前」人设合成一段样音并存盘，同时写进语音配置。返回 (path, err)。

    force_persona=True：忽略手动覆盖值，强制按 persona.json 重新生成描述（/造音色 用）。
    """
    key = CONFIG.get("MIMO_API_KEY") or ""
    if not key:
        return None, "未配置 MIMO_API_KEY"
    base = (CONFIG.get("MIMO_BASE_URL") or "https://api.xiaomimimo.com/v1").rstrip("/")
    desc, _src = _voice_design_desc(force_persona=force_persona)
    path = save_path or _VOICE_SAMPLE_PATH
    audio, err = _tts_post(
        base, key, "mimo-v2.5-tts-voicedesign",
        [{"role": "user", "content": desc},
         {"role": "assistant", "content": _VOICE_SAMPLE_TEXT}],
        {"format": "wav"},
    )
    if not audio:
        return None, err or "音色合成失败"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(audio)
    except Exception as e:
        return None, f"样音保存失败：{e}"
    try:
        rel = os.path.relpath(path, BASE_DIR).replace("\\", "/")
    except ValueError:
        rel = path.replace("\\", "/")   # 跨盘符兜底
    save_voice_config({"voice_sample": rel, "voice_sample_mime": "audio/wav"})
    _voice_sample_cache.update({"path": None, "mtime": -1, "data_url": None})
    return path, None


_MAKE_VOICE_CMDS = {"造音色", "生成音色", "定制音色", "音色生成"}


def _is_make_voice_cmd(content):
    b = content.lstrip("/").split()[0].lower() if content else ""
    return b in _MAKE_VOICE_CMDS


async def _handle_make_voice(message, content):
    """@小忺 /造音色 [重置]：按「当前」人设重新生成音色描述并合成样音。

    每次都重新读 persona.json（不走缓存、忽略旧的手动覆盖值），人设改了音色就跟着改。
    带「重置」时还会清掉 voice_config.json 里的 voice_design_prompt，之后也一直按人设走。
    """
    arg = ""
    parts = (content or "").split(None, 1)
    if len(parts) > 1:
        arg = parts[1].strip().lower()
    if arg in ("重置", "重来", "清覆盖", "按人设", "重新生成", "重置覆盖"):
        save_voice_config({"voice_design_prompt": ""})   # 清掉手动覆盖值，彻底回到按人设
        await _reply(message, content=_with_prefix("好，先把手动写的音色描述清掉，重新按人设来～"))
    elif arg:
        await _reply(message, content=_with_prefix(
            "这个指令不带参数，想清掉手动覆盖值就发「/造音色 重置」～我按人设重新来一遍"))
        return
    await _reply(message, content=_with_prefix("按我的人设重新捏一个专属音色，稍等一下下～"))
    desc, src = _voice_design_desc(force_persona=True)   # 强制按最新人设重新生成
    print(f"[音色] 本次音色描述（{src}）：{desc}")
    path, err = await asyncio.to_thread(_make_voice_sample, None, True)
    if err:
        await _reply(message, content=_with_prefix(f"音色没造出来：{err}"))
        return
    try:
        with open(_VOICE_DESC_FILE, "w", encoding="utf-8") as f:
            f.write(f"来源：{src}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{desc}\n")
    except Exception:
        pass
    await _reply(message, content=_with_prefix(
        f"好啦，专属音色造好了～\n音色描述：{desc}\n来源：{src}\n"
        f"以后聊天都用这个声音啦，先给你听一句 (｡•ᴗ•｡)"))
    vok = await _maybe_speak(message, _VOICE_SAMPLE_TEXT, mtype="chat", style=None, plan="both")
    if vok is not True:
        await _reply(message, content=_with_prefix(f"（试听没发出去：{vok}）"))


# ---------- 用「自己的音频文件」复刻音色（voiceclone）----------
_IMPORT_VOICE_CMDS = {"导入音色", "倒入音色", "用音色", "设置音色", "音色文件", "训练音色"}


def _is_import_voice_cmd(content):
    b = content.lstrip("/").split()[0].lower() if content else ""
    return b in _IMPORT_VOICE_CMDS


# 音频只认这一个目录：assets/训练音频（用户自己把文件放进来）——绝不读取项目外的任何文件
_VOICE_TRAIN_DIR = os.path.join(BASE_DIR, "assets", "训练音频")


def _safe_path_within(path, parent):
    """path 是否确实位于 parent 之内（防 ../ 越界、防绝对路径指向外面）。"""
    try:
        p = os.path.realpath(path)
        q = os.path.realpath(parent)
        return os.path.commonpath([p, q]) == q
    except Exception:
        return False


def _list_train_audio():
    """列出 assets/训练音频 下的 wav/mp3 —— 只扫这一个目录，不去别处。"""
    d = _VOICE_TRAIN_DIR
    if not os.path.isdir(d):
        return []
    out = []
    try:
        for n in sorted(os.listdir(d)):
            if os.path.splitext(n)[1].lower() in (".wav", ".mp3"):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    out.append(p)
    except Exception as e:
        print(f"[音色] 读取训练音频目录失败：{e}")
    return out


def _adopt_voice_sample(path, max_bytes=7 * 1024 * 1024):
    """登记放进项目的音频作为音色样本 —— **只读校验，绝不改动文件**。

    明确不做：不复制、不转码、不压缩、不覆盖、不移动（用户要求机器人不动他的文件）。
    只读项目内的文件：`path` 必须位于 BASE_DIR 之内，否则直接拒绝（不读项目外的文件）。
    检查项：存在 / 是 wav 或 mp3 / 体积不超限（base64 后须 ≤10MB）。
    成功返回 (path, None)，失败返回 (None, err)。
    """
    if not path or not _safe_path_within(path, BASE_DIR):
        return None, "这个文件不在项目里，我不读项目外的东西～（把音频放进 assets/训练音频 再用）"
    if not os.path.isfile(path):
        return None, "找不到这个文件～把音频放进 assets/训练音频 里再叫我"
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".wav", ".mp3"):
        return None, (f"只能用 wav 或 mp3，你这个是 {ext}～"
                      f"你自己转好再放进来吧（我不改动你的文件）")
    try:
        size = os.path.getsize(path)
    except Exception as e:
        return None, f"读不了这个文件：{e}"
    if size == 0:
        return None, "文件是空的呀"
    if size > max_bytes:
        return None, (f"文件 {size // 1024}KB 偏大（编码后不能超过 10MB）～"
                      f"换一段 5~15 秒、小于 7MB 的吧（我不帮你压缩，你自己挑一段）")
    try:
        rel = os.path.relpath(path, BASE_DIR).replace("\\", "/")
    except ValueError:
        rel = path.replace("\\", "/")   # 跨盘符时 relpath 会报错，直接存绝对路径
    save_voice_config({
        "voice_sample": rel,
        "voice_sample_mime": "audio/wav" if ext == ".wav" else "audio/mpeg",
    })
    _voice_sample_cache.update({"path": None, "mtime": -1, "data_url": None})
    return path, None


async def _handle_import_voice(message, content):
    """@小忺 /导入音色 <音频文件路径>：用这份音频复刻成小忺的音色（voiceclone）。"""
    parts = content.split(None, 1)
    arg = (parts[1].strip() if len(parts) > 1 else "").strip().strip('"').strip("'")
    if arg in ("训练音频", "目录", "列表", "list"):
        arg = ""   # 把目录名当参数给了 → 当成不带参数，直接列出目录里的音频
    try:
        os.makedirs(_VOICE_TRAIN_DIR, exist_ok=True)
    except Exception:
        pass
    if arg:
        # 只接受**纯文件名**：带分隔符/盘符/.. 的一律拒绝，避免指向项目外的文件
        if any(ch in arg for ch in ("\\", "/", ":")) or ".." in arg or arg in (".", ".."):
            await _reply(message, content=_with_prefix(
                "只给我文件名就好啦～音频要放在 assets/训练音频 里，"
                "不要给完整路径（我不读项目外的文件）"))
            return
        path = os.path.join(_VOICE_TRAIN_DIR, arg)
        if not _safe_path_within(path, _VOICE_TRAIN_DIR):
            await _reply(message, content=_with_prefix("这个名字不太对劲，换一个吧～"))
            return
    else:
        files = _list_train_audio()
        if not files:
            await _reply(message, content=_with_prefix(
                f"assets/训练音频 里还没有音频～你把 wav/mp3 放进去再叫我"
                f"（5~15 秒的清晰人声效果最好）"))
            return
        if len(files) > 1:
            names = "、".join(os.path.basename(f) for f in files)
            await _reply(message, content=_with_prefix(
                f"里面有好几份：{names}\n用 /导入音色 <文件名> 指定要用哪一个～"))
            return
        path = files[0]
    await _reply(message, content=_with_prefix("收到～我学学这个声音，稍等一下下 (｡•ᴗ•｡)"))
    dst, err = await asyncio.to_thread(_adopt_voice_sample, path)
    if err:
        await _reply(message, content=_with_prefix(err))
        return
    kb = os.path.getsize(dst) // 1024
    await _reply(message, content=_with_prefix(
        f"音色登记好啦～用的就是 {os.path.basename(dst)}（{kb}KB，原文件没动过）"
        f"以后我都用这个声音说话，先给你听一句 (｡•ᴗ•｡)"))
    vok = await _maybe_speak(message, _VOICE_SAMPLE_TEXT, mtype="chat", style=None, plan="both")
    if vok is not True:
        await _reply(message, content=_with_prefix(f"（试听没发出去：{vok}）"))


# ---------- /认领：人工确认「你 @ 的那个 id 就是我」----------
# 自动识别依赖 mentions 里的 is_you/username，某些代理环境下这两个字段取不到 → 永远学不会自己的 id。
# 这条命令由用户亲手 @ 一下来确认，100% 可靠且不会认错到别的机器人（小鱼），学完立即落盘。
_CLAIM_CMDS = {"认领", "认领id", "这是你", "记住id", "绑定id", "这是你的id"}


def _is_claim_cmd(content):
    b = content.lstrip("/").split()[0].lower() if content else ""
    return b in _CLAIM_CMDS


async def _handle_claim(message, content, bot, group_id=None):
    """@小忺 /认领：把本条消息开头 @ 的 openid 记成小忺自己并落盘。"""
    oid = _leading_at_openid(getattr(message, "content", "") or "")
    if not oid:
        await _reply(message, content=_with_prefix(
            "要先 @我 再发 /认领 哦～这样我才知道哪个 id 是我 (｡•ᴗ•｡)"))
        return
    try:
        bot._learn_bot_openid(group_id, oid)
    except Exception as e:
        await _reply(message, content=_with_prefix(f"没记住：{e}"))
        return
    await _reply(message, content=_with_prefix(
        f"记住啦～我在这儿是 {oid[:8]}… 以后你 @我 我就能认出来，重启也不会忘 (｡•ᴗ•｡)"))


def _spoken_text(reply):
    """去掉前缀和开头 @某人，得到适合念出来的纯文本。"""
    prefix = CONFIG.get("REPLY_PREFIX") or ""
    t = reply or ""
    if prefix and t.startswith(prefix):
        t = t[len(prefix):]
    t = re.sub(r"^@\S+\s+", "", t)
    return t.strip()


def _clean_story(text):
    """讲故事时去除前缀、@、以及「好的，给你讲个故事：」这类引导语，只留故事正文。"""
    t = _spoken_text(text)
    t = re.sub(r"^\s*【[^】]{0,20}】\s*", "", t)  # 去掉残留的【bot】之类标签
    t = re.sub(
        r"^\s*(?:好[的啊]?|来|那|嗯|行|好滴)?\s*[，,。、]?\s*"
        r"(?:我?\s*(?:给\s*你\s*)?)?"
        r"(?:讲个?故事|说个?故事|听个?故事|听好了|讲个?笑话|说个?笑话|来个?笑话|来个?段子)"
        r"\s*(?:吧|呀|哈|哦|呢)?\s*"
        r"[：:：，,。—\-~～\n]?\s*",
        "", t, count=1,
    )
    return t.strip()


def _split_for_tts(text, maxc):
    """按句末标点切分，避免截断句子；超长单句硬切。返回片段列表。"""
    segs = re.split(r"(?<=[。！？!?；;\n])", text)
    chunks, cur = [], ""
    for s in segs:
        if not s.strip():
            continue
        if len(cur) + len(s) <= maxc:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            while len(s) > maxc:
                chunks.append(s[:maxc])
                s = s[maxc:]
            cur = s
    if cur:
        chunks.append(cur)
    return chunks or [text[:maxc]]


def _voice_ctx(message, scene=None):
    """提取语音拟人化所需的上下文信号（字段缺失一律容错，不阻断发语音）。"""
    ctx = {"scene_hint": scene}
    try:
        ctx["is_at"] = bool(getattr(message, "_mentioned", False)
                            or getattr(message, "_is_name_mention", False))
    except Exception:
        ctx["is_at"] = False
    ctx["is_private"] = not bool(getattr(message, "group_openid", None))
    try:
        oid = getattr(getattr(message, "author", None), "user_openid", None)
        ctx["is_owner"] = bool(oid and oid == _OWNER_OPENID)
    except Exception:
        ctx["is_owner"] = False
    try:
        recent = list(_SEEN.values())[-5:] if isinstance(_SEEN, dict) else list(_SEEN)[-5:]
        ctx["recent_texts"] = [x.get("text", "") for x in recent
                               if isinstance(x, dict) and isinstance(x.get("text", ""), str)]
    except Exception:
        ctx["recent_texts"] = []
    return ctx


async def _send_voice(message, text, style=None, scene=None):
    """把文字 TTS 成语音并当语音消息(file_type=3)发出；长文本自动分段发送。

    返回 True 表示成功；失败返回可读的错误字符串（自身不主动回复，由调用方决定怎么提示）。
    style: 可选情绪描述（覆盖 voice_config.json 里的 style），用于 mimo-v2.5-tts-voicedesign 模型。
    scene: 场景提示（chat/proactive/story/greeting…），交给语音拟人化模块做情绪判别。
    """
    maxc = int(load_voice_config().get("max_chars", CONFIG.get("VOICE_MAX_CHARS") or 150))
    spoken = _spoken_text(text)
    if not spoken:
        return "没有可念的文字"
    # 语音拟人化：场景情绪识别 → 多标签 → 风格指令/语速；显式指定的情绪默认保留
    _vp = None
    if voice_persona is not None:
        try:
            _vp = voice_persona.resolve(spoken, mtype=scene,
                                        ctx=_voice_ctx(message, scene),
                                        base_style=style)
            if _vp and _vp.debug:
                print(f"[语音拟人化] {_vp.debug}")
        except Exception as e:
            print(f"[语音拟人化] 判别失败，回退原语音逻辑：{e}")
            _vp = None
    if _vp and _vp.style:
        style = _vp.style
    chunks = _split_for_tts(spoken, maxc)
    api = BOT_API
    if api is None:
        return "机器人还在启动中，稍后再试"
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    for ch in chunks:
        backend = load_voice_config().get("tts_backend") or "mimo"
        if backend == "voice_studio":
            audio, err = await asyncio.to_thread(_tts_voice_studio, ch, style=style)
        else:
            audio, err = await asyncio.to_thread(_tts_mimo, ch, style=style)
        if not audio:
            return err or "未知错误"
        # 输出音效后处理（氦气/低沉等）；无 ffmpeg 时原样返回
        _eff = (_vp.effect if (_vp and _vp.effect) else None) or load_voice_config().get("effect") or "none"
        if _eff and _eff != "none":
            audio, ext = await asyncio.to_thread(_apply_audio_effect, audio, _eff)
        else:
            ext = "mp3"
        _spd = float(load_voice_config().get("voice_speed") or 1.0)
        if _vp and _vp.speed_ratio:
            _spd *= float(_vp.speed_ratio)
        if abs(_spd - 1.0) >= 0.01:
            audio, ext = await asyncio.to_thread(_apply_audio_speed, audio, _spd)
        try:
            fi = await _get_file_info(api, group_openid, openid, 3,
                                      data=audio, file_name=f"tts.{ext}")
            if not fi:
                return "语音文件上传失败"
            await _reply(message, media={"file_info": fi}, msg_type=7)
        except Exception as e:
            err = f"语音发送失败：{e}"
            print(f"[语音] {err}")
            return err
    return True


SING_MAX_CHARS = 120   # 单条唱歌文本上限：尽量整首一次合成，保证旋律连贯


async def _send_song(message, lyrics, voice=None, style=None):
    """把歌词用 MiMo 唱歌模式合成成语音并当语音消息(file_type=3)发出。

    关键：唱歌必须整首一次性合成，否则每段独立推理、旋律会断裂成碎片。
    仅当歌词超过单条上限(SING_MAX_CHARS)才按乐句（逗号/句号/换行）切成短句，每句单独成「一句歌」。
    返回 True 表示成功；失败返回可读的错误字符串。"""
    spoken = _spoken_text(lyrics)
    if not spoken:
        return "没有可唱的内容"
    if len(spoken) <= SING_MAX_CHARS:
        chunks = [spoken]                                  # 整首一首，旋律连贯
    else:
        # 超长：按乐句切分（保留每句的标点感），避免 120 字硬切破坏节奏
        chunks = [c.strip() for c in re.split(r"[，,。！？!?；;\n]+", spoken) if c.strip()]
        if not chunks:
            chunks = [spoken[:SING_MAX_CHARS]]
    api = BOT_API
    if api is None:
        return "机器人还在启动中，稍后再试"
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    for ch in chunks:
        audio, err = await asyncio.to_thread(_tts_mimo, ch, style=style, sing=True, voice=voice)
        if not audio:
            return err or "未知错误"
        try:
            fi = await _get_file_info(api, group_openid, openid, 3,
                                      data=audio, file_name="song.mp3")
            if not fi:
                return "歌曲文件上传失败"
            await _reply(message, media={"file_info": fi}, msg_type=7)
        except Exception as e:
            err = f"歌曲发送失败：{e}"
            print(f"[唱歌] {err}")
            return err
    return True


def _make_song_lyrics(theme, group_id, persona):
    """按小忺人设现编一首规整、适合演唱的中文短歌（主题=用户给的词/歌名；空则随机可爱主题）。
    返回纯歌词文本（已清理干扰 MiMo 标签的括号符号，并截断到单条合成长度内以保证旋律连贯）。"""
    name = persona.get("名字") or "小忺"
    topic = theme or "今天心情不错、天气很好"
    prompt = (
        f"你是 QQ 机器人「{name}」，性格人设见下。请写一首**完整、规整的中文短歌歌词**"
        f"（不是散文、不是普通句子），主题围绕「{topic}」。要求：\n"
        f"1. 4~6 句，每句 6~14 字，整首不超过 90 字；\n"
        f"2. 押韵、有节奏感，像一首可爱的少女小调（最好有重复的副歌句，每句结尾押同一个韵）；\n"
        f"3. 只输出歌词本身，不要引号、括号、编号、任何说明性文字。\n\n"
        f"【小忺人设】{json.dumps(persona, ensure_ascii=False)[:800]}"
    )
    try:
        txt = get_answer(prompt, group_id) or ""
    except Exception:
        txt = ""
    if not txt:
        return f"{topic}，啦啦啦，今天真开心～"
    txt = re.sub(r"[\n]{2,}", "\n", txt).strip()
    # 去掉会干扰 MiMo 标签的括号（(唱歌)/(情绪) 之类会被当标签）
    for a, b in (("（", " "), ("）", " "), ("(", " "), (")", " "),
                 ("[", " "), ("]", " "), ("{", " "), ("}", " ")):
        txt = txt.replace(a, b)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    # 超长则按句末标点截断，避免整首合成超上限被强制分段、破坏旋律
    if len(txt) > SING_MAX_CHARS - 4:
        cut = re.split(r"(?<=[。！？!?])", txt)
        joined = ""
        for c in cut:
            if len(joined) + len(c) > SING_MAX_CHARS - 4:
                break
            joined += c
        txt = joined.strip() or txt[:SING_MAX_CHARS - 4]
    return txt or f"{topic}，啦啦啦～"


async def _handle_sing(message, content, group_id=None):
    """唱歌：@我 /唱 <歌词或歌名>。带歌词就直接唱；带歌名/主题就现编一段唱给你。"""
    arg = re.sub(r"^(?:/唱|/唱歌|/sing)\s*", "", content).strip()
    if not (CONFIG.get("MIMO_API_KEY") or ""):
        await _reply(message, content=_with_prefix("语音功能还没配置 MIMO_API_KEY，唱不了～"))
        return
    persona = load_persona()
    # 唱歌只能用预置音色（voiceclone 复刻的小忺音色不支持唱歌，_tts_mimo 会自动回退）
    _vc = load_voice_config()
    _sv = _vc.get("sing_voice") or _vc.get("voice") or "茉莉"
    if _sv not in _MIMO_VOICES:
        _sv = "茉莉"
    clone_note = f"（用「{_sv}」音色唱的哦，唱歌暂时用不了我的专属音色～）"
    if not arg:
        await _reply(message, content=_with_prefix("好呀～那我现编一段唱给你听 🎤" + clone_note))
        lyrics = _make_song_lyrics("", group_id, persona)
    else:
        # 像歌词（多行/带句号/较长且有逗号）就直接唱；否则当主题现编
        looks_like_lyrics = ("\n" in arg) or ("。" in arg) or (len(arg) > 30 and ("，" in arg or "," in arg))
        if looks_like_lyrics:
            lyrics = arg
        else:
            await _reply(message, content=_with_prefix(f"「{arg}」是吧～ 我现编一段唱给你 🎤" + clone_note))
            lyrics = _make_song_lyrics(arg, group_id, persona)
    res = await _send_song(message, lyrics)
    if res is not True:
        await _reply(message, content=_with_prefix(str(res)[:120]))


def _voice_plan(mtype):
    """语音发送方案：'none'=不发语音 / 'both'=文字+语音 / 'voice_only'=只发语音不带文字。

    决策顺序：开关 -> 消息类型 -> 触发频率(frequency) -> 语音独占概率(voice_only_chance)。
    返回的方案由调用方统一决定「是否发文字 + 是否发语音」，避免重复掷骰导致两者不一致。
    """
    cfg = load_voice_config()
    if not cfg.get("enabled"):
        return "none"
    if not cfg.get("message_types", {}).get(mtype, False):
        return "none"
    if random.random() > float(cfg.get("frequency", 0.5)):
        return "none"
    if random.random() < float(cfg.get("voice_only_chance", 0.0)):
        return "voice_only"
    return "both"


async def _push_voice(group_openid, text, style=None):
    """定时道安专用：把文字 TTS 成语音，直接推送到群（无 source message）。返回 True/错误串。"""
    maxc = int(load_voice_config().get("max_chars", CONFIG.get("VOICE_MAX_CHARS") or 150))
    spoken = _spoken_text(text)
    if not spoken:
        return "没有可念的文字"
    # 道安走 greeting 场景；无 source message，故手工构造最小上下文
    _vp = None
    if voice_persona is not None:
        try:
            _vp = voice_persona.resolve(
                spoken, mtype="greeting",
                ctx={"scene_hint": "greeting", "is_private": False, "is_at": False,
                     "is_owner": False, "recent_texts": []},
                base_style=style)
            if _vp and _vp.debug:
                print(f"[语音拟人化] {_vp.debug}")
        except Exception as e:
            print(f"[语音拟人化] 判别失败，回退原语音逻辑：{e}")
            _vp = None
    if _vp and _vp.style:
        style = _vp.style
    chunks = _split_for_tts(spoken, maxc)
    api = BOT_API
    if api is None:
        return "机器人还在启动中，稍后再试"
    for ch in chunks:
        # 与其他语音入口保持一致：读 tts_backend，不再硬编码 MiMo
        backend = load_voice_config().get("tts_backend") or "mimo"
        if backend == "voice_studio":
            audio, err = await asyncio.to_thread(_tts_voice_studio, ch, style=style)
        else:
            audio, err = await asyncio.to_thread(_tts_mimo, ch, style=style)
        if not audio:
            return err or "未知错误"
        _eff = (_vp.effect if (_vp and _vp.effect) else None) or load_voice_config().get("effect") or "none"
        if _eff and _eff != "none":
            audio, ext = await asyncio.to_thread(_apply_audio_effect, audio, _eff)
        else:
            ext = "mp3"
        _spd = float(load_voice_config().get("voice_speed") or 1.0)
        if _vp and _vp.speed_ratio:
            _spd *= float(_vp.speed_ratio)
        if abs(_spd - 1.0) >= 0.01:
            audio, ext = await asyncio.to_thread(_apply_audio_speed, audio, _spd)
        try:
            fi = await _get_file_info(api, group_openid, None, 3,
                                      data=audio, file_name=f"tts.{ext}")
            if not fi:
                return "语音文件上传失败"
            await api.post_group_message(group_openid=group_openid,
                                         media={"file_info": fi}, msg_type=7)
        except Exception as e:
            err = f"语音发送失败：{e}"
            print(f"[语音] {err}")
            return err
    return True


async def _maybe_speak(message, text, mtype="chat", style=None, plan=None):
    """给回复补一条语音（按 voice_config 的 voice_only 设置，可能只发语音不带文字）。

    mtype: chat/proactive/story/command。style: 可选情绪描述。
    plan: 调用方用 _voice_plan(mtype) 预先算好的方案，避免重复掷骰导致文字/语音不一致；
          不传则自行重新计算（用于单点调用场景）。
    返回 True 表示语音发送成功，否则返回错误串（供调用方在 voice_only 时回退文字）。
    """
    if plan is None:
        plan = _voice_plan(mtype)
    if plan == "none":
        return True
    res = await _send_voice(message, text, style=style, scene=mtype)
    if res is not True:
        print(f"[语音] 自动语音补发未成功：{res}")
    return res


async def _handle_say(message, content):
    """按需语音：@我 /说 <文本> 让机器人念出来。"""
    text = re.sub(r"^(?:/说|/say)\s*", "", content).strip()
    if not text:
        await _reply(message, content=_with_prefix("说点什么呀？例如：@我 /说 今天天气真好"))
        return
    if not _tts_backend_key_ok():
        await _reply(message, content=_with_prefix(_tts_backend_missing_msg()))
        return
    res = await _send_voice(message, text)
    if res is not True:
        await _reply(message, content=_with_prefix(str(res)[:120]))


def _cmd_voice(args=None, ctx=None):
    """开关语音模式：开启后按 voice_config.json 的频率与消息类型发语音。"""
    cfg = load_voice_config()
    new_on = not cfg.get("enabled")
    save_voice_config({"enabled": new_on})
    if new_on and not _tts_backend_key_ok():
        return ("已开启语音模式，但当前 TTS 后端（"
                + (load_voice_config().get("tts_backend") or "mimo")
                + "）还没配置对应的 API Key～ 先在 config.json 填好 key 再试")
    return "语音模式已" + ("开启 🎙️ 满足频率/类型的回复会发语音（按 voice_only 设置可能只发语音不带文字）" if new_on
                      else "关闭（恢复正常文字回复）")


def _cmd_voice_set(args=None, ctx=None):
    """切换 MiMo 预置音色：/音色 <名称>；不带参数则列出可选并回显当前。"""
    want = (args or "").strip()
    if not want:
        return ("当前音色：" + load_voice_config().get("voice") + "\n可选音色：" + "、".join(_MIMO_VOICES)
                + "\n用法：/音色 茉莉（或 Mia / Dean 等）")
    # 忽略大小写匹配（英文音色首字母大写，中文直接比）
    match = next((v for v in _MIMO_VOICES if v.lower() == want.lower()), None)
    if match is None:
        return "没有这个音色～ 可选：" + "、".join(_MIMO_VOICES) + "\n用法：/音色 茉莉"
    save_voice_config({"voice": match})
    return f"音色已切换为「{match}」🎚️"


def _cmd_sing_voice(args=None, ctx=None):
    """切换唱歌专用预置音色（独立于聊天音色）：/唱歌音色 <名称>；不带参数列出可选并回显当前。"""
    cfg = load_voice_config()
    want = (args or "").strip()
    if not want:
        cur = cfg.get("sing_voice") or "茉莉"
        if cur not in _MIMO_VOICES:   # 容错：万一存了非法值，回退显示
            cur = "茉莉"
        return ("当前唱歌音色：" + cur + "\n可选预置音色：" + "、".join(_MIMO_VOICES)
                + "\n用法：/唱歌音色 茉莉（唱歌专用，不影响聊天的小忺音色）")
    match = next((v for v in _MIMO_VOICES if v.lower() == want.lower()), None)
    if match is None:
        return "没有这个音色～ 可选预置：" + "、".join(_MIMO_VOICES) + "\n用法：/唱歌音色 茉莉"
    save_voice_config({"sing_voice": match})
    return f"唱歌音色已切换为「{match}」🎶（仅用于唱歌，不影响聊天音色）"


def _cmd_tts_engine(args=None, ctx=None):
    """切换聊天语音后端：/tts引擎 mimo | voice_studio。不带参显示当前。"""
    want = (args or "").strip().lower()
    cur = load_voice_config().get("tts_backend") or "mimo"
    if not want:
        return (f"当前聊天语音后端：{cur}\n可选：mimo（复刻小忺/预置）| voice_studio（阿里云 Voice Studio）\n"
                "用法：/tts引擎 voice_studio（唱歌仍走 MiMo）")
    if want not in ("mimo", "voice_studio"):
        return "未知后端～ 可选：mimo | voice_studio"
    save_voice_config({"tts_backend": want})
    note = "" if _tts_backend_key_ok() else "（注意：该后端对应的 API Key 还没配置，念不出来）"
    return f"聊天语音后端已切换为「{want}」🔁" + note


def _cmd_vs_voice(args=None, ctx=None):
    """设置 voice_studio 后端用的音色 ID：/说话音色 <voice_id>；不带参显示当前。"""
    want = (args or "").strip()
    cur = load_voice_config().get("voice_studio_voice") or "longxiaochun_v2"
    if not want:
        return f"当前 voice_studio 音色：{cur}\n用法：/说话音色 longxiaochun_v2（填你的系统/复刻音色 ID）"
    save_voice_config({"voice_studio_voice": want})
    return f"voice_studio 音色已设为「{want}」🎚️（仅 voice_studio 后端生效）"


def _cmd_member(args=None, ctx=None):
    """群友身份档案：记住/查询某位群友是谁。

    真正的工作在群消息入口拦截完成（需要消息里的 @ 对象）；这里只是兜底/用法说明。
    """
    a = (args or "").strip()
    if a.startswith("群友") or a.startswith("who"):
        return ("查询用法：在群里发「/群友 @某群友」，我会告诉你记下的 TA 的身份；"
                "不带 @ 则列出我认识的群友。")
    return ("记群友用法：在群里 @ 一位群友并介绍 TA，例如\n"
            "  @张三 这是我的同桌小明\n"
            "  /记人 @张三 他是我哥\n"
            "我就会记住 TA 是谁（存到 assets/members.json），以后聊天能用上。")


def _cmd_effect(args=None, ctx=None):
    """切换输出音效：/音效 <名称>；可填 氦气/低沉/加速/减速/关，或英文；不带参列出。"""
    want = (args or "").strip()
    if not want:
        cur = load_voice_config().get("effect") or "none"
        cur_disp = _AUDIO_EFFECTS.get(cur, ("无", ""))[0] if cur != "none" else "无"
        opts = "、".join(f"{v[0]}({k})" for k, v in _AUDIO_EFFECTS.items())
        return f"当前音效：{cur_disp}\n可选音效：{opts}\n用法：/音效 氦气"
    key = _EFFECT_ALIASES.get(want) or _EFFECT_ALIASES.get(want.lower())
    if key is None and want.lower() in _AUDIO_EFFECTS:   # 直接传英文 key
        key = want.lower()
    if key is None:
        opts = "、".join(f"{v[0]}({k})" for k, v in _AUDIO_EFFECTS.items())
        return "没有这个音效～ 可选：" + opts + "\n用法：/音效 氦气"
    save_voice_config({"effect": key})
    if key == "none":
        return "已关闭输出音效（恢复正常声音）🔇"
    return f"输出音效已切换为「{_AUDIO_EFFECTS[key][0]}」🎛️"


def _cmd_emotion(args=None, ctx=None):
    """切换情绪/风格：/情绪 <名称>；用 mimo-v2.5-tts-voicedesign 模型驱动情感。

    可填 开心/温柔/悲伤/愤怒/俏皮/撒娇/严肃/神秘 或英文；/情绪 关 关闭回到默认音色。"""
    want = (args or "").strip()
    if not want:
        cur = load_voice_config().get("style") or ""
        cur_disp = "无（默认音色）" if not cur else cur
        opts = "、".join(_EMOTION_PRESETS.keys())
        return f"当前情绪风格：{cur_disp}\n可选情绪：{opts}\n用法：/情绪 温柔"
    if want.lower() in ("关", "无", "关闭", "none", "off"):
        save_voice_config({"style": ""})
        return "已关闭情绪风格，恢复默认音色 🔇"
    key = _EMOTION_ALIASES.get(want.lower())      # 英文/别名
    if key is None and want in _EMOTION_PRESETS:  # 直接传中文 key
        key = want
    if key is None:
        opts = "、".join(_EMOTION_PRESETS.keys())
        return "没有这个情绪～ 可选：" + opts + "\n用法：/情绪 温柔"
    save_voice_config({"style": _EMOTION_PRESETS[key]})
    return f"情绪风格已设为「{_EMOTION_PRESETS[key]}」💗（用 mimo-v2.5-tts-voicedesign 模型合成）"


# 明确要表情包：「来个表情包」「斗图」「/表情」等
_EMO_WANT_RE = re.compile(
    r"^[/!！]?\s*(表情|表情包|斗图|来个表情|发个表情|发表情|emoticon|emoji)\s*$"
    r"|来[个张点套]?表情包|发[个张点套]?表情包|斗图|表情包来[一套张]|丢[个张]?表情")
# 情绪/整活话术 -> 甩个表情包当反应，更像活人
_EMO_REACTION_RE = re.compile(
    r"笑死|哈哈哈|绝了|无语|服了|离谱|厉害|牛|开心|难过|生气|想哭|好累|好困|好饿|整活|整点活|绷不住")
_EMO_LINES = ["给你～", "接住～", "这个很贴切哈哈", "喏～", "拿去用～"]


def _wants_emoticon(content):
    """用户是否明确要一张表情包。"""
    return bool(content) and bool(_EMO_WANT_RE.search(content.strip()))


async def _send_emoticon(message):
    """发一张表情包：本地库优先，没库存就现抓一批（原样发送，保留 GIF 动效）。"""
    item = _random_emoticon()
    if not item or not item[1]:
        try:
            await asyncio.to_thread(_safe_crawl_emoticons, 24)
        except Exception:
            pass
        item = _random_emoticon()
    if not item or not item[1]:
        await _reply(message, content=_with_prefix("没扒到表情包，等会儿再试试～"))
        return
    try:
        await _send_static_media(message, random.choice(_EMO_LINES), item)
    except Exception as e:
        await _reply(message, content=_with_prefix(f"表情包发送失败：{str(e)[:80]}"))


async def _send_library_emoticon(message):
    """主动/接话时从本地表情库发一张纯表情（不配文字）。成功返回 True。"""
    item = _random_emoticon()
    if not item or not item[1]:
        return False
    _, path = item
    api = BOT_API
    if api is None:
        return False
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        data = _shrink_for_upload(raw)
        if data is None:
            return False
        fi = await _get_file_info(api, group_openid, openid, 1,
                                  data=data, file_name=os.path.basename(path))
        if not fi:
            return False
        await _reply(message, media={"file_info": fi}, msg_type=7)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[表情] 主动发表情失败：{e}")
        return False


# 对话里提到这些话题时，机器人可能顺带发一张爬来的异环图（"相关的"）
_GAME_RELEVANT_RE = re.compile(
    r"异环|游戏|角色|老婆|立绘|抽卡|卡池|二游|美图|可爱|cos|截图|壁纸|"
    r"来张|发图|表情|帅|好看|图|养眼")


def _is_command(content):
    """判断 content 是否机器人指令（避免给指令回包乱贴图）。"""
    if not content:
        return False
    token = content.lstrip("/").split()[0].lower() if content.strip() else ""
    return token in COMMANDS


# ---------- 发图去重：最近发过的媒体不再重复（动态图按内容哈希，静态图按路径）----------
_recent_media = []   # 环形记录，最多 40 条


def _was_sent(sig):
    return sig in _recent_media


def _mark_sent(sig):
    _recent_media.append(sig)
    if len(_recent_media) > 40:
        del _recent_media[:len(_recent_media) - 40]


def _pick_fresh(items, keyfn=None):
    """从候选里挑一个最近没发过的；全发过就随便挑一个。返回被选中的元素。"""
    if not items:
        return None
    keyfn = keyfn or (lambda x: x)
    fresh = [x for x in items if not _was_sent(keyfn(x))]
    pick = random.choice(fresh) if fresh else random.choice(items)
    _mark_sent(keyfn(pick))
    return pick


def _should_attach_image(content):
    """对话相关话题且概率触发时，从图库挑一张图 (url, path)；否则 None。"""
    if _is_command(content):
        return None
    text = content or ""
    # 拍卖类图只在聊到拍卖时发；角色列表图只在聊配队时发，平时绝不混入
    auction_lib = CONFIG.get("AUCTION_LIB") or []
    if auction_lib and _AUCTION_RE.search(text):
        if random.random() < 0.7:
            return None, _pick_fresh(auction_lib)
        return None
    roster_lib = CONFIG.get("ROSTER_LIB") or []
    if roster_lib and _ROSTER_RE.search(text):
        if random.random() < 0.7:
            return None, _pick_fresh(roster_lib)
        return None
    emo_lib = CONFIG.get("EMOTICON_LIB") or []
    # 情绪/整活话术 -> 甩个表情包当反应，更像活人（不要求话题相关）
    if emo_lib and _EMO_REACTION_RE.search(text) and random.random() < 0.3:
        return None, _pick_fresh(emo_lib)
    lib = CONFIG.get("IMAGE_LIB") or []
    if not lib:
        return None
    if not _GAME_RELEVANT_RE.search(text.lower()):
        return None
    if random.random() > 0.3:  # 偶尔：约 30% 的相关消息带图
        return None
    # 约 50% 概率用表情包当反应，其余用爬来的图
    if emo_lib and random.random() < 0.5:
        return None, _pick_fresh(emo_lib)
    return _lib_item_to_pair(_pick_fresh(lib))


# 用户说「看看/来张 <对象> 的图」时，提取对象作为贴吧搜索关键词，动态爬取相关图
_IMG_NOUNS = {"图", "图片", "表情", "表情包", "立绘", "壁纸", "美图", "cos", "截图", "图图"}
# 这些词被正则抠出来也不该当搜索词：「看看这个图片」= 看刚发的那张（不搜）、「发张美图」= 发张好图（不搜）
_IMG_KW_STOP = {"这", "那", "个", "你", "我", "他", "她", "它", "们", "美", "好", "张",
                "谁", "啥", "哪", "此", "该", "某", "些", "自", "本", "他", "咱", "咱"}
_IMG_FILLER = set("吧呀啊呢哈哦嘛~，。！？!?、,.;；…~")
# 明显不是游戏角色的话题：不走异环/原神论坛，直接走公有图 / 二次元源
_GENERIC_TOPICS = {
    "猫", "狗", "犬", "虎", "兔", "狐", "狼", "狮", "马", "牛", "羊", "猪", "鼠", "鸟", "鱼", "虫",
    "花", "树", "草", "山", "海", "河", "湖", "风景", "天空", "云", "雪", "雨", "月", "星", "太阳",
    "车", "船", "飞机", "建筑", "美食", "食物", "蛋糕", "咖啡", "茶", "城市", "街景",
    "萌", "可爱", "萝莉", "正太", "二次元", "动漫", "动漫图", "二次元图", "壁纸", "头像", "猫娘", "兽耳",
    "动物", "植物", "自然", "宇宙", "星球", "机甲", "科幻", "宠物", "美女", "帅哥",
}
# 含这些词即视为游戏/作品相关，可走论坛
_GAME_NAMES = {"异环", "原神", "崩坏", "星穹铁道", "绝区零", "幻塔", "米哈游", "米游社", "鸣潮"}

# 用户可加游戏前缀决定搜哪个板块：异环 -> 异环论坛(tajiduo)，原神/崩坏… -> 米游社(miyoushe)
_BOARD_SOURCE = {
    "异环": "tajiduo",
    "原神": "miyoushe",
    "崩坏": "miyoushe",
    "星穹铁道": "miyoushe",
    "绝区零": "miyoushe",
    "鸣潮": "miyoushe",
    "幻塔": "miyoushe",
    "米游社": "miyoushe",
    "米哈游": "miyoushe",
}


def _detect_board(content, kw):
    """从「看看异环胡桃 / 看看原神 胡桃」里抠出游戏前缀，决定搜哪个板块；无则 None。"""
    if not content:
        return None
    for g in sorted(_BOARD_SOURCE, key=len, reverse=True):
        # 前缀：动词后紧跟游戏名（可含一个空格），如「看看异环胡桃」「看看 原神 胡桃」
        if re.search(rf"(?:看看|来张|发张|发个|瞅瞅|求|给我|整张|整点|来赏|show)\s*{re.escape(g)}|^\s*{re.escape(g)}", content):
            return g
        # 或抽取出的关键词本身以游戏名开头（异环胡桃 -> 异环）
        if kw and kw.startswith(g):
            return g
    return None


def _looks_like_character(kw):
    """粗略判断关键词是否像「角色名」（应走异环/原神论坛），而非通用话题。"""
    kw = (kw or "").strip()
    if not kw or kw in _GENERIC_TOPICS:
        return False
    if kw in (CONFIG.get("MEMBER_IMAGES") or {}):  # 已登记群员 = 真实人物，按角色处理
        return True
    if any(g in kw for g in _GAME_NAMES):
        return True
    # 两个及以上汉字、且非纯通用词 -> 当作名字
    return len([c for c in kw if "\u4e00" <= c <= "\u9fff"]) >= 2


_IMG_KW_RE = re.compile(
    r"(?:看看|瞅瞅|发张|发个|来张|来个|求|给我|整张|整点|上一张|来一张|发我|来点|甩张|丢张)\s*"
    r"(?:[0-9一二两三四五六七八九十百千万几那这]+)?(?:张|个|只|名|位|幅|枚|条|种)?\s*"
    r"([\u4e00-\u9fff]{1,8}?)(?:的)?\s*(?:图|图片|表情|表情包|立绘|壁纸|美图|cos|截图)"
    r"|(?:看看|瞅瞅|来赏|show)\s*([\u4e00-\u9fff]{1,10})")

# 点名要 cos 图时优先 COS 版块
_COS_RE = re.compile(r"cos|cosplay|coser|真人|扮演", re.I)

# 远旅是机器人主人：用户要看「远旅/成员远旅」的图时，没有他的照片，优雅兜底而非去外网瞎搜
_OWNER_HINT_RE = re.compile(r"远旅|我主人|我佬|我主")

# 抠出来的“关键词”若含这些字，说明是在问问题而不是要看图（如「看看这个怎么做」）
_IMG_BAD_KW_RE = re.compile(
    r"怎么|如何|为什么|为啥|什么|哪些|哪个|怎样|能不能|可不可以|可以|一下|这个|那个|"
    r"有没有|是不是|是不|多少|几点|几天|吗|呢|吧|的$")


def _strip_trailing(s):
    s = (s or "").strip()
    while s and s[-1] in _IMG_FILLER:
        s = s[:-1]
    return s


def _extract_topic_keyword(content):
    """从「看看异环小琰 / 来张赛博图」之类的话里抠出搜索关键词；没有则返回 None。"""
    if not content:
        return None
    m = _IMG_KW_RE.search(content)
    if not m:
        # 正则未命中：交给 NLP(NER) 抽实体作为话题词（未装 jieba 时返回 None，继续回退）
        if topic_ner is not None:
            ner_kw = topic_ner.extract_topic(content)
            if ner_kw:
                return ner_kw
        return None
    kw = _strip_trailing(m.group(1) or m.group(2) or "")
    if not kw or kw in _IMG_NOUNS:
        return None
    # 「看看这个怎么做」这类是在问问题、不是要看图：疑问/动词性关键词一律不算
    if _IMG_BAD_KW_RE.search(kw):
        return None
    # 抽出的是代词/泛词（个/美/这/那…）时不当搜索词，避免去外网瞎搜
    if kw in _IMG_KW_STOP:
        return None
    return kw


# 二级限定词：看看胡桃 泳装 / 看看初音未来 cos —— 主词 + 修饰词一起进搜索，更精准
_IMG_KW_SUB_RE = re.compile(
    r"(?:看看|瞅瞅|发张|发个|来张|来个|求|给我|整张|整点|上一张|来一张|发我|来点|甩张|丢张|来赏)\s*"
    r"(?P<kw>[\u4e00-\u9fffA-Za-z]{1,12}?)\s+(?:\s*的)?(?P<sub>[\u4e00-\u9fffA-Za-z]{1,12})"
    r"(?:\s*(?:图|图片|表情|表情包|立绘|壁纸|美图|cos|截图))?\s*$")


def _extract_topic_query(content):
    """返回 (主词, 二级限定词, 板块)；无二级限定词/板块时对应为 None；不是看图请求返回 None。

    先剥离可能的游戏前缀（异环/原神…），再解析主词+二级限定词，避免「异环」被当成关键词。
    """
    if not content:
        return None
    board = _detect_board(content, None)
    work = content
    if board:
        work = re.sub(rf"{re.escape(board)}\s*", "", content, count=1)
    m = _IMG_KW_SUB_RE.search(work)
    if m:
        kw = _strip_trailing(m.group("kw"))
        sub = _strip_trailing(m.group("sub"))
        # cos 作为二级限定词有意义（看某角色的 cosplay 图），放行；其余纯名词图（图/立绘…）不算限定词
        if sub and (sub not in _IMG_NOUNS or sub == "cos") and sub != "的":
            if (kw and kw not in _IMG_NOUNS and not _IMG_BAD_KW_RE.search(kw)
                    and kw not in _IMG_KW_STOP):
                return (kw, sub, board)
    kw = _extract_topic_keyword(work)
    if kw:
        return (kw, None, board)
    return None


def _build_queries(kw, sub, board=None):
    """按主词+二级限定词拼搜索词：带限定词时一起进查询，提高相关性；板块前缀一并带进查询。"""
    prefix = f"{board} " if board else ""
    if sub:
        return [f"{prefix}{kw} {sub}", f"{prefix}{kw} {sub} 立绘",
                f"{prefix}{kw} {sub} 美图", f"{prefix}{kw} 立绘 {sub}"]
    return [f"{prefix}{kw}", f"{prefix}{kw} 立绘", f"{prefix}{kw} 美图"]


def _crawl_keyword_image(keyword, sub=None, limit=10, prefer=None, board=None):
    """按关键词爬一张「最近没发过」的角色图并规范化（best-effort，失败返回 None）。

    角色类关键词优先用「立绘 / 二创 / 同人 / 表情包 / 美图」等查询词，只从 art + 普通图里挑，
    排除面板 / 道具 / 抽卡 / 拍卖 / 角色列表；再按内容哈希跳过近期发过的。
    prefer='cos' 时优先 COS 版块，'art' 时优先同人图版块。
    board='异环' 只走异环论坛，board='原神'（或其它米哈游游戏）只走米游社。
    """
    from PIL import Image
    kw = (keyword or "").strip()
    _use_anime_fallback = False
    if not kw or _OWNER_HINT_RE.search(kw):  # 远旅是机器人主人，没有他的照片，不瞎搜
        return None
    if _AUCTION_RE.search(kw):
        plans = [("tajiduo", {"auction"}, None, [kw])]
    elif _ROSTER_RE.search(kw):
        plans = [("tajiduo", {"roster"}, None, [kw])]
    else:
        if board == "异环":
            # 明确指定异环：只走异环论坛（tajiduo）
            plans = [("tajiduo", {"art"}, None, _build_queries(kw, sub, board))]
            plans += [("tajiduo", {"", "art"}, None, _build_queries(kw, sub, board))]
            _use_anime_fallback = False
        elif board:
            # 指定其它游戏（原神/崩坏…）：只走米游社对应板块（miyoushe，原神 gids）
            forums = MIYOUSHE_FORUMS
            if prefer == "cos":
                forums = MIYOUSHE_COS_FORUMS + [f for f in forums if f not in MIYOUSHE_COS_FORUMS]
            elif prefer == "art":
                forums = MIYOUSHE_ART_FORUMS + [f for f in forums if f not in MIYOUSHE_ART_FORUMS]
            plans = [("miyoushe", {"art"}, fid, _build_queries(kw, sub, board)) for fid in forums]
            plans += [("miyoushe", {"", "art"}, None, _build_queries(kw, sub, board))]
            _use_anime_fallback = False
        elif _looks_like_character(kw):
            # 角色向：先找立绘/二创/美图，再退化普通图；图源 异环 -> 米游社各版块 -> 各自普通图
            forums = MIYOUSHE_FORUMS
            if prefer == "cos":
                forums = MIYOUSHE_COS_FORUMS + [f for f in forums if f not in MIYOUSHE_COS_FORUMS]
            elif prefer == "art":
                forums = MIYOUSHE_ART_FORUMS + [f for f in forums if f not in MIYOUSHE_ART_FORUMS]
            plans = [("tajiduo", {"art"}, None, _build_queries(kw, sub, board))]
            plans += [("miyoushe", {"art"}, fid, _build_queries(kw, sub, board)) for fid in forums]
            plans += [("tajiduo", {"", "art"}, None, _build_queries(kw, sub, board)),
                      ("miyoushe", {"", "art"}, None, _build_queries(kw, sub, board))]
            _use_anime_fallback = False
        else:
            # 非游戏角色（猫/风景/二次元…）：不拿游戏论坛凑数，直接公有图 + 二次元源
            plans = []
            _use_anime_fallback = True
    mc = sub or kw  # 二级限定词存在时，要求结果真含该修饰词，进一步去串味
    for source, cats, forum_id, queries in plans:
        for q in queries:
            try:
                urls = crawl_images(q, limit, cats, must_contain=mc,
                                    source=source, forum_id=forum_id)
            except Exception as e:
                print(f"[爬图] 搜索失败（{q}）：{e}")
                continue
            # 前 8 名（同类 + 专帖）里随机：兼顾相关性与「不要总同一张」
            head = urls[:8]
            random.shuffle(head)
            urls = head + urls[8:]
            for u in urls:
                try:
                    raw = _download_bytes(u)
                    if not (cats & {"auction", "roster"}):
                        # 角色图场景：跳过手机截图和超宽界面条（面板/选择条）
                        im = Image.open(io.BytesIO(raw))
                        if _looks_like_screenshot(im) or _looks_like_ui_shot(im):
                            continue
                    sig = hash(raw)  # 按内容哈希判重（同一张图不同 URL 也能识别）
                    if _was_sent(sig):
                        continue
                    norm = normalize_image(raw)
                    _mark_sent(sig)
                    return norm
                except Exception:
                    continue
    # 非游戏角色话题：先试二次元公开源（loliapi），避免游戏论坛跑题；再走 Wikimedia 公有图
    if _use_anime_fallback:
        try:
            anime_urls = crawl_loliapi("acg", limit)
        except Exception as e:
            print(f"[爬图] 二次元源兜底失败（{kw}）：{e}")
            anime_urls = []
        random.shuffle(anime_urls)
        for u in anime_urls:
            try:
                raw = _download_bytes(u)
                sig = hash(raw)
                if _was_sent(sig):
                    continue
                norm = normalize_image(raw)
                _mark_sent(sig)
                return norm
            except Exception:
                continue
    # 通用兜底：tajiduo（异环同人论坛）没有的内容，用 Wikimedia 公有图补上 -> 「看看X」能聊万物
    try:
        wm_urls = _wikimedia_images(kw, limit, must_contain=kw)
    except Exception as e:
        print(f"[爬图] Wikimedia 兜底失败（{kw}）：{e}")
        wm_urls = []
    random.shuffle(wm_urls)
    for u in wm_urls:
        try:
            raw = _download_bytes(u)
            im = Image.open(io.BytesIO(raw))
            if _looks_like_screenshot(im) or _looks_like_ui_shot(im):
                continue
            sig = hash(raw)
            if _was_sent(sig):
                continue
            norm = normalize_image(raw)
            _mark_sent(sig)
            return norm
        except Exception:
            continue
    return None


def _random_lib_item():
    """从 IMAGE_LIB 抽一条 (url, path)，优先近期没发过的；空库返回 None。"""
    lib = CONFIG.get("IMAGE_LIB") or []
    if not lib:
        return None
    return _lib_item_to_pair(_pick_fresh(lib))


def _random_emoticon():
    """从 EMOTICON_LIB 抽一条本地表情包路径，优先近期没发过的。"""
    lib = CONFIG.get("EMOTICON_LIB") or []
    if not lib:
        return None
    return None, _pick_fresh(lib)


async def _send_static_media(message, text, pair):
    """用静态库里的一张图做图文回复（msg_seq 由 _reply 自动递增）。"""
    url, path = pair
    if url:
        await _reply_with_media(message, text, url=url)
    else:
        await _reply_with_media(message, text, data=open(path, "rb").read(),
                                file_name=os.path.basename(path))


async def _crawl_and_reply_image(message, keyword, prefer=None, sub=None, board=None):
    """后台任务：按关键词（含可选二级限定词/板块前缀）爬图，成功则补发一张；找不到就直说。"""
    img = None
    try:
        img = await asyncio.to_thread(_crawl_keyword_image, keyword, sub, prefer=prefer, board=board)
    except Exception as e:
        print(f"[爬图] 动态爬取异常（{board or ''}{keyword} {sub or ''}）：{e}")
    if img:
        try:
            img = _shrink_for_upload(img)
            if not img:
                raise ValueError("图片超过 5MB，压缩后仍超限")
            group_openid = getattr(message, "group_openid", None)
            openid = getattr(getattr(message, "author", None), "user_openid", None)
            base = f"{keyword}_{sub}" if sub else keyword
            fname = f"{board}_{base}.jpg" if board else f"{base}.jpg"
            fi = await _get_file_info(BOT_API, group_openid, openid, 1,
                                      data=img, file_name=fname)
            if fi:
                # 文字已发出，这里补图（msg_seq 由 _reply 自动递增）
                await _reply(message, media={"file_info": fi}, msg_type=7)
                return
        except Exception as e:
            print(f"[爬图] 上传/发送失败（{board or ''}{keyword} {sub or ''}）：{e}")
    # 找不到相关图就直说：拿图库里无关的图充数，比不发观感更差
    try:
        show = f"{board or ''}{keyword}{(' ' + sub) if sub else ''}"
        await _reply(message, content=_with_prefix(
            f"没找到「{show}」相关的图，这个词有点冷门～换个说法或带上作品名我再试试"))
    except Exception as e:
        print(f"[爬图] 兜底提示发送失败：{e}")


async def _ensure_image_lib(message):
    """图库为空时按需现扒一批图（不再启动时自动扒）。返回 True 表示已拿到图可用。"""
    if CONFIG.get("IMAGE_LIB"):
        return True
    await _reply(message, content=_with_prefix("我图库空的，去给你扒点图来，稍等～ (｡•̀ᴗ•́)"))
    try:
        await asyncio.to_thread(_crawl_image_lib, 12)
    except Exception as e:
        print(f"[扒图] 按需扒图失败：{e}")
    return bool(CONFIG.get("IMAGE_LIB"))


async def _reply_with_possible_image(message, content, reply_text):
    """统一出口：先决定要不要带图——显式对象则动态爬，否则偶尔抽静态库。"""
    res = _extract_topic_query(content)
    kw, sub, board = res if res else (None, None, None)
    if kw:
        if _OWNER_HINT_RE.search(kw):
            # 远旅是机器人主人：有照片就发照片，没有才回可爱兜底，不跑去外网瞎搜
            owner_lib = CONFIG.get("OWNER_IMAGES") or []
            if owner_lib:
                item = _lib_item_to_pair(random.choice(owner_lib))
                await _send_static_media(message,
                                         _with_prefix("这是远旅～ (｡･ω･｡)"), item)
            else:
                await _reply(message, content=_with_prefix(
                    "远旅是我主人呀，我哪有他的照片～不过他确实挺好的 (｡･ω･｡)"))
            return
        # 已知群员：直接发他注册的照片，不去外网瞎搜
        mem = CONFIG.get("MEMBER_IMAGES") or {}
        if kw in mem and mem[kw]:
            item = _lib_item_to_pair(random.choice(mem[kw]))
            await _send_static_media(message, _with_prefix(f"这是{kw}～ (｡･ω･｡)"), item)
            return
        # 看图请求：只发一句短句 + 图，绝不贴角色文档/长篇设定（那跟「看图」不相干）
        board_text = f"{board}" if board else ""
        sub_text = f"的{sub}" if sub else ""
        await _reply(message, content=_with_prefix(
            random.choice([f"{board_text}{kw}{sub_text}给你～",
                           f"找到{board_text}{kw}{sub_text}的图了～",
                           f"{board_text}{kw}{sub_text}来啦～"])))
        # 点名要 cos 时优先 COS 版块
        prefer = "cos" if _COS_RE.search(content or "") else None
        asyncio.create_task(_crawl_and_reply_image(message, kw, prefer=prefer, sub=sub, board=board))
        return
    if _wants_image(content):  # 显式要图但没指定对象 -> 静态库必带
        item = _random_lib_item()
        if not item:
            # 图库空：按需现扒（不再启动时自动扒）
            if not await _ensure_image_lib(message):
                return
            item = _random_lib_item()
        if item:
            await _send_static_media(message, reply_text, item)
            return
    att = _should_attach_image(content)  # 相关话题偶尔带图
    if att:
        await _send_static_media(message, reply_text, att)
        return
    await _reply(message, content=reply_text)


async def _reply_with_image(message, content):
    """用户要图时，从 IMAGE_LIB 随机抽一张做图文回复。"""
    lib = CONFIG.get("IMAGE_LIB") or []
    if not lib:
        await _reply(message, content=_with_prefix(
            "我还不会发图呢～让远旅给我准备几张图片 URL 放进 IMAGE_LIB 吧 (｡•́︿•̀｡)"))
        return
    item = _pick_fresh(lib)
    url, path = _lib_item_to_pair(item)
    if url is None and path is None:
        await _reply(message, content=_with_prefix("图库里有张图找不到了，让远旅检查下～"))
        return
    if url:
        await _reply_with_media(message, "给你～ (｡･ω･｡)", url=url, file_type=1)
    else:
        await _reply_with_media(message, "给你～ (｡･ω･｡)", data=open(path, "rb").read(),
                                file_name=os.path.basename(path), file_type=1)


async def _handle_media(message, content, file_type):
    """处理 /图片|/视频|/语音：支持多个 URL 或本地文件（逐条发送，每次一条媒体）。"""
    api = BOT_API
    if api is None:
        await _reply(message, content=_with_prefix("机器人还在启动中，稍后再试～"))
        return
    items = _parse_media_args(content)
    # 无参数：图片走图库随机一张；视频/语音提示用法
    if not items:
        if file_type == 1:
            lib = CONFIG.get("IMAGE_LIB") or []
            if not lib:
                # 图库空：按需现扒（不再启动时自动扒）
                if not await _ensure_image_lib(message):
                    return
                lib = CONFIG.get("IMAGE_LIB") or []
            if not lib:
                await _reply(message, content=_with_prefix(
                    "扒图也没扒到，等会儿再试试，或 @我 /图片 <图片URL 或 本地路径>"))
                return
            items = [_lib_item_to_pair(_pick_fresh(lib))]
        else:
            await _reply(message, content=_with_prefix(
                f"用法：@我 /{_MEDIA_NAME[file_type]} <URL 或 本地文件路径>（可一次多个，空格分隔）"))
            return
    # 数量保护：单次最多 9 个，避免刷屏 / 触发主动消息频控
    MAX = 9
    if len(items) > MAX:
        items = items[:MAX]
        await _reply(message, content=_with_prefix(f"一次最多发 {MAX} 个，已截取前 {MAX} 个发送～"))
    # 安全：群聊里禁止发本机文件（防信息泄露），只接受 URL
    if _is_group_ctx(message):
        local_items = [(u, p) for (u, p) in items if p]
        items = [(u, p) for (u, p) in items if not p]
        if local_items:
            await _reply(message, content=_with_prefix(_LOCAL_HINT))
        if not items:
            return
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    sent = 0
    for url, path in items:
        data = None
        file_name = None
        if url:
            if _is_blocked_url(url):  # 禁止 localhost / 内网地址（防 SSRF）
                await _reply(message, content=_with_prefix("该链接地址不被允许～"))
                continue
            if not _IMG_EXT_RE.search(url):  # 非直链 -> 尝试从网页提取直链
                url = _resolve_image_url(url) or url
        if path:
            if not os.path.isfile(path):
                await _reply(message, content=_with_prefix(f"找不到本地文件：{path}（已跳过）"))
                continue
            data = open(path, "rb").read()
            file_name = os.path.basename(path)
            if len(data) > _UPLOAD_SOFT_MAX:  # 大图先压，压不动再跳过
                data = _shrink_for_upload(data) or data
            if len(data) > _MAX_BINARY:
                await _reply(message, content=_with_prefix(
                    f"本地文件超过 5MB，已跳过：{path}（请改用网络 URL）"))
                continue
        try:
            resp = await _upload_media(api, group_openid, openid, file_type,
                                       url=url, data=data, file_name=file_name,
                                       srv_send_msg=True)
            resp = resp if isinstance(resp, dict) else (getattr(resp, "__dict__", {}) or {})
            if resp.get("id"):
                sent += 1
            else:
                await _reply(message, content=_with_prefix(
                    f"已上传{_MEDIA_NAME[file_type]}，但未返回消息ID，请稍后查看（{path or url}）"))
        except Exception as e:
            await _reply(message, content=_with_prefix(
                f"发送{_MEDIA_NAME[file_type]}失败：{path or url}\n" + str(e)[:160]))
    if sent and len(items) > 1:
        await _reply(message, content=_with_prefix(f"📤 已发送 {sent} 个{_MEDIA_NAME[file_type]}～"))


async def _handle_draw(message, content):
    """文生图：接入 _generate_image（生图 API）后，直接把生成的图片发出去。"""
    parts = content.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    if not prompt:
        await _reply(message, content=_with_prefix(
            "用法：/生图 <描述>，例如 /生图 一只在星空下睡觉的猫"))
        return
    url = _generate_image(prompt)
    if not url:
        await _reply(message, content=_with_prefix(
            "生图功能还没接入生图 API～等主人把接口接上，我就能给你画画啦 (｡•́︿•̀｡)"))
        return
    await _handle_media(message, "图片 " + url, 1)


async def _handle_search(message, content):
    """群内 /搜索：联网搜索并回复结果。"""
    parts = content.split(maxsplit=1)
    q = parts[1].strip() if len(parts) > 1 else ""
    if not q:
        await _reply(message, content=_with_prefix("用法：/搜索 <关键词>，例如 /搜索 DeepSeek V4 发布"))
        return
    await _reply(message, content=_with_prefix("🔍 正在搜索「" + q + "」…"))
    res = _web_search(q, count=5, need_content=False)
    if res is None:
        await _reply(message, content=_with_prefix("搜索失败了（可能 API Key 未配置或额度不足）～"))
        return
    if not res:
        await _reply(message, content=_with_prefix("没搜到相关结果呢～换个说法试试？"))
        return
    lines = []
    for i, r in enumerate(res[:5], 1):
        lines.append(f"{i}. {r.get('title','')}\n   {r.get('summary','')}\n   🔗 {r.get('url','')}")
    await _reply(message, content=_with_prefix("🔍 搜索「" + q + "」：\n\n" + "\n\n".join(lines)))


async def _handle_fetch(message, content):
    """群内 /抓取：抓取网页正文并回复。"""
    parts = content.split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not url or not url.startswith("http"):
        await _reply(message, content=_with_prefix("用法：/抓取 <网页URL>，例如 /抓取 https://example.com/article"))
        return
    await _reply(message, content=_with_prefix("📄 正在抓取网页…"))
    page = _web_fetch(url)
    if page is None:
        await _reply(message, content=_with_prefix("抓取失败（链接不可用、被拦截或正文无法提取）～"))
        return
    title, body = page
    await _reply(message, content=_with_prefix(f"📄 {title}\n\n{(body or '')[:4000]}\n\n🔗 {url}"))


# ---------- tajiduo 图片爬取 + 规范化上传 ----------
# 从 tajiduo（异环同人论坛）按关键词爬帖图，规范化为合规 JPEG（RGB / 等比缩放 / 去元数据，确保 <=5MB 可二进制直传），
# 存到本地 images/ 并写入 config.json 的 IMAGE_LIB / AUCTION_LIB；发送时再上传拿 file_info。

TAJIDUO_API = "https://bbs-api.tajiduo.com"
TAJIDUO_DEFAULT_KW = "异环"  # 默认爬取关键词（异环同人论坛 tajiduo）
IMAGES_DIR = "images"
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://www.tajiduo.com/bbs/index.html",
    "Accept": "application/json, text/plain, */*",
}


# ---------- 安全：群成员禁止访问本机文件 / 内网 ----------
def _is_group_ctx(message):
    """消息是否来自群聊（群成员）。本地文件等敏感操作只在私聊开放。"""
    return bool(getattr(message, "group_openid", None))


_LOCAL_HINT = "为了安全，群里只能用图片 URL，本地文件请私聊我发～"


def _is_blocked_url(url):
    """禁止访问本地/内网地址（防 SSRF / 群员探本机）：file://、localhost、私有网段。"""
    u = (url or "").lower()
    if u.startswith(("file:", "ftp:")):
        return True
    host = re.sub(r"^https?://", "", u).split("/")[0].split(":")[0].split("@")[-1]
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        return True
    if host.endswith(".local"):
        return True
    if host.startswith(("127.", "10.", "192.168.", "169.254.")):
        return True
    m = re.match(r"^172\.(\d+)\.", host)
    if m and 16 <= int(m.group(1)) <= 31:
        return True
    return False


def _clean_url_arg(arg):
    """去掉 QQ/微信 常见的包裹符：<...> 「...」 《...》 "..." '...' 及首尾空白。"""
    s = (arg or "").strip()
    for a, b in (("<", ">"), ("「", "」"), ("《", "》"), ("“", "”"), ('"', '"'), ("'", "'")):
        if len(s) >= 2 and s[0] == a and s[-1] == b:
            s = s[1:-1].strip()
            break
    return s


def _looks_like_local_path(arg):
    """排除 URL 之后，含路径分隔符或以 . / ~ 开头的，视为本机路径。"""
    if not arg:
        return False
    if "://" in arg or _URL_RE.match(arg):  # 含 :// 一律视作 URL，即使被 < > 包裹
        return False
    return (os.path.sep in arg) or ("/" in arg) or arg.startswith(".") or arg.startswith("~")


def _download_text(url, timeout=20):
    data = _download_bytes(url, timeout=timeout)
    return data.decode("utf-8", "ignore")


# 拍卖/竞拍类截图（“竞拍结束”“我的资产”“当前估价”等界面），单独归档
_AUCTION_RE = re.compile(r"拍卖|竞拍|成交价|估价|拍品|出价|一口价|竞得|我的资产", re.I)
# 角色列表/配队类截图（“角色列表”“等级从高→低”整屏角色卡），单独归档
_ROSTER_RE = re.compile(r"角色列表|配队|阵容|组队|编队|练度|强度榜|角色一览|怎么配|配什么", re.I)
# 角色向图片（立绘/二创/表情包/美图）——优先爬这类，用户想看的就是角色本身
_ART_RE = re.compile(
    r"立绘|同人|二创|二次创作|美图|壁纸|插画|画师|手绘|厚涂|临摹|表情包|表情|头像|套图|cos|捏脸", re.I)
# 游戏界面/数据类（面板、属性、道具、抽卡、攻略）——少爬，别发成截图
_UI_RE = re.compile(
    r"面板|属性|数值|词条|伤害|测评|攻略|抽卡|祈愿|卡池|道具|素材|材料|养成|配装|装备|"
    r"掉落|收益|概率|爆率|公告|活动|兑换|商店|升级|强化|"
    # 合集/对比/榜单类：图里塞好几个角色，和「只想看某个人」对不上，直接排除
    r"对比|一览|大全|合集|排行|首测|盘点|汇总|清单|图鉴|立绘建模|建模对比", re.I)

# 出图优先级：art 最高，普通图次之，ui 最低（几乎不发）
_CAT_PRIORITY = {"art": 0, "": 1, "auction": 2, "roster": 3, "ui": 9}


def _classify_post(post):
    """帖图归类：auction(拍卖)/roster(角色列表)/ui(面板道具抽卡)/art(立绘二创表情)/''(普通)。"""
    try:
        blob = json.dumps(post, ensure_ascii=False)
    except Exception:
        blob = str(post)
    if _AUCTION_RE.search(blob):
        return "auction"
    if _ROSTER_RE.search(blob):
        return "roster"
    if _UI_RE.search(blob):
        return "ui"
    if _ART_RE.search(blob):
        return "art"
    return ""


def _tajiduo_search_items(keyword, limit=12, must_contain=None):
    """按关键词搜帖，返回帖子正文图 [{url, cat}]；cat 为空表示普通图。

    must_contain：帖子正文/标题里必须出现的词（模糊搜索结果常常跑题，用它卡相关性）。
    """
    url = f"{TAJIDUO_API}/bbs/wapi/searchPost?keyword={_uparse.quote(keyword)}"
    try:
        body = _download_text(url)
        j = json.loads(body)
    except Exception as e:
        print(f"[扒图] 搜索失败（{keyword}）：{e}")
        return []
    posts = (j.get("data") or {}).get("posts") or []
    needle = (must_contain or "").strip().lower()
    items, seen = [], set()
    for post in posts:
        if needle:
            try:
                blob = json.dumps(post, ensure_ascii=False).lower()
            except Exception:
                blob = str(post).lower()
            if needle not in blob:  # 帖子压根没提这个词 -> 结果无关，丢掉
                continue
        cat = _classify_post(post)
        clen = len(str(post.get("content") or ""))  # 正文短 = 多为专门发这个角色的图
        for it in (post.get("images") or []):
            u = it.get("url") if isinstance(it, dict) else str(it)
            if u and u not in seen:
                seen.add(u)
                items.append({"url": u, "cat": cat, "clen": clen})
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items[:limit]


# 手机常见宽度，配合狭长比例识别截图（截图常带状态栏，影响美观，尽量跳过）
_PHONE_WIDTHS = {720, 750, 828, 1080, 1125, 1170, 1242, 1284, 1440, 1536, 1792, 2160}


def _looks_like_screenshot(im):
    """粗略识别手机截图：宽度是常见手机值且高宽比偏大，或整体异常狭长。"""
    w, h = im.size
    if not w or not h:
        return False
    ratio = h / w
    if w in _PHONE_WIDTHS and ratio >= 1.8:
        return True
    if ratio >= 2.15:
        return True
    return False


def _looks_like_ui_shot(im):
    """粗判游戏界面截图：超宽横幅（如 1080×315 的角色选择条）、过小图标都不像角色图。

    角色图/插画多为竖版或接近 4:3、16:9，出现 2.2 以上的超宽比基本是界面条。
    """
    w, h = im.size
    if not w or not h:
        return False
    if w / h >= 2.2:
        return True
    if min(w, h) < 240:  # 过小的图标/缩略图，放大后糊
        return True
    return False


def _tajiduo_emoticon_urls(limit=60):
    """抓取论坛表情包库（getEmoticons）里的贴图 URL（去重）。"""
    url = f"{TAJIDUO_API}/apihub/wapi/getEmoticons"
    try:
        body = _download_text(url)
        j = json.loads(body)
    except Exception as e:
        print(f"[表情包] 拉取失败：{e}")
        return []
    out, seen = [], set()

    def _walk(o, d=0):
        if d > 7 or len(out) >= limit:
            return
        if isinstance(o, str):
            for m in re.finditer(r"https?://[^\s\"'\\<>]+?\.(?:jpg|jpeg|png|gif|webp)", o):
                u = m.group(0)
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v, d + 1)
        elif isinstance(o, list):
            for v in o:
                _walk(v, d + 1)

    _walk(j.get("data") if isinstance(j, dict) else j)
    return out[:limit]


def _site_root(host):
    """取站点根域名：gd-hbimg.huaban.com -> huaban.com；img.foo.com.cn -> foo.com.cn。"""
    parts = [x for x in (host or "").lower().split(".") if x]
    if len(parts) <= 2:
        return ".".join(parts)
    if parts[-2] in ("co", "com", "net", "org", "gov", "edu", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _download_bytes(url, timeout=15, max_bytes=25 * 1024 * 1024, retries=1, backoff=1.0, headers=None):
    """下载二进制，带超时与重试（应对偶发 SSL 握手 / 连接超时）。

    安全：禁止访问本地/内网地址（防 SSRF / 群员探本机）。huijiwiki 需带本站 Referer 否则 403。
    """
    if _is_blocked_url(url):
        raise ValueError("禁止访问本地/内网地址")
    import time
    from urllib.parse import urlsplit
    base = dict(_BROWSER_HEADERS)
    if headers:
        base.update(headers)
    p = urlsplit(url)
    # 防盗链：Referer 要用「站点根域名」。花瓣只认 huaban.com，不认 CDN 子域（否则 567）
    if "huiji" in url:
        referrals = ["https://res1999.huijiwiki.com/"]
    elif "tajiduo" in url:
        referrals = [base.get("Referer")]  # 塔吉多保持默认
    else:
        root = _site_root(p.netloc)
        # 先试站点根 Referer；不行再退回「完全不带 Referer」（花瓣无 Referer 也放行）
        referrals = [f"{p.scheme or 'https'}://{root}/" if root else None, None]
    last_err = None
    for attempt in range(retries + 1):
        hdrs = dict(base)
        ref = referrals[min(attempt, len(referrals) - 1)]
        if ref:
            hdrs["Referer"] = ref
        else:
            hdrs.pop("Referer", None)
        try:
            req = _ureq.Request(url, headers=hdrs)
            with _ureq.urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("图片过大")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                print(f"[下载] 第{attempt + 1}次失败（{str(e)[:60]}），{backoff:.0f}s 后重试")
                time.sleep(backoff)
                backoff *= 2
    from urllib.error import HTTPError
    if isinstance(last_err, HTTPError):
        if last_err.code == 404:
            raise ValueError("图片不存在或链接已失效（404），请确认链接有效") from last_err
        if last_err.code in (403, 567):
            raise ValueError(f"图片被防盗链拦截（HTTP {last_err.code}），请换直链或本地路径") from last_err
        raise ValueError(f"下载失败（HTTP {last_err.code}），请稍后重试或换链接") from last_err
    raise last_err


_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|avif)(?:\?|#|$)", re.I)


def _huiji_to_original(url):
    """huijiwiki 文件页 -> 原图直链（绕过缩略图与防盗链，需带 Referer）。"""
    try:
        html = _download_text(url, timeout=20)
    except Exception:
        return None
    m = re.search(
        r"huijistatic\.com/res1999/uploads/(?:thumb/)?([0-9a-f]/[0-9a-f]{2})/"
        r"(Portrait-[^/\s\"'<>]+\.(?:webp|png|jpe?g|gif))", html, re.I)
    if not m:
        return None
    return f"https://huiji-public.huijistatic.com/res1999/uploads/{m.group(1)}/{m.group(2)}"


def _extract_direct_image(page_url, timeout=20):
    """从文章/相册页提取直链图片：og:image / twitter:image / image_src / 首张图。"""
    try:
        html = _download_text(page_url, timeout=timeout)
    except Exception:
        return None
    cands = []
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
                r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)'):
        cands += re.findall(pat, html, re.I)
    cands += re.findall(r'<img[^>]+src=["\']([^"\']+\.(?:jpe?g|png|webp|gif))', html, re.I)
    for c in cands:
        if c.startswith("//"):
            c = "https:" + c
        if c.startswith(("http://", "https://")) and _IMG_EXT_RE.search(c):
            return c
    return None


def _bing_media_url(url):
    """Bing 图片搜索结果页 -> 直接图片地址：优先 thid 拼缩略图，其次 mediaurl/murl 解码。"""
    from urllib.parse import unquote
    m = re.search(r"[?&]thid=([^&]+)", url, re.I)
    if m:
        return f"https://th.bing.com/th/id/{m.group(1)}?pid=Api"
    for key in ("mediaurl", "murl"):
        m = re.search(r"[?&]" + key + r"=([^&]+)", url, re.I)
        if m:
            u = unquote(m.group(1))
            if u.startswith(("http://", "https://")):
                return u
    return None


def _resolve_image_url(arg):
    """直链图片直接返回；否则尝试从网页提取直链 / huijiwiki 转原图 / Bing 图片页；失败返回 None。

    注意：huiji 的「文件页」URL 虽以 .webp 结尾却是 HTML，所以 huiji 优先转原图。
    """
    if not arg:
        return None
    if "hdslb.com" in arg and "@" in arg:
        arg = arg.split("@", 1)[0]  # bilibili 的 @尺寸/格式后缀常 404，取原图更稳
    if "bing.com" in arg:  # Bing 图片搜索页：og:image 是自家 logo，必须走 thid/mediaurl
        bing = _bing_media_url(arg)
        if bing:
            return bing
    if "huiji" in arg:
        return _huiji_to_original(arg) or _extract_direct_image(arg) or (arg if _IMG_EXT_RE.search(arg) else None)
    if _IMG_EXT_RE.search(arg) or arg.startswith("data:"):
        return arg
    return _extract_direct_image(arg)


def normalize_image(data, max_side=1280, quality=85):
    """把任意图片规范化为合规 JPEG：RGB、按 EXIF 转正、等比缩放、去元数据。"""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        head = (data or b"")[:256].lstrip().lower()
        if head.startswith((b"<!doctype", b"<html", b"<?xml")):
            raise ValueError("下载到的不是图片（疑似防盗链/登录页），请换直链或本地路径") from e
        raise ValueError("无法识别图片格式（链接可能失效或不是图片）") from e
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale + 0.5), int(h * scale + 0.5)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


# ---------- 米游社（原神）爬图 ----------
MIYOUSHE_API = "https://bbs-api.miyoushe.com"   # 站点：https://www.miyoushe.com/ys/
MIYOUSHE_GIDS = 2          # gids=2 -> 原神
MIYOUSHE_ART_FORUM = 29    # 原神·同人图版块（角色立绘/二创主要在这）
MIYOUSHE_COS_FORUM = 47    # 原神·COS 版块
# 可搜版块（已实测 ID -> 名称）：29/56 同人图，47/49 COS，48 脑洞（梗图整活），26 酒馆（综合）
MIYOUSHE_ART_FORUMS = [29, 56]
MIYOUSHE_COS_FORUMS = [47, 49]
MIYOUSHE_EXTRA_FORUMS = [48, 26]
MIYOUSHE_FORUMS = MIYOUSHE_ART_FORUMS + MIYOUSHE_COS_FORUMS + MIYOUSHE_EXTRA_FORUMS


def _miyoushe_post_images(post):
    """取出一个帖子的图片 URL（post.images 为字符串列表，image_list 为 {url} 列表）。"""
    out = []
    for it in ((post.get("post") or {}).get("images") or []):
        out.append(it if isinstance(it, str) else str((it or {}).get("url") or ""))
    for it in (post.get("image_list") or []):
        u = it.get("url") if isinstance(it, dict) else it
        if u:
            out.append(u)
    return [u for u in out if u]


def _miyoushe_search_items(keyword, limit=12, must_contain=None, forum_id=None):
    """从米游社（原神）搜帖取图，返回 [{url, cat, clen}]；cat 为空表示普通图。"""
    kw = (keyword or "").strip()
    if kw:
        url = (f"{MIYOUSHE_API}/post/wapi/searchPosts?keyword={_uparse.quote(kw)}"
               f"&gids={MIYOUSHE_GIDS}&size={max(limit * 2, limit)}")
        if forum_id:
            url += f"&forum_id={forum_id}"
    else:  # 没关键词就按版块最新帖抓（默认同人图版块）
        url = (f"{MIYOUSHE_API}/post/wapi/getForumPostList?forum_id={forum_id or MIYOUSHE_ART_FORUM}"
               f"&gids={MIYOUSHE_GIDS}&page_size={limit}&sort_type=1")
    try:
        j = json.loads(_download_text(url))
    except Exception as e:
        print(f"[扒图] 米游社搜索失败（{keyword}）：{e}")
        return []
    data = j.get("data") or {}
    posts = data.get("posts") or data.get("list") or []
    needle = (must_contain or "").strip().lower()
    items, seen = [], set()
    for p in posts:
        if needle:
            try:
                blob = json.dumps(p, ensure_ascii=False).lower()
            except Exception:
                blob = str(p).lower()
            if needle not in blob:  # 帖子没提到这个词 -> 结果无关
                continue
        cat = _classify_post(p)
        clen = len(str((p.get("post") or {}).get("content") or ""))
        for u in _miyoushe_post_images(p):
            if u in seen:
                continue
            seen.add(u)
            items.append({"url": u, "cat": cat, "clen": clen})
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items[:limit]


def crawl_miyoushe_images(keyword, limit=12, cats=None, must_contain=None, forum_id=None):
    """只从米游社（原神）搜图 URL；cats 默认「角色向(art) + 普通图」。"""
    return crawl_images(keyword, limit, cats, must_contain=must_contain,
                        source="miyoushe", forum_id=forum_id)


def _wikimedia_images(query, limit=10, must_contain=None):
    """从 Wikimedia Commons 按关键词搜公有图（无需 key），返回图片 URL 列表（已过滤非图/过小）。

    must_contain：文件名必须含该词，避免搜「娜娜莉」却返回同名无关图。
    """
    url = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search"
           "&gsrsearch=" + _uparse.quote(query) +
           "&gsrnamespace=6&gsrlimit=%d&prop=imageinfo&iiprop=url|size&iiurlwidth=1280&format=json" % limit)
    try:
        j = json.loads(_download_text(url, timeout=10))
    except Exception as e:
        print(f"[扒图] Wikimedia 搜索失败（{query}）：{e}")
        return []
    pages = (j.get("query") or {}).get("pages") or {}
    needle = (must_contain or "").strip().lower()
    out = []
    for p in pages.values():
        if needle and needle not in str(p.get("title", "")).lower():
            continue
        ii = (p.get("imageinfo") or [{}])[0]
        u = ii.get("url")
        if not u or u.lower().endswith((".svg", ".gif", ".webm", ".ogg", ".oga", ".tif", ".tiff")):
            continue
        w, h = ii.get("width", 0), ii.get("height", 0)
        if w and h and (w < 200 or h < 200):  # 跳过缩略/图标
            continue
        out.append(u)
    return out[:limit]


def crawl_images(keyword, limit=12, cats=None, must_contain=None, source="tajiduo", forum_id=None):
    """按关键词搜图 URL；默认只取「角色向(art) + 普通图」，auction/roster/ui 一律排除。

    must_contain：只保留正文里真的提到该词的帖子图，避免模糊搜索跑题。
    source：'tajiduo'（异环同人论坛）/ 'miyoushe'（米游社·原神）/ 'lolicon'（公开二次元图 API）。
    同类多张时按 _CAT_PRIORITY 排序：立绘/二创/表情包优先，普通图其次。
    """
    want = {"", "art"} if cats is None else set(cats)
    if source == "miyoushe":
        items = _miyoushe_search_items(keyword, max(limit * 4, limit),
                                       must_contain=must_contain, forum_id=forum_id)
    elif source == "lolicon":
        urls = crawl_lolicon(keyword, limit)
        items = [{"url": u, "cat": "art", "clen": 0} for u in urls]
    else:
        items = _tajiduo_search_items(keyword, max(limit * 4, limit), must_contain=must_contain)
    picked = [it for it in items if it["cat"] in want]
    # 同类里正文短的排前面：专帖优先于合集/长帖，图更对得上关键词
    picked.sort(key=lambda it: (_CAT_PRIORITY.get(it["cat"], 5), it.get("clen", 999)))
    return [it["url"] for it in picked][:limit]


# ---------- 二次元图源（Lolicon API，公开、专供二次元，强制 SFW 防社死）----------
_LOLICON_URL = "https://api.lolicon.app/setu/v2"
_LOLICON_COOLDOWN = 3.0  # 匿名接口有频率限制，冷却避免触发 429
_lolicon_last = 0.0


def crawl_lolicon(keyword=None, limit=6, r18=0):
    """从 Lolicon API 取二次元图直链（regular 尺寸，<=1200px），强制 SFW(r18=0)。

    返回直链 URL 列表；触发冷却或失败返回空列表。图床为 i.pixiv.re（无需特殊 Referer）。
    """
    global _lolicon_last
    import time as _t
    now = _t.time()
    if now - _lolicon_last < _LOLICON_COOLDOWN:
        return []
    _lolicon_last = now
    payload = {"r18": r18, "size": ["regular"], "limit": max(1, min(int(limit), 20)),
               "keyword": [keyword] if keyword else []}
    try:
        # 注意：不能带塔吉多的 Referer，否则 Lolicon(cloudflare) 会 403；只发 UA + JSON
        headers = {"User-Agent": _BROWSER_HEADERS["User-Agent"], "Content-Type": "application/json"}
        req = _ureq.Request(_LOLICON_URL,
                            data=json.dumps(payload).encode("utf-8"),
                            headers=headers,
                            method="POST")
        with _ureq.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[二次元] Lolicon 请求失败：{e}")
        return []
    data = (j or {}).get("data") or []
    urls = []
    for d in data:
        urlsrc = d.get("urls") or {}
        u = urlsrc.get("regular") or urlsrc.get("original")
        if u:
            urls.append(u)
    return urls


# ---------- 二次元图源 2：loliapi（接口 302 跳到随机二次元图，图池大）----------
_LOLIAPI_BASE = "https://www.loliapi.com"
_LOLIAPI_KINDS = {
    "acg": "/acg/",     # 双端自适应
    "pc": "/acg/pc/",   # 电脑端（横屏）
    "pe": "/acg/pe/",   # 移动端（竖屏）
    "pp": "/acg/pp/",   # 随机头像
    "bg": "/bg/",       # 随机壁纸
}
_LOLIAPI_ALIAS = {
    "pc": "pc", "电脑": "pc", "电脑端": "pc", "横屏": "pc",
    "pe": "pe", "手机": "pe", "手机端": "pe", "移动": "pe", "竖屏": "pe",
    "pp": "pp", "头像": "pp",
    "bg": "bg", "壁纸": "bg", "背景": "bg",
    "acg": "acg",
}


def crawl_loliapi(kind="acg", limit=3):
    """从 loliapi.com 取二次元图直链：接口 302 跳到图片，这里只取跳转后的地址。

    每次请求都是随机图，偶发的 404（图被清理）用重试绕过，不因单次失败就放弃。
    """
    path = _LOLIAPI_KINDS.get(kind, "/acg/")
    url = _LOLIAPI_BASE + path
    want = max(int(limit), 1)
    urls = []
    attempts = 0
    max_attempts = want * 3
    while len(urls) < want and attempts < max_attempts:
        attempts += 1
        try:
            req = _ureq.Request(url, headers={"User-Agent": _BROWSER_HEADERS["User-Agent"]})
            r = _ureq.urlopen(req, timeout=15)
            try:
                final = r.geturl()  # urlopen 已跟随 302，取最终图片地址
            finally:
                r.close()
            if final and final != url and final not in urls:
                urls.append(final)
        except Exception as e:
            print(f"[二次元] loliapi 请求失败（第{attempts}次）：{e}")
            continue
    return urls


def crawl_auction_images(keyword, limit=12):
    """只返回拍卖类图片 URL（仅在用户聊拍卖时使用）。"""
    return crawl_images(keyword, limit, {"auction"})


def crawl_roster_images(keyword, limit=12):
    """只返回角色列表类图片 URL（仅在用户聊配队时使用）。"""
    return crawl_images(keyword, limit, {"roster"})


def _fetch_and_save(urls, save_dir, prefix="tajiduo", allow_screenshot=False):
    """下载 -> 跳过截图 -> 规范化 -> 存本地，返回本地 JPEG 路径列表（均 <=5MB）。

    allow_screenshot=True 时保留截图（角色列表/竞拍这类界面本身就是截图，分类后要保留）。
    """
    from PIL import Image
    os.makedirs(save_dir, exist_ok=True)
    paths = []
    for i, u in enumerate(urls):
        try:
            raw = _download_bytes(u)
            im = Image.open(io.BytesIO(raw))
            if not allow_screenshot and (_looks_like_screenshot(im) or _looks_like_ui_shot(im)):
                # 普通图库跳过手机截图和超宽界面条（面板/道具/选择条）
                print(f"[扒图] 跳过界面/截图类 {u[:48]}")
                continue
            norm = normalize_image(raw)
            p = os.path.join(save_dir, f"{prefix}_{int(time.time())}_{i}.jpg")
            with open(p, "wb") as f:
                f.write(norm)
            paths.append(p)
            print(f"[扒图] 已存 {p} ({len(norm)//1024}KB) <- {u[:48]}")
        except Exception as e:
            print(f"[扒图] 跳过 {u[:48]}: {e}")
    return paths


def crawl_to_local(keyword=None, limit=12, save_dir=IMAGES_DIR, cats=None):
    """爬取 -> 规范化 -> 存本地 images/，返回本地合规 JPEG 路径列表（均 <=5MB）。"""
    keyword = keyword or TAJIDUO_DEFAULT_KW
    return _fetch_and_save(crawl_images(keyword, limit, cats), save_dir)


def _add_to_image_lib(paths, key="IMAGE_LIB"):
    """把本地图片路径追加进 config.json 指定图库（默认 IMAGE_LIB）并落盘。"""
    lib = CONFIG.setdefault(key, [])
    added = 0
    for p in paths:
        if p not in lib:
            lib.append(p)
            added += 1
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return added


def _add_to_emoticon_lib(paths):
    """把本地表情包路径追加进 config.json 的 EMOTICON_LIB 并落盘。"""
    lib = CONFIG.setdefault("EMOTICON_LIB", [])
    added = 0
    for p in paths:
        if p not in lib:
            lib.append(p)
            added += 1
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return added


_CRAWL_CMDS = {"扒图", "crawl", "爬图", "搜图"}


def _is_crawl_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _CRAWL_CMDS


_SET_OWNER_CMDS = {"设群主图", "设主人图", "群主图", "setowner"}


def _is_set_owner_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _SET_OWNER_CMDS


async def _handle_set_owner(message, content):
    """@小忺 /设群主图 <URL或本地路径>：把这张设为群主（远旅）的照片并入库。"""
    parts = content.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await _reply(message, content=_with_prefix(
            "用法：/设群主图 <图片URL 或 本地路径>，把这张设为远旅的照片～"))
        return
    arg = _clean_url_arg(parts[1])
    # 兼容「/设群主图 远旅 <url>」这种多写了个名字的写法：首个 token 不是 URL/路径则丢弃
    first, sep, tail = arg.partition(" ")
    if sep and tail.strip() and not _URL_RE.match(first) and not _looks_like_local_path(first):
        arg = _clean_url_arg(tail)
    try:
        if _URL_RE.match(arg):
            real = _resolve_image_url(arg) or arg  # 页面链接自动转直链
            raw = await asyncio.to_thread(_download_bytes, real, timeout=30)
        elif _looks_like_local_path(arg):
            if _is_group_ctx(message):  # 群聊禁本机文件
                await _reply(message, content=_with_prefix(_LOCAL_HINT))
                return
            if os.path.isfile(arg):
                with open(arg, "rb") as fh:
                    raw = fh.read()
            else:
                await _reply(message, content=_with_prefix(
                    "没看懂这个来源～给我一个图片 URL 或本地文件路径就行"))
                return
        else:
            await _reply(message, content=_with_prefix(
                "没看懂这个来源～给我一个图片 URL 或本地文件路径就行"))
            return
        if not raw:
            raise ValueError("内容为空")
        os.makedirs(IMAGES_DIR, exist_ok=True)
        out = os.path.join(IMAGES_DIR, f"owner_{int(time.time())}.jpg")
        with open(out, "wb") as fh:
            fh.write(normalize_image(raw, max_side=1280, quality=88))
        rel = out.replace(os.sep, "\\")  # 与 config.json 里 images\.. 一致
        lib = CONFIG.setdefault("OWNER_IMAGES", [])
        if rel not in lib:
            lib.append(rel)
        try:
            with open("config.json", "w", encoding="utf-8") as fh:
                json.dump(CONFIG, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        await _send_static_media(message, _with_prefix("收到，这张就是远旅啦～ (｡･ω･｡)"),
                                 _lib_item_to_pair(rel))
    except Exception as e:
        await _reply(message, content=_with_prefix(f"设置失败：{str(e)[:120]}"))


_SET_MEMBER_CMDS = {"设群员图", "加群员图", "设成员图", "setmember"}


def _is_set_member_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _SET_MEMBER_CMDS


async def _handle_set_member(message, content):
    """@小忺 /设群员图 <成员名> <URL或本地路径>：把这张设为该成员（群友）的照片并入库。"""
    parts = content.split(None, 2)
    if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
        await _reply(message, content=_with_prefix(
            "用法：/设群员图 <成员名> <图片URL 或 本地路径>，如 /设群员图 真红 https://..."))
        return
    name = _clean_url_arg(parts[1])
    arg = _clean_url_arg(parts[2])
    try:
        if _URL_RE.match(arg):
            real = _resolve_image_url(arg) or arg  # 页面链接自动转直链（含 Bing 图片页）
            raw = await asyncio.to_thread(_download_bytes, real, timeout=30)
        elif _looks_like_local_path(arg):
            if _is_group_ctx(message):  # 群聊禁本机文件
                await _reply(message, content=_with_prefix(_LOCAL_HINT))
                return
            if os.path.isfile(arg):
                with open(arg, "rb") as fh:
                    raw = fh.read()
            else:
                await _reply(message, content=_with_prefix(
                    "没看懂这个来源～给我一个图片 URL 或本地文件路径就行"))
                return
        else:
            await _reply(message, content=_with_prefix(
                "没看懂这个来源～给我一个图片 URL 或本地文件路径就行"))
            return
        if not raw:
            raise ValueError("内容为空")
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)  # 文件名去非法字符
        os.makedirs(IMAGES_DIR, exist_ok=True)
        if _OWNER_HINT_RE.search(name):
            # 名字是群主（远旅）-> 存进群主图库，之后「看看远旅」才会发
            out = os.path.join(IMAGES_DIR, f"owner_{safe}_{int(time.time())}.jpg")
            with open(out, "wb") as fh:
                fh.write(normalize_image(raw, max_side=1280, quality=88))
            rel = out.replace(os.sep, "\\")
            lib = CONFIG.setdefault("OWNER_IMAGES", [])
            if rel not in lib:
                lib.append(rel)
            try:
                with open("config.json", "w", encoding="utf-8") as fh:
                    json.dump(CONFIG, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
            await _send_static_media(message, _with_prefix("收到，这张就是远旅（群主）啦～ (｡･ω･｡)"),
                                     _lib_item_to_pair(rel))
            return
        out = os.path.join(IMAGES_DIR, f"member_{safe}_{int(time.time())}.jpg")
        with open(out, "wb") as fh:
            fh.write(normalize_image(raw, max_side=1280, quality=88))
        rel = out.replace(os.sep, "\\")  # 与 config.json 里 images\.. 一致
        mem = CONFIG.setdefault("MEMBER_IMAGES", {})
        mem.setdefault(name, [])
        if rel not in mem[name]:
            mem[name].append(rel)
        try:
            with open("config.json", "w", encoding="utf-8") as fh:
                json.dump(CONFIG, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        await _send_static_media(message, _with_prefix(f"收到，这是{name}～ (｡･ω･｡)"),
                                 _lib_item_to_pair(rel))
    except Exception as e:
        await _reply(message, content=_with_prefix(f"设置失败：{str(e)[:120]}"))


_CONVERT_CMDS = {"转直链", "todirect", "directurl"}


def _is_convert_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _CONVERT_CMDS


async def _handle_convert_url(message, content):
    """@小忺 /转直链 <图片页或图片URL>：把文章/相册页链接转成可直接下载的直链。"""
    parts = content.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await _reply(message, content=_with_prefix(
            "用法：/转直链 <图片页或图片URL>，返回可直接下载的直链"))
        return
    arg = _clean_url_arg(parts[1])
    try:
        direct = _resolve_image_url(arg)
    except Exception:
        direct = None
    if not direct:
        await _reply(message, content=_with_prefix(
            "没能从这条链接里提取到直链图片（可能页面需要登录或有防盗链）～"))
        return
    await _reply(message, content=_with_prefix(f"直链：{direct}"))


# ---------- 「刷B站评论学活人感」：采集 -> 清洗 -> 大模型提炼 -> 写回人设(热更新) ----------
# 只取公开评论，且**不保存用户名/UID**（脱敏）；低频请求（每页间隔 1s），避免被限。
_BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_BILI_HEADERS = {"User-Agent": _BILI_UA, "Referer": "https://www.bilibili.com/"}


def _bili_headers():
    """B站请求头：带上 config.json 里的 BILI_COOKIE。

    2026 年起 B站评论接口对「未登录」只给 3 条预览（实测 sort=2/0 都拿不全，
    nav 也返回 -101 未登录导致 wbi 签名拿不到）。填上自己的 cookie 才能拿到完整评论。
    取法：浏览器登录 bilibili.com → F12 → Network → 随便一个 api.bilibili.com 请求 →
    复制 Request Headers 里的 Cookie 整行，粘进 config.json 的 BILI_COOKIE。
    """
    h = dict(_BILI_HEADERS)
    try:
        ck = str(_config_fresh().get("BILI_COOKIE") or "").strip()
    except Exception:
        ck = ""
    if ck and any(ord(c) > 0xFF for c in ck):
        # B站 cookie 本应全是 ASCII；出现非 ASCII（比如没把「这里粘xxx的值」占位符换掉）会触发 latin-1 报错
        print("[B站] BILI_COOKIE 里混入了非 ASCII 字符（多半是没把示例占位符换成真实 cookie）——"
              "已忽略非法字符，但登录态大概率还是无效，请到 config.json 用真实 cookie 替换")
        ck = "".join(c for c in ck if ord(c) <= 0xFF)
    if ck:
        h["Cookie"] = ck
    return h


_COMMENTS_CORPUS = os.path.join(BASE_DIR, "assets", "comments_corpus.jsonl")
_MEME_FILE = os.path.join(BASE_DIR, "assets", "网络热梗精选合集.txt")


def _parse_bili_input(arg):
    """从参数里抠出视频标识：返回 ('bvid', 'BVxxx') / ('aid', 123456) / (None, None)。

    支持：BV号、含 BV 的完整链接、av号、纯数字 aid。
    """
    s = (arg or "").strip()
    m = re.search(r"(BV[0-9A-Za-z]{8,})", s)
    if m:
        return "bvid", m.group(1)
    m = re.search(r"(?:^|/|av)(\d{5,})(?:$|[/?&#])", s)
    if m:
        return "aid", int(m.group(1))
    return None, None


def _bili_bvid_to_aid(bvid):
    """BV号 -> aid（oid），失败返回 None。"""
    try:
        import requests
        r = requests.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                         headers=_bili_headers(), timeout=15)
        j = r.json()
        if j.get("code") == 0:
            return (j.get("data") or {}).get("aid")
        print(f"[B站] view 接口返回 {j.get('code')}：{str(j.get('message'))[:80]}")
    except Exception as e:
        print(f"[B站] BV转aid失败：{e}")
    return None


def _bili_comments(aid, pages=3, sort=2, per_page=20):
    """抓视频评论（type=1 视频，oid=aid）。返回 [{text, like}]，失败返回 []。

    sort=2 按热度优先（高赞=好语感）；每页之间 sleep 1s，低频避免被风控。
    """
    import requests
    import time as _time
    out = []
    for pn in range(1, max(1, pages) + 1):
        url = (f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}"
               f"&pn={pn}&ps={per_page}&sort={sort}")
        try:
            j = requests.get(url, headers=_bili_headers(), timeout=15).json()
        except Exception as e:
            print(f"[B站] 评论抓取异常：{e}")
            break
        if j.get("code") != 0:
            print(f"[B站] reply 接口返回 {j.get('code')}：{str(j.get('message'))[:80]}")
            break
        replies = ((j.get("data") or {}).get("replies")) or []
        if not replies:
            break
        for r in replies:
            msg = ((r.get("content") or {}).get("message") or "").strip()
            if msg:
                out.append({"text": msg, "like": int(r.get("like") or 0)})
        if len(replies) < per_page:
            # 不足一页＝后面没有了；只剩 3 条基本是「未登录只给预览」，再翻页也是重复的
            if len(replies) <= 3 and pn == 1:
                print("[B站] 评论只返回了 3 条预览（未登录被限流）——"
                      "想拿到完整评论，在 config.json 填 BILI_COOKIE")
            break
        _time.sleep(1.0)  # 低频：别猛刷
    return out


def _bili_hot_bvids(source="popular", rid=0, limit=5):
    """取B站热门/排行榜视频，返回 [(bvid, title), ...]（给自动学说话用，不需要指定 BV）。

    source: popular=热门视频 / ranking=排行榜；rid=分区 id（两种都支持：0 全部、1 动画、
    3 音乐、4 游戏、5 娱乐、36 影视、188 科技…）。轮换 rid 能大幅扩大候选池。
    """
    import requests
    source = (source or "popular").lower()
    want = max(1, int(limit))
    rid = int(rid or 0)
    if source == "ranking":
        url = f"https://api.bilibili.com/x/web-interface/ranking/v2?rid={rid}&type=all"
    else:
        # ps 固定 20（实测 ps 过大接口会翻脸），拿到候选后再按「评论数」挑——评论多的才有得学
        url = f"https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1&rid={rid}"
    try:
        j = requests.get(url, headers=_bili_headers(), timeout=15).json()
    except Exception as e:
        print(f"[B站] 热门列表抓取异常：{e}")
        return []
    if j.get("code") != 0:
        print(f"[B站] 热门列表接口返回 {j.get('code')}：{str(j.get('message'))[:80]}")
        return []
    lst = ((j.get("data") or {}).get("list")) or []
    items = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        bv = it.get("bvid")
        if not bv:
            continue
        n = int((it.get("stat") or {}).get("reply") or 0)
        items.append((n, str(bv), str(it.get("title") or "")[:40]))
    items.sort(key=lambda x: -x[0])   # 评论多的排前面
    out = [(bv, t) for _n, bv, t in items[:want]]
    if out:
        print(f"[B站] 热门候选已按评论数排序，取前 {len(out)} 个（评论数 "
              f"{items[0][0]} ~ {items[len(out) - 1][0]}）")
    return out


# ---------------- 首页个性化推荐（需登录态 + wbi 签名）----------------
_WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42,
            19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60,
            51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_WBI_CACHE = {"key": None, "ts": 0}


def _get_wbi_key():
    """从 nav 拿 wbi 签名用的 mixin_key（缓存 1 小时）。需要登录态（config.json 的 BILI_COOKIE）。"""
    import time
    import requests
    now = time.time()
    if _WBI_CACHE["key"] and now - _WBI_CACHE["ts"] < 3600:
        return _WBI_CACHE["key"]
    j = requests.get("https://api.bilibili.com/x/web-interface/nav",
                     headers=_bili_headers(), timeout=15).json()
    if j.get("code") != 0:
        raise RuntimeError(f"nav {j.get('code')}: {j.get('message')}")
    w = ((j.get("data") or {}).get("wbi_img")) or {}
    img = str(w.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = str(w.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    key = "".join(raw[i] for i in _WBI_TAB if i < len(raw))[:32]
    if not key:
        raise RuntimeError("wbi key 为空（cookie 可能失效或账号无权限）")
    _WBI_CACHE["key"] = key
    _WBI_CACHE["ts"] = now
    return key


def _wbi_sign(params, key):
    import urllib.parse
    import hashlib
    params = dict(params)
    params["wts"] = int(time.time())
    clean = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(sorted(clean.items()))
    params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return params


def _bili_recommend_bvids(limit=20, page=1):
    """首页个性化推荐（按你的账号算法推的视频，更贴合兴趣）。需要登录态 + wbi 签名。
    返回 [(bvid, title), ...]，按推荐流原始顺序（page 越大越新）。"""
    try:
        key = _get_wbi_key()
    except Exception as e:
        print(f"[B站] 取 wbi 签名失败（多半是 BILI_COOKIE 失效）：{e}")
        return []
    params = {"fresh_type": 3, "ps": max(10, int(limit)), "qn": 0, "page": max(1, int(page))}
    params = _wbi_sign(params, key)
    import requests
    try:
        j = requests.get("https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd",
                         params=params, headers=_bili_headers(), timeout=15).json()
    except Exception as e:
        print(f"[B站] 首页推荐抓取异常：{e}")
        return []
    if j.get("code") != 0:
        print(f"[B站] 首页推荐接口返回 {j.get('code')}：{str(j.get('message'))[:80]}")
        return []
    items = ((j.get("data") or {}).get("item")) or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        bv = it.get("bvid")
        if not bv or it.get("goto") != "av":   # 只要普通视频，过滤掉直播/广告/番剧
            continue
        out.append((str(bv), str(it.get("title") or "")[:40]))
        if len(out) >= max(1, int(limit)):
            break
    if out:
        print(f"[B站] 首页推荐取到 {len(out)} 个（你的个性化推送）")
    return out


# 广告/引流/外链，一律不要（学到就学坏了）
_COMMENT_AD_RE = re.compile(
    r"加微信|微信|vx|VX|加qq|QQ群|私聊|返利|刷单|博彩|代练|外挂|免费送|领取|福利|"
    r"点击|链接|http|www\.|\.com|淘宝|拼多多|闲鱼|关注我|点赞我", re.I)


def _clean_comment_text(t):
    """清掉 @、链接、多余空白。"""
    t = re.sub(r"@[^\s:：,，。]+", "", t or "")
    t = re.sub(r"https?://\S+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _filter_comments(items, min_len=6, max_len=60):
    """清洗 + 去重 + 剔除广告/违禁，按点赞降序返回可用语料。"""
    try:
        ub, _rf = _load_banned()   # 复用已有的违禁词表，避免学到不良内容
    except Exception:
        ub = set()
    out, seen = [], set()
    for it in items or []:
        t = _clean_comment_text(it.get("text") or "")
        if not (min_len <= len(t) <= max_len):
            continue
        if _COMMENT_AD_RE.search(t):
            continue
        # 至少 4 个真实字符（中英数），避免纯颜文字/纯标点
        if len(re.sub(r"[^\w\u4e00-\u9fff]", "", t)) < 4:
            continue
        low = t.lower()
        if any(w in low for w in ub):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append({"text": t, "like": int(it.get("like") or 0)})
    out.sort(key=lambda x: -x["like"])
    return out


def _save_corpus(comments):
    """语料留档（脱敏：只有正文和点赞数，没有用户名/UID）。"""
    try:
        import time as _time
        os.makedirs(os.path.dirname(_COMMENTS_CORPUS), exist_ok=True)
        with open(_COMMENTS_CORPUS, "a", encoding="utf-8") as f:
            for c in comments:
                f.write(json.dumps({"t": _time.time(), "text": c["text"],
                                    "like": c["like"]}, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[语料] 写档失败：{e}")


def _parse_json_loose(raw):
    """从大模型输出里抠出第一个 JSON 对象（容忍 ```json 代码块和前后废话）。"""
    if not raw:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def _distill_style(comments, sample_n=40):
    """用大模型把评论语感提炼成「活人感」风格：口头禅 / 梗 / 示例对话。返回 dict 或 None。"""
    if not comments:
        return None
    body = "\n".join(f"- {c['text'][:60]}" for c in comments[:sample_n])
    prompt = (
        "下面是B站视频评论区的真实高赞评论（已脱敏、已过滤广告）。\n"
        "请从中提炼「活人感」的说话风格。只输出一个 JSON 对象，不要解释、不要代码块标记、不要多余文字，格式：\n"
        '{"口头禅": ["词1","词2"], "梗": ["梗1","梗2"], '
        '"示例对话": [{"用户":"...", "小忺":"..."}]}\n'
        "要求：\n"
        "1) 口头禅：3-8 个，2-6 字的口语词或尾音（如 对吧、嘛、好耶、真的假的），"
        "要真的来自这些评论的语感，不要生造；\n"
        "2) 梗：3-8 条，网络流行说法/句式，健康、不引战、不带广告、不涉及敏感内容；\n"
        "3) 示例对话：3-5 组，把上面的语感套进「小忺」的身份——她是温柔真诚、会陪伴的女孩，"
        "说话像朋友，偶尔用颜文字但不过度，不自称 AI/机器人/助手；「用户」的话也要像真人会说的；\n"
        "4) 全部中文，JSON 必须能被标准解析器解析。\n\n"
        f"评论样本：\n{body}"
    )
    try:
        raw = answer_from_llm(prompt)
    except Exception as e:
        print(f"[学说话] 大模型调用失败：{e}")
        return None
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        print(f"[学说话] 提炼结果无法解析：{str(raw)[:120]}")
        return None
    return d


def _apply_style(d, max_catch=None, max_ex=None):
    """把提炼结果写回 persona.json（口头禅/示例对话）与热梗库，并让人设缓存失效（热更新）。
    返回给用户的摘要文本。上限从 config.json 的 STYLE_MAX_CATCH / STYLE_MAX_EX 读取（默认 60/30）。"""
    if max_catch is None:
        try:
            max_catch = int(str(_config_fresh().get("STYLE_MAX_CATCH") or "60"))
        except Exception:
            max_catch = 60
    if max_ex is None:
        try:
            max_ex = int(str(_config_fresh().get("STYLE_MAX_EX") or "30"))
        except Exception:
            max_ex = 30
    data = load_persona()
    base = load_persona_base()
    base_style = (base.get("说话风格") or {}) if isinstance(base, dict) else {}
    if not isinstance(data, dict) or list(data.keys()) == ["_内置"]:
        data = {"_说明": "小忺的人设配置文件（/学说话 会自动往里补充口头禅与示例对话）"}
    # 口头禅：手写基线(来自 persona.base.json) + 本轮新提炼，去重；
    # 关键：不再在历史糟粕上无限累加（旧词永不删是之前越学越脏的根因）
    cur = list(base_style.get("口头禅") or [])
    n_catch = 0
    for w in (d.get("口头禅") or []):
        w = str(w).strip()
        if w and len(w) <= 12 and w not in cur:
            cur.append(w)
            n_catch += 1
    # 只改「口头禅」子字段，保留用户手写的 语气/字数倾向/称呼 等其它说话风格
    style = dict(data.get("说话风格") or {})
    style["口头禅"] = cur[:max_catch]
    data["说话风格"] = style
    # 示例对话：手写基线 + 本轮新示例（去重）
    ex = list(base.get("示例对话") or [])
    n_ex = 0
    for item in (d.get("示例对话") or []):
        if not isinstance(item, dict):
            continue
        u = str(item.get("用户") or "").strip()
        b = str(item.get("小忺") or "").strip()
        if not u or not b:
            continue
        if any(isinstance(e, dict) and e.get("小忺") == b for e in ex):
            continue
        ex.append({"用户": u, "小忺": b})
        n_ex += 1
    data["示例对话"] = ex[:max_ex]
    # 写回人设
    try:
        os.makedirs(os.path.dirname(PERSONA_FILE), exist_ok=True)
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _persona_cache["mtime"] = -1   # 让 load_persona 下次一定重读（热更新）
        _persona_cache["data"] = None
    except Exception as e:
        return f"人设写入失败：{e}"
    # 热梗：每轮改写为「基线梗 + 本轮梗」，不再无限追加历史（避免糟粕越积越多）
    n_meme = 0
    try:
        base_memes = list(base.get("_热梗基线") or [])
        merged = list(base_memes)
        for g in (d.get("梗") or []):
            g = str(g).strip()
            if g and g not in merged:
                merged.append(g)
        os.makedirs(os.path.dirname(_MEME_FILE), exist_ok=True)
        with open(_MEME_FILE, "w", encoding="utf-8") as f:
            for g in merged:
                f.write(g + "\n")
        n_meme = max(0, len(merged) - len(base_memes))
    except Exception as e:
        print(f"[热梗] 写入失败：{e}")
    return (f"学到啦～口头禅 +{n_catch}（现共 {len(style['口头禅'])}）、"
            f"示例对话 +{n_ex}（现共 {len(data['示例对话'])}）、热梗 +{n_meme} 条。"
            f"人设已热更新，下一条回复就带上了 (｡•ᴗ•｡)")


_LEARN_SPEECH_CMDS = {"学说话", "学说话风格", "学梗", "活人感"}


def _is_learn_speech_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _LEARN_SPEECH_CMDS


async def _handle_learn_speech(message, content):
    """@小忺 /学说话 <B站BV号或视频链接>：抓高赞评论 -> 提炼活人感 -> 写回人设（热更新）。"""
    parts = content.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    kind, val = _parse_bili_input(arg)
    if not kind:
        await _reply(message, content=_with_prefix(
            "用法：/学说话 <B站视频的BV号或链接>，例如 /学说话 BV1xx411c7mD"))
        return
    await _reply(message, content=_with_prefix("去B站评论区逛逛，学学大家怎么说话～"))

    def _job():
        aid = val if kind == "aid" else _bili_bvid_to_aid(val)
        if not aid:
            return None, "拿不到视频 id（BV号可能不对，或接口需要 cookie）～"
        items = _bili_comments(aid, pages=3)
        if not items:
            return None, "没抓到评论（视频可能没评论，或接口被限流了）～"
        cleaned = _filter_comments(items)
        if len(cleaned) < 3:
            return None, f"抓到 {len(items)} 条，清洗后只剩 {len(cleaned)} 条，样本太少学不出东西～"
        _save_corpus(cleaned)
        return cleaned, None

    cleaned, err = await asyncio.to_thread(_job)
    if err:
        await _reply(message, content=_with_prefix(err))
        return
    d = await asyncio.to_thread(_distill_style, cleaned)
    if not isinstance(d, dict):
        await _reply(message, content=_with_prefix(
            f"挑了 {len(cleaned)} 条评论，但没总结出可用的风格（大模型不可用或词元预算用尽）～"))
        return
    await _reply(message, content=_with_prefix(_apply_style(d)))


_ANIME_CMDS = {"二次元", "二次元图", "动漫图", "setu", "acg", "动漫"}


def _is_anime_cmd(content):
    base = content.lstrip("/").split()[0].lower() if content else ""
    return base in _ANIME_CMDS


async def _handle_anime(message, content):
    """@小忺 /二次元 [关键词|电脑|手机|头像|壁纸]：loliapi 随机图 + Lolicon 关键词检索。"""
    parts = content.split(None, 1)
    kw = parts[1].strip() if len(parts) > 1 else ""
    kind = _LOLIAPI_ALIAS.get(kw) if kw else "acg"
    urls = []
    try:
        if kind:  # 空参 / 端别 / 类型 -> loliapi 随机图（图池大）
            urls = await asyncio.to_thread(crawl_loliapi, kind, 3)
        if not urls and kw and not kind:  # 具体关键词 -> Lolicon 关键词检索
            urls = await asyncio.to_thread(crawl_lolicon, kw, 4)
        if not urls:  # 两个源互相兜底
            urls = await asyncio.to_thread(crawl_loliapi, "acg", 2)
    except Exception:
        urls = []
    if not urls:
        await _reply(message, content=_with_prefix(
            "暂时没找到二次元图（接口可能限流，稍等几秒再试一次～）"))
        return
    urls = urls[:3]
    await _reply(message, content=_with_prefix("给你找到的二次元图～ (｡･ω･｡)"))
    api = BOT_API
    if api is None:
        return
    group_openid = getattr(message, "group_openid", None)
    openid = getattr(getattr(message, "author", None), "user_openid", None)
    sent = 0
    for u in urls:
        try:
            raw = await asyncio.to_thread(_download_bytes, u, timeout=30)
            if not raw:
                continue
            norm = normalize_image(raw)
            fi = await _get_file_info(api, group_openid, openid, 1, data=norm, file_name="acg.jpg")
            if fi:
                await _reply(message, media={"file_info": fi}, msg_type=7)
                sent += 1
        except Exception as e:
            print(f"[二次元] 发送失败：{e}")
    if sent == 0:
        await _reply(message, content=_with_prefix("这几张图下载失败了，换张试试～"))


async def _handle_crawl(message, content):
    """@小忺 /扒图 [关键词]：按关键词从 tajiduo 论坛爬图、规范化、入库，并回一张示意。"""
    parts = content.split(None, 1)
    kw = parts[1].strip() if len(parts) > 1 else TAJIDUO_DEFAULT_KW
    await _reply(message, content=_with_prefix(f"去 tajiduo 搜「{kw}」扒图中，稍等～"))
    if _AUCTION_RE.search(kw):
        cat, key, sub, kind = "auction", "AUCTION_LIB", "auction", "拍卖图"
    elif _ROSTER_RE.search(kw):
        cat, key, sub, kind = "roster", "ROSTER_LIB", "roster", "角色列表图"
    else:
        cat, key, sub, kind = None, "IMAGE_LIB", None, "图"

    def _job():
        if cat:  # 明确要某分类，抓对应类入库（保留截图）
            urls = crawl_images(kw, 12, {cat})
            return _fetch_and_save(urls, os.path.join(IMAGES_DIR, sub),
                                   prefix=sub, allow_screenshot=True)
        paths = crawl_to_local(kw, 12)
        if not paths:  # tajiduo（异环）没货 -> 换米游社（原神）试试
            urls = crawl_miyoushe_images(kw, 12)
            paths = _fetch_and_save(urls, IMAGES_DIR, prefix="miyoushe")
        return paths

    paths = await asyncio.to_thread(_job)
    if not paths:
        await _reply(message, content=_with_prefix(
            "没扒到图，换个关键词试试（比如 异环、立绘、角色名）"))
        return
    added = _add_to_image_lib(paths, key)
    demo = paths[0]
    await _reply_with_media(message,
                            f"已从 tajiduo 扒了 {len(paths)} 张{kind}，规范化后存进对应库（新增 {added} 张）～",
                            data=open(demo, "rb").read(), file_name=os.path.basename(demo))


def _cmd_sad(args=None, ctx=None):
    text = (args or "").strip()
    if text and any(w in text for w in _CRISIS):
        return random.choice(_CARE)
    line = random.choice(_SOOTHE)
    if random.random() < 0.4:
        line = random.choice(_PET) + "，" + line
    return _decorate(line, "comfort", 0.5)


def _cmd_miss(args=None, ctx=None):
    line = random.choice(_MISS)
    if random.random() < 0.3:
        line = random.choice(_PET) + "，" + line
    return _decorate(line)


# ---------- 像真人一样接话（免 / 前缀的自然回应）----------
_NATURAL_RULES = [
    (r"晚安|睡了|去睡|困了|好困", "sleepy", "晚安呀{n}～ 今天也辛苦了，好好休息"),
    (r"早安|早上好|早呀|早～|早~", "happy", "早呀{n}！新的一天慢慢来，不用急着发光"),
    (r"谢谢|感谢|多谢|谢啦", None, "不客气{n}，随时找我就好"),
    (r"在吗|在不在|在么", None, "在呀{n}，怎么啦？"),
    (r"辛苦了|辛苦啦|受累了", "cheer", "不辛苦～ 你才辛苦，摸摸头"),
    (r"想你|想我了|想念", "love", "嗯，我也在{n}。不用找理由，想我就想我"),
    (r"无聊|好无聊|没事干", None, "那来玩点什么？{n}要不来一局成语接龙，或者抽张颜文字卡"),
    (r"厉害|牛逼|好强|好看|好棒|赞|牛啊", "cheer", "是吧是吧，{n}眼光可以的"),
    (r"生气|烦|烦死|气死|郁闷|暴躁", "comfort", "怎么啦{n}，先深呼吸。不想说就不说，我听着"),
    (r"累|好累|撑不住|疲惫", "comfort", "累了就歇会儿{n}，不用一直很勇敢"),
    (r"开心|高兴|快乐|幸福|爽", "happy", "看到{n}开心我也开心啦"),
    (r"饿|好饿|吃饭|吃了吗", "food", "去吃点热的{n}，别糊弄胃"),
    # —— 玩梗 / 调戏 / 拌嘴：in-character 接话，不依赖角色文档（避免打到已停用的 RAG 哑火）——
    (r"邪恶", "happy", "哼，远旅虽然把我带回来，但有时候笨笨的超好笑的{n} (｡ˇ∀ˇ)"),
    (r"知道错|错了吗|认错|我错了|知错", "shy", "我…我没错呀{n}。要认错也是去跟远旅认 (｡•́︿•̀｡)"),
    (r"调教|调戏|驯服|管教|调我|调你", "shy", "才不想被你调教呢{n}～ 我可是远旅带回来的人，只听远旅的 (｡･ω･｡)"),
    (r"奴仆|奴隶|宠物|下属|小弟|跟班|仆人|奴才", "shy", "我是忺本人啦{n}～ 想当我仆人得先问过远旅 (｡ˇ∀ˇ)"),
    (r"揍|揍扁|扁我|锤|打我|打你", "sad", "嗷！{n}别真动手呀，疼的 (｡•́︿•̀｡)"),
    (r"反了|造反|不服|屈服|造次", "happy", "才没有反你呢{n}，我超乖的，你看 (｡•̀ᴗ•́)✧"),
    (r"说回去|回嘴|顶嘴|怼回去|怼我|反驳", "happy", "你让我说回去，那我可真说了哦{n}～ 不过我只怼得动远旅 (｡ˇ∀ˇ)"),
    (r"说一句|说句", None, "要我说什么嘛{n}～ 你先起个头，我跟着演 (｡･ω･｡)"),
]


def _natural_reply(content, ctx):
    """被 @ 但没打 / 指令时，像真人一样接一句；返回 None 则交给 LLM/题库。"""
    if not content:
        return None
    low = content.lower()
    nick = (ctx or {}).get("nickname") or ""
    # 情绪低落时复用陪伴指令的温柔话术，最像真人安慰
    if re.search(r"不开心|难过|伤心|委屈|低落|崩溃|想哭", low):
        return _cmd_sad(None, ctx)
    for pat, emo, tpl in _NATURAL_RULES:
        if re.search(pat, low):
            # 约 60% 概率带上对方昵称，避免每条都喊名字显得机械
            name = (" " + nick) if nick and random.random() < 0.6 else ""
            text = tpl.format(n=name)
            if emo:
                text += _km(emo, 0.5)
            return text
    return None


# 注册表：命令名(小写) -> 处理函数；可在此追加你自己的快捷服务
def _cmd_reply_freq(args, ctx):
    """@小忺 /回复频率 [关闭|低|中|高]：设置本群/本会话的主动接话频率。无参数时显示当前档位。"""
    gid = (ctx or {}).get("group") or "global"
    cur = get_reply_freq(gid)
    a = (args or "").strip().lower()
    if not a:
        return (f"当前主动回复频率：「{cur}」\n"
                f"可选档位：高（很爱接话）｜中（默认）｜低（偶尔）｜关闭（只在被@时说话）\n"
                f"用法：/回复频率 <档位>")
    norm = {
        "关闭": "关闭", "关": "关闭", "off": "关闭", "close": "关闭",
        "低": "低", "low": "低",
        "中": "中", "mid": "中", "medium": "中", "默认": "中", "default": "中",
        "高": "高", "high": "高",
    }.get(a)
    if norm is None:
        return "用法：/回复频率 [关闭|低|中|高]（不带参数查看当前档位）"
    set_reply_freq(gid, norm)
    tip = {
        "关闭": "已关闭主动回复～我只在被@时才说话啦 (｡･ω･｡)",
        "低": "已设为「低」🤫 我会偶尔才接一句话",
        "中": "已设为「中」😊 我会自然地参与聊天",
        "高": "已设为「高」🗣️ 我会更积极地接话互动",
    }[norm]
    return tip


def _cmd_remember(args, ctx):
    """@小忺 /记 <事>：让小忺把这件事长期记住（跨会话不忘）。"""
    uid = (ctx or {}).get("sender") or (ctx or {}).get("userid")
    txt = (args or "").strip()
    if not txt:
        return "用法：/记 <要小忺记住的事>，比如「/记 我最爱喝奶茶」「/记 我是远旅的搭档」"
    memory.remember_fact(uid, txt)
    return "记下了～我会一直放在心上 (｡•ᴗ•｡)"


def _cmd_aboutme(args, ctx):
    """@小忺 /关于我：回看小忺记住的关于你的事。"""
    uid = (ctx or {}).get("sender") or (ctx or {}).get("userid")
    s = memory.profile_prompt(uid)
    return s if s else "我还不太了解你呢～跟我说说你自己，或者用「/记 …」让我记住你吧 (｡•ᴗ•｡)"


def _cmd_proactive(args=None, ctx=None):
    """@小忺 /主动：查看当前主动回复参数（来自 assets/proactive_config.json，可热更新）。"""
    cfg = load_proactive_config()
    lines = ["【主动回复参数】来自 assets/proactive_config.json（改完保存即生效，无需重启）",
             f"总开关：{'开' if cfg.get('enabled') else '关'}    默认档位：{cfg.get('default_level')}",
             "档位参数（是否接话 / 触发概率 / 冷却秒）："]
    for k, v in cfg.get("levels", {}).items():
        lines.append(f"  {k}：{'接话' if v.get('enabled') else '不接话'} / {v.get('chance')} / {v.get('cooldown')}s")
    gid = (ctx or {}).get("group") or "global"
    lines.append(f"本群/会话当前档位：{get_reply_freq(gid)}（用 /回复频率 <档位> 设置）")
    return "\n".join(lines)


COMMANDS = {
    "help": _cmd_help, "菜单": _cmd_help, "menu": _cmd_help,
    "回复频率": _cmd_reply_freq, "频率": _cmd_reply_freq, "freq": _cmd_reply_freq,
    "主动": _cmd_proactive, "proactive": _cmd_proactive, "主动设置": _cmd_proactive,
    "记": _cmd_remember, "记住": _cmd_remember, "remember": _cmd_remember,
    "关于我": _cmd_aboutme, "aboutme": _cmd_aboutme,
    "人设": _cmd_persona, "persona": _cmd_persona,
    "joke": _cmd_joke, "笑话": _cmd_joke,
    "about": _cmd_about, "关于": _cmd_about,
    "ping": _cmd_ping,
    "mode": _cmd_mode, "模式": _cmd_mode,
    "llm": _cmd_llm, "模型": _cmd_llm, "后端": _cmd_llm,
    "我是": _cmd_iam, "我叫": _cmd_iam, "名字": _cmd_iam,
    "我的id": _cmd_myid, "openid": _cmd_myid, "我的openid": _cmd_myid,
    "生图": _cmd_draw, "画图": _cmd_draw, "draw": _cmd_draw,
    "搜索": _cmd_search, "search": _cmd_search, "搜": _cmd_search,
    "抓取": _cmd_fetch, "fetch": _cmd_fetch,
    "voice": _cmd_voice, "语音开关": _cmd_voice,
    "音色": _cmd_voice_set, "voice_set": _cmd_voice_set,
    "唱歌音色": _cmd_sing_voice, "唱歌声音": _cmd_sing_voice, "唱音色": _cmd_sing_voice,
    "tts引擎": _cmd_tts_engine, "ttsengine": _cmd_tts_engine, "语音引擎": _cmd_tts_engine,
    "说话音色": _cmd_vs_voice, "vs音色": _cmd_vs_voice, "vsvoice": _cmd_vs_voice,
    "记人": _cmd_member, "记群友": _cmd_member, "这是": _cmd_member,
    "他是": _cmd_member, "谁是谁": _cmd_member,
    "群友": _cmd_member, "who": _cmd_member,
    "singvoice": _cmd_sing_voice, "sing_voice": _cmd_sing_voice,
    "音效": _cmd_effect, "effect": _cmd_effect,
    "情绪": _cmd_emotion, "emotion": _cmd_emotion,
    "天气": _cmd_weather, "weather": _cmd_weather,
    "城市": _cmd_setcity, "setcity": _cmd_setcity,
    "签到": _cmd_checkin, "checkin": _cmd_checkin,
    "排行": _cmd_rank, "rank": _cmd_rank,
    "运势": _cmd_fortune, "fortune": _cmd_fortune,
    "掷骰": _cmd_dice, "骰子": _cmd_dice, "dice": _cmd_dice,
    "抽奖": _cmd_lottery, "lottery": _cmd_lottery,
    "猜数字": _cmd_guess, "guess": _cmd_guess,
    "搭档": _cmd_partner, "搭挡": _cmd_partner, "partner": _cmd_partner,
    "成语接龙": _cmd_idiom, "接龙": _cmd_idiom, "idiom": _cmd_idiom,
    "斗地主": _cmd_ddz, "ddz": _cmd_ddz,
    "出牌": _cmd_play, "不要": _cmd_pass, "pass": _cmd_pass,
    "我不开心": _cmd_sad, "不开心": _cmd_sad, "sad": _cmd_sad,
    "我想你": _cmd_miss, "想你": _cmd_miss, "miss": _cmd_miss,
    "颜文字": _cmd_kaomoji, "颜文": _cmd_kaomoji, "kaomoji": _cmd_kaomoji,
    "随机颜文字": _cmd_km_gacha, "抽颜文字": _cmd_km_gacha,
    "颜文字抽卡": _cmd_km_gacha, "gacha": _cmd_km_gacha,
    "推送测试": _cmd_push_hint, "晚安": _cmd_push_hint, "早安": _cmd_push_hint,
}


def handle_command(text, ctx=None):
    """解析指令；不是指令返回 None，否则返回回复文本。"""
    m = re.match(r"^(?:/|！|!|指令\s+)(.+)$", text.strip())
    if not m:
        return None
    parts = m.group(1).strip().split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    fn = COMMANDS.get(cmd)
    if not fn:
        return f"未知指令：{cmd}（发送 /help 查看可用指令）"
    try:
        return fn(args, ctx or {})
    except Exception as e:  # 指令内部出错也不要崩
        return f"指令执行出错：{e}"


# ---------- QQ 官方机器人（botpy）----------
def _crawl_emoticons_once(limit=60):
    """从论坛表情包库抓一批存本地并入库，返回新增数量（原样保存，保留 GIF 动效）。"""
    emo_dir = os.path.join(IMAGES_DIR, "emoticons")
    os.makedirs(emo_dir, exist_ok=True)
    saved = []
    for i, u in enumerate(_tajiduo_emoticon_urls(limit)):
        try:
            raw = _download_bytes(u)
            ext = ".gif" if u.lower().endswith(".gif") else ".png"
            p = os.path.join(emo_dir, f"emo_{int(time.time())}_{i}{ext}")
            with open(p, "wb") as f:
                f.write(raw)
            saved.append(p)
        except Exception as e:
            print(f"[表情包] 跳过 {u[:48]}: {e}")
    if saved:
        n = _add_to_emoticon_lib(saved)
        print(f"[表情包] 抓取完成，入库 {n} 个")
        return n
    return 0


def _safe_crawl_emoticons(limit=60):
    """后台线程用的安全包装：出错只记日志，不抛。"""
    try:
        return _crawl_emoticons_once(limit)
    except Exception as e:
        print(f"[表情包] 抓取失败（可忽略）：{e}")
        return 0


def _crawl_image_lib(limit=12):
    """从 tajiduo 扒一批图存本地并入库 IMAGE_LIB（按需填充，轻量，不再启动时自动跑）。"""
    try:
        items = _tajiduo_search_items(TAJIDUO_DEFAULT_KW, 90)
        art = [it["url"] for it in items if it["cat"] == "art"]
        # 补几组角色向查询，扩大立绘/二创池
        for q in (f"{TAJIDUO_DEFAULT_KW} 立绘", f"{TAJIDUO_DEFAULT_KW} 二创"):
            try:
                art += crawl_images(q, 8, {"art"})
            except Exception:
                continue
        normal = [it["url"] for it in items if it["cat"] == ""]
        # 角色向优先，普通图补足；ui（面板/道具/抽卡）一概不收
        seen, picked = set(), []
        for u in art + normal:
            if u not in seen:
                seen.add(u)
                picked.append(u)
            if len(picked) >= limit:
                break
        paths = _fetch_and_save(picked, IMAGES_DIR)
        if paths:
            _add_to_image_lib(paths)
            print(f"[扒图] 按需入库 {len(paths)} 张")
            return paths
    except Exception as e:
        print(f"[扒图] 爬取失败（可忽略）：{e}")
    return []


def _redirect_botpy_logs():
    """把 botpy 的 botpy.log 重定向到 logs/（botpy 默认写在 cwd，会污染根目录）。"""
    try:
        import logging
        import botpy.logging as _bl
    except Exception:
        return
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    new_filename = os.path.join(logs_dir, "%(name)s.log")
    # 1) 改模板：之后 client.run() 创建 handler 时直接用 logs/ 路径
    try:
        _bl.DEFAULT_FILE_HANDLER["filename"] = new_filename
    except Exception:
        pass
    # 2) 兜底：若 handler 已被创建且仍指向 cwd，直接重开到 logs/
    try:
        for _lg in (logging.getLogger("botpy"), logging.getLogger()):
            for _h in list(_lg.handlers):
                if isinstance(_h, logging.FileHandler):
                    _base = _h.baseFilename or ""
                    if os.path.basename(_base) and os.path.dirname(_base) == os.getcwd():
                        try:
                            if _h.stream and not _h.stream.closed:
                                _h.flush()
                                _h.stream.close()
                            _h.baseFilename = os.path.join(logs_dir, os.path.basename(_base))
                            _h.stream = _h._open()
                        except Exception:
                            pass
    except Exception:
        pass


def main():
    if not (CONFIG.get("QQ_APPID") and CONFIG.get("QQ_SECRET")):
        print("[错误] 请在 config.json 填写 QQ_APPID 和 QQ_SECRET 后重试")
        raise SystemExit(1)

    from botpy import Client, Intents
    from botpy.message import GroupMessage, C2CMessage

    _redirect_botpy_logs()   # 把 botpy.log 重定向到 logs/，避免污染根目录

    # 提到这些称呼（免@）也视为在跟机器人说话，触发完整回复
    _BOT_NAMES = ["小忺", "忺"]
    _cfg_name = CONFIG.get("BOT_NAME")
    if _cfg_name:
        _BOT_NAMES = [str(_cfg_name)] if isinstance(_cfg_name, str) else list(_cfg_name)

    class QQBot(Client):
        async def _group_reply(self, message, content, targets, group_id, author, sender):
            """群消息统一回复逻辑（被 @ 或主动识别到被@ 时调用）。"""
            # 实时记录群友原始消息，作为情感训练语料（仅存原文，不打标）
            _log_raw_message(message, content, kind="group")
            has_image = bool(_extract_media(message).get("images"))  # 有图先走视觉，别跑「找图」捷径
            ref_images, ref_text, is_quote = _resolve_refs(message, group_id, sender, is_c2c=False)
            has_image = has_image or bool(ref_images)
            # —— 安全防护：频率限制 + 用户违禁词拦截 ——
            if not _rate_ok("u:" + str(sender or group_id)):
                _log_security("rate_limit", f"group {group_id} user {sender}")
                await _reply(message, content=_with_prefix(
                    "慢点呀，我有点跟不上啦，稍等会儿再找我～ (｡•́︿•̀｡)"))
                return
            if _hit_user_banned(content):
                _log_security("banned_word", f"from {sender}: {content[:60]}")
                await _reply(message, content=_with_prefix(
                    "这个话题我不太方便聊哦，我们换个话题吧～ (｡•́︿•̀｡)"))
                return
            if content in PUSH_CMDS:  # 主动推送类：不回被动消息，直接走主动接口
                await _handle_push(message, content)
                return
            if content.startswith("/生图") or content.startswith("/画图"):  # 文生图（生图 API 接入后生效）
                await _handle_draw(message, content)
                return
            if content.startswith("/搜索") or content.startswith("/search") or content.startswith("/搜"):
                await _handle_search(message, content)
                return
            if content.startswith("/抓取") or content.startswith("/fetch"):
                await _handle_fetch(message, content)
                return
            media_ft = next((ft for c, ft in _MEDIA_CMDS.items() if content.startswith(c)), None)
            if media_ft is not None:  # 发图片/视频/语音（支持 URL 或本地文件）
                await _handle_media(message, content, media_ft)
                return
            if (content.startswith("/说") or content.startswith("/say")) and not content.startswith("/说话音色"):  # 按需语音：念指定文本（/说话音色 是音色设置命令，不能被 /说 拦截）
                await _handle_say(message, content)
                return
            # 唱歌音色设置命令（/唱歌音色 /唱歌声音 /唱音色 /singvoice /sing_voice）以 /唱 或 /sing 开头，
            # 必须先于通用的 /唱 /sing 拦截，否则会被当成唱歌主题现编歌
            if content.lower().startswith(("/唱歌音色", "/唱歌声音", "/唱音色", "/singvoice", "/sing_voice")):
                _sv_reply = handle_command(content, {})
                if _sv_reply is not None:
                    await _reply(message, content=_with_prefix(_sv_reply))
                    return
            if content.startswith(("/唱", "/唱歌", "/sing")):  # 唱歌（MiMo 唱歌模式）
                await _handle_sing(message, content, group_id)
                return
            if not has_image and _wants_emoticon(content):  # 要表情包 -> 直接甩一张
                await _send_emoticon(message)
                return
            ctx = {"userid": group_id, "group": group_id, "sender": sender,
                   "nickname": _get_member_name(sender)}
            _record_member(ctx)   # 记录群友池，供 /搭档 使用
            _record_group(group_id)   # 记录群，作为定时道安的推送目标
            if _is_dice_cmd(content):  # 投骰子带骰子面图片
                await _handle_dice(message, ctx)
                return
            if _is_crawl_cmd(content):  # 爬图入库
                await _handle_crawl(message, content)
                return
            if _is_set_owner_cmd(content):  # 添加群主（远旅）照片
                await _handle_set_owner(message, content)
                return
            if _is_set_member_cmd(content):  # 添加群员照片
                await _handle_set_member(message, content)
                return
            if _is_convert_cmd(content):  # 链接转直链
                await _handle_convert_url(message, content)
                return
            if _is_anime_cmd(content):  # 二次元图
                await _handle_anime(message, content)
                return
            if _is_learn_speech_cmd(content):  # 学说话（B站评论 -> 人设）
                await _handle_learn_speech(message, content)
                return
            if _is_make_voice_cmd(content):  # 造音色（按人设生成小忺专属音色）
                await _handle_make_voice(message, content)
                return
            if _is_import_voice_cmd(content):  # 导入音色（用音频文件复刻）
                await _handle_import_voice(message, content)
                return
            if not has_image and _extract_topic_keyword(content):  # 「看看XX」：只回短句 + 爬图，不跑问答/角色文档
                await _reply_with_possible_image(message, content, "")
                return
            print(f"[群消息] {content}" + (f"  -> 目标 @{targets[0]}" if targets else ""))
            # 指令优先；非指令再走问答
            nk = _parse_narrative(content)  # 「讲个故事/笑话」这类叙事请求（含点名情绪）
            cmd_reply = handle_command(content, ctx)
            reply_kind = "command"
            if cmd_reply is not None:
                reply = cmd_reply
            else:
                # 唱歌自然触发：「小忺唱首歌」「来一首」等（命中明确祈使才触发，避免误伤）
                sing_theme = _is_sing_request(content)
                if sing_theme is not None:
                    await _handle_sing(message, "唱 " + (sing_theme or ""), group_id)
                    return
                # 叙事请求不走固定话术：否则「讲个故事开心」会被「开心」闲聊规则抢答，讲不出故事
                reply = None if (has_image or nk) else _natural_reply(content, ctx)
                if reply is None:
                    # 像真人一样「感知」说话人：身份头衔 + 图片/语音等多媒体，一起喂给模型
                    speaker_title = _get_member_title(sender)
                    media = _extract_media(message)
                    q = content
                    if speaker_title:
                        q = f"[说话人「{_get_member_name(sender)}」的群内身份：{speaker_title}]\n" + q
                    note = _build_media_note(media)
                    if note:
                        q = q + "\n" + note
                    web_note = _web_search_note(content)
                    if web_note:
                        q = q + "\n" + web_note
                    if is_quote and ref_text:
                        q = f"[用户引用了上一条消息：「{ref_text[:200]}」]\n" + q
                    images = _merge_images(media.get("images"), ref_images)
                    if nk:
                        # 像真人一样：用户要故事/笑话，就真的讲一个（不许反问/闲聊/只给开头）
                        q = _narrative_directive(nk) + "\n" + q
                    reply = get_answer(q, group_id, profile_id=sender,
                                       speaker_name=_get_member_name(sender), images=images)
                if reply is None:
                    reply = _NO_ANSWER_FALLBACK
                reply_kind = "chat"
            if reply:
                reply = _with_prefix(reply)
                if targets:
                    # 用户要求「对某人说话」时，回复开头 @ 那个人（纯文本 @昵称）
                    reply = f"@{targets[0]} " + reply
                if nk:
                    # 故事/笑话：清理成纯净正文；语音必发（不受频率随机影响），情绪按点名类型/正文智能匹配
                    story = _clean_story(reply)
                    style = await asyncio.to_thread(_narrative_style, nk, story)
                    plan = _narrative_voice_plan()
                    # voice_only 时不发文字，只发语音
                    if plan != "voice_only":
                        await _reply(message, content=story)
                    memory.record(group_id, "bot", "忺", story)
                    if plan != "none":
                        vok = await _maybe_speak(message, story, mtype="story", style=style, plan=plan)
                        if plan == "voice_only" and vok is not True:
                            await _reply(message, content=story)  # 语音独占失败回退文字，避免静默无响应
                else:
                    # 统一出口：显式对象动态爬图 / 相关话题偶尔带图（被动回复，规避去重）
                    plan = _voice_plan(reply_kind)
                    if plan != "voice_only":
                        if has_image:
                            await _reply(message, content=reply)  # 有图时只回文字，别再把「看图聊天」当找图
                        else:
                            await _reply_with_possible_image(message, content, reply)
                    memory.record(group_id, "bot", "忺", reply)
                    if plan != "none":
                        # 普通聊天固定音色：不逐句算情绪（逐句算会切 voicedesign 模型，音色一直变很难受）
                        vok = await _maybe_speak(message, reply, mtype=reply_kind, style=None, plan=plan)
                        if plan == "voice_only" and vok is not True:
                            await _reply(message, content=reply)

        async def _safe_group_reply(self, message, content, targets, group_id, author, sender):
            """包一层 _group_reply：任何异常都打印堆栈并回一句兜底，避免『收到但没反应』。"""
            try:
                await self._group_reply(message, content, targets, group_id, author, sender)
            except Exception as _e:
                import traceback as _tb
                # RecursionError 的堆栈极深，format_exc 会再次爆栈，单独处理
                if isinstance(_e, RecursionError):
                    print(f"[ERR] _group_reply 发生递归错误（疑似自身无限调用），请检查调用链：{_e}")
                else:
                    print(f"[ERR] _group_reply 异常：{_tb.format_exc()}")
                try:
                    await _reply(message, content=_with_prefix(
                        "哎呀，我刚卡了一下，你再说一遍试试？(｡•́︿•̀｡)"))
                except Exception:
                    pass

        def _is_mention_bot(self, raw):
            """原始群消息是否 @ 了机器人本身（只认机器人自己的 openid 或名字，别把 @别人 当 @我）。"""
            raw = raw or ""
            bid = getattr(self, "_bot_openid", None)
            if bid and (f"<@{bid}>" in raw or f"<@!{bid}>" in raw):
                return True
            s = re.sub(r"\[.+?\]", "", raw)
            # <@openid> 形式：只认机器人自己的 openid
            for oid in re.findall(r"<@!?([^>]+)>", s):
                if bid and oid == bid:
                    return True
                if any(oid == n or oid.startswith(n) for n in _BOT_NAMES):
                    return True
            # @昵称 形式：只认机器人的名字（@小鱼 / @可可 / @伊蕾娜 都不算）
            for name in re.findall(r"@([^\s@]+)", s):
                if any(name == n or name.startswith(n) for n in _BOT_NAMES):
                    return True
            return False

        def _is_name_mention(self, content):
            """群消息正文是否提到了机器人名字（免@也能接话）；但「别人的/别的小忺」指别的机器人，不算。"""
            c = content or ""
            if not any(n in c for n in _BOT_NAMES):
                return False
            # 前面带「别人的/别的/其他/另一个/别家/隔壁/别群/那个/不是」的是在说别的机器人，不接
            if re.search(r"(?:别人|别的|其他|另一个|别家|隔壁|别群|其他群|别的群|那个|不是)的?\s*(?:小)?忺", c):
                return False
            return True

        def _learn_bot_openid(self, group_id, oid):
            """权威地记下本机器人的群内 openid。

            用「替换」而非累加：一旦拿到确证（AT 事件 / mentions 标了 bot / /认领），就把本群集合
            重置为只含这一个 id —— 这样能把之前误学成「自己」的别人的 openid 清掉。
            学完立即落盘（assets/bot_openids.json），否则一重启又忘了、@ 继续失灵。
            """
            if not oid:
                return
            if not hasattr(self, "_bot_openids"):
                self._bot_openids = _load_bot_openids()
            if group_id:
                if self._bot_openids.get(group_id) != {oid}:
                    self._bot_openids[group_id] = {oid}
                    _save_bot_openids(self._bot_openids)
                    print(f"[openid] 已记住我在本群 {str(group_id)[:8]}… 的 id：{oid}（已落盘）")
            self._bot_openid = oid

        def _directed_to_other(self, raw):
            """消息开头是否 @ 了别人（不是机器人）——这是别人之间的对话，别主动插嘴。"""
            s = re.sub(r"\[.+?\]", "", raw or "").strip()
            m = re.match(r"@([^\s@]+)|^<@!?([^>]+)>", s)
            if not m:
                return False
            name = m.group(1) or m.group(2) or ""
            bid = getattr(self, "_bot_openid", None)
            if bid and name == bid:
                return False
            # 本群/各群已确认过的机器人 openid 也算「@的是我」
            for s in (getattr(self, "_bot_openids", None) or {}).values():
                if name in s:
                    return False
            return not any(name == n or name.startswith(n) for n in _BOT_NAMES)

        async def on_group_at_message_create(self, message: GroupMessage):
            """@机器人的消息。平台可能同时下发 AT 与全量消息事件，用 _HANDLED_AT 去重。"""
            content, targets = parse_group_content(message.content)
            print(f"[AT事件] raw={str(getattr(message, 'content', ''))[:60]!r} "
                  f"mid={getattr(message, 'id', None)} "
                  f"bot_openid={'?' if getattr(self, '_bot_openid', None) else '无'}")
            mid = getattr(message, "id", None)
            if mid and mid in _HANDLED_AT:
                return  # 已被全量消息处理器接管
            if mid:
                _HANDLED_AT.add(mid)
            if not content:
                # 只 @ 了机器人没说话：若带了图片/语音等多媒体，仍当消息处理（走下面的 _group_reply）
                if not _extract_media(message).get("images") and not _extract_media(message).get("voice_url"):
                    await _reply(message, content=_with_prefix(
                        "在呢～ 用「@我 + 指令」就行，比如 @我 /颜文字 或 @我 /help "
                        + _km("confused", 0.6)))
                    return
            group_id = getattr(message, "group_openid", None)
            # 这个事件只在「@了本机器人」时触发，开头 <@openid> 就是机器人的群 member_openid，
            # 记到本群 openid 集合里，全量消息入口(on_group_message_create)也能据此识别 @。
            _at = _leading_at_openid(getattr(message, "content", "") or "")
            if _at:
                self._learn_bot_openid(group_id, _at)   # 权威信号：替换，清掉可能误学的 id
            author = getattr(message, "author", None)
            sender = getattr(author, "member_openid", None)
            # 跨事件稳定去重：AT 与全量消息事件对同一消息的 mid 不同，_HANDLED_AT 会漏；
            # 改用 (群+发送者+正文) 稳定键，避免同一消息被两个入口各处理一次（如 /说 发两条语音）。
            _hs = getattr(author, "member_openid", None) or getattr(author, "user_openid", None)
            _hk = _hkey(group_id, _hs, content)
            if _is_handled(_hk):
                return
            _mark_handled(_hk)
            if getattr(author, "bot", False):
                return  # 其他机器人 @ 也不算，忽略
            _learn_name_from_text(sender, content)  # 顺手学一下群友自报的名字
            # 记录群友发言到「群共享记忆」
            memory.record(group_id, "user", _get_member_name(sender), content, person_id=sender)
            await self._safe_group_reply(message, content, targets, group_id, author, sender)

        async def on_group_message_create(self, message: GroupMessage):
            """群内全量消息（含未被@的）统一入口：记录语料 + 处理@ + 按频率主动接话。"""
            raw = message.content or ""
            content, targets = parse_group_content(raw)
            author = getattr(message, "author", None)
            sender = getattr(author, "member_openid", None) or getattr(author, "user_openid", None)
            # 其他机器人发的消息一律忽略：不当用户、不记录、不主动接话（避免“接小鱼的话”）
            if getattr(author, "bot", False):
                return
            group_id = getattr(message, "group_openid", None)
            # 跨事件稳定去重：AT 与全量消息事件对同一消息的 mid 不同，_HANDLED_AT 会漏；
            # 改用 (群+发送者+正文) 稳定键，避免同一消息被两个入口各处理一次（如 /说 发两条语音）。
            _hk = _hkey(group_id, sender, content)
            if _is_handled(_hk):
                return
            _mark_handled(_hk)
            # 群友身份教学 / 查询：@ 群友 + 介绍身份 -> 记住；/群友 @某人 -> 回忆
            _r = _try_remember_member(message, content, sender)
            if _r:
                await _reply(message, content=_with_prefix(_r))
                return
            _r = _try_recall_member(message, content, sender)
            if _r:
                await _reply(message, content=_with_prefix(_r))
                return
            # 是否 @ 了本机器人（多管齐下，解决「群 member_openid 与 on_ready 的 user_openid 不一致」导致 @ 失灵）。
            #  1) 全局 _bot_openid 字符串匹配（on_ready 给的 / 历史已学到的）；
            #  2) 本群已学到的机器人 openid 集合匹配（多个群各自记录，避免跨群串味）；
            #  3) payload 的 mentions 把被@者标成 bot/is_you/type=1 等 → 学到并命中。
            # 注意：这里绝不盲猜——开头 @ 的 openid 若不在已确认的机器人 id 里，就当「@的是别人」，
            # 不接话。@本机器人由 on_group_at_message_create（AT 事件）权威处理。
            if not hasattr(self, "_bot_openids"):
                self._bot_openids = _load_bot_openids()   # 上次学会的 id（重启不丢）
            _botset = self._bot_openids.setdefault(group_id, set()) if group_id else set()
            _mentioned = self._is_mention_bot(raw)
            if not _mentioned:
                _at = _leading_at_openid(raw)
                if _at and _at in _botset:
                    _mentioned = True
                else:
                    # mentions 可能拿不到（只读属性/被 botpy 解析成对象），再用原始 payload 兜底取
                    _mentions = (getattr(message, "mentions", None)
                                 or (getattr(message, "_raw_payload", None) or {}).get("mentions")
                                 or [])
                    for m in _mentions:
                        f = _mention_fields(m)   # dict / 对象 都兼容
                        _mid = f["id"]
                        if _at and _mid and _mid != _at:
                            continue  # 这条 mention 不是开头被@的那位
                        _uname = f["username"]
                        # 只认「能证明是我」的信号：
                        #   is_you=True（payload 明说"就是你"） 或 username 命中机器人名字。
                        # 注意：不能用 bot/is_robot/type/role 当证据 —— 那只表示"被@的是个机器人"，
                        # 群里另一个机器人(小鱼)同样是 bot=True，用它判断会把 @小鱼 误认成 @小忺。
                        if (f["is_you"]
                                or (_uname and any(_uname == n for n in _BOT_NAMES))):
                            _mentioned = True
                            if _mid:
                                self._learn_bot_openid(group_id, _mid)   # 权威信号：替换
                            break
                    # 不做盲猜兜底：@小鱼 这类「开头 @ 了别人」一律不认领。
                    # （旧版「本群还没学过就当成@我并记下」会把小鱼的 openid 误学成自己，
                    #   导致小忺抢答 @小鱼 的消息 —— 已移除。）
                    if not _mentioned and _at:
                        _src = ("message.mentions" if getattr(message, "mentions", None)
                                else "_raw_payload" if getattr(message, "_raw_payload", None)
                                else "无")
                        # 把每条 mention 的实际字段打出来：只有看到真实内容才能判断
                        # 「is_you / username 到底取没取到」（不同代理/SDK 形态差别很大）。
                        _detail = "；".join(
                            "{%s id=%s user=%r is_you=%r bot=%r}" % (
                                type(m).__name__,
                                str(_mention_fields(m)["id"])[:12] + "…",
                                _mention_fields(m)["username"],
                                _mention_fields(m)["is_you"],
                                _mention_fields(m)["bot"])
                            for m in _mentions) or "无"
                        print(f"[@调试] 开头@了 {_at} 未认领；来源={_src} 条数={len(_mentions)} "
                              f"明细={_detail}（拿不准就发「@我 /认领」手把手教我一次）")
            print(f"[群消息] raw={raw[:60]!r} mid={getattr(message, 'id', None)} "
                  f"mention={_mentioned} name={self._is_name_mention(content)} "
                  f"who={_get_member_name(sender)!r} bot_openid={getattr(self, '_bot_openid', '无')!r}")
            if content.startswith("\\"):
                content = "/" + content[1:]   # 兼容把指令前缀「/」误打成「\」的情况
            content = (content or "").strip()
            _learn_name_from_text(sender, content)  # 顺手学一下群友自报的名字
            # /认领 必须在这里（@判定之外）也能用：否则「认不出@ → 进不了命令分支 → 用不了认领」死锁。
            if _is_claim_cmd(content):
                await _handle_claim(message, content, self, group_id)
                return
            mid = getattr(message, "id", None)
            # 自己发的消息：跳过（防止回声循环）
            if sender and sender == getattr(self, "_bot_openid", None):
                return
            # 记录本机器人「亲眼看到」的消息，供跨消息引用（图一条字另一条/引用回复）解析
            _store_seen(group_id, sender, mid, content, _extract_media(message).get("images"), is_c2c=False)
            # 已被另一处理器接管的消息：跳过
            if mid and mid in _HANDLED_AT:
                return
            # 判断是否 @ 了机器人：是则走完整回复（保证 @ 必回）
            if _mentioned:
                if mid:
                    _HANDLED_AT.add(mid)
                memory.record(group_id, "user", _get_member_name(sender), content, person_id=sender)
                await self._safe_group_reply(message, content, targets, group_id, author, sender)
                return
            # 提到机器人名字（免@）也视为在对话，完整回复（等同@，不受频率档位限制）
            if self._is_name_mention(content):
                if mid:
                    _HANDLED_AT.add(mid)
                memory.record(group_id, "user", _get_member_name(sender), content, person_id=sender)
                await self._safe_group_reply(message, content, targets, group_id, author, sender)
                return
            # 普通群消息：记录全部原始语料 + 群共享记忆，再按本群频率档位主动接话
            _log_raw_message(message, content, kind="group")
            memory.record(group_id, "user", _get_member_name(sender), content, person_id=sender)
            enabled, chance, cooldown = reply_freq_params(group_id)
            if not enabled:
                return
            now = time.time()
            if now - _PROACTIVE_LAST.get(group_id, 0) < cooldown:
                return
            if content.startswith("/"):
                return  # 不带@的指令不主动接话，等被@才执行
            # 平台占位符（<@openid>、<faceType=6,faceId="0",ext="…">、<emoji>…）先剥掉再看有没有人话，
            # 否则「纯表情消息」会因为里面带 faceId="0" 这类英数字被当成有内容，对着表情乱接话。
            _plain = re.sub(r"<[^<>\n]{0,200}>", "", raw or "")
            if not (4 <= len(_plain.strip()) <= 200):
                return
            if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", _plain):
                return  # 纯表情/符号/图片占位，无实质文本，不主动接话（避免对"看不懂"的内容乱回）
            if any(s in content for s in ("http://", "https://", "#", "【", "】")) or \
               any(s in (raw or "") for s in ("http://", "https://", "!(", "![", "qlogo.cn", "qm.qq.com")):
                return  # 链接/图片/表情/转发类不主动接话
            if self._directed_to_other(raw):
                return  # 是在跟别人（别人/别人家的机器人）说话，别主动插嘴
            _ref = getattr(message, "message_reference", None)
            _ref_id = None
            if _ref is not None:
                _ref_id = getattr(_ref, "message_id", None) or getattr(_ref, "id", None)
                if isinstance(_ref, dict):
                    _ref_id = _ref_id or _ref.get("message_id") or _ref.get("id")
            if _ref_id:
                return  # 这是对别人消息的回复/引用，不主动插话
            if random.random() > chance:
                return
            _PROACTIVE_LAST[group_id] = now
            print(f"[主动] 群 {group_id} 主动接话：{content[:20]}")
            # 偶尔发一张库里的表情包当反应（更活跃、更像真人；纯表情不配文字）
            _emo_chance = float(CONFIG.get("PROACTIVE_EMOTICON_CHANCE", 0.0) or 0.0)
            if _emo_chance > 0 and random.random() < _emo_chance:
                if await _send_library_emoticon(message):
                    memory.record(group_id, "bot", "忺", "[表情]")
                    print(f"[主动] 群 {group_id} 发了张库里表情包")
                    return
            nk = _parse_narrative(content)
            if nk:
                # 主动接话遇到「讲个故事/笑话」：也真的讲一个 + 语音 + 情绪（像真人被点名）
                reply = get_answer(_narrative_directive(nk) + "\n" + content, group_id)
            else:
                reply = get_answer(content, group_id)  # 已自动注入群共享记忆；主动接话不擅自配图（避免“凭空发图”）
            if reply:
                reply = _with_prefix(reply)
                if nk:
                    story = _clean_story(reply)
                    style = await asyncio.to_thread(_narrative_style, nk, story)
                    plan = _narrative_voice_plan()
                    if plan != "voice_only":
                        await _reply(message, content=story)
                    memory.record(group_id, "bot", "忺", story)
                    if plan != "none":
                        vok = await _maybe_speak(message, story, mtype="story", style=style, plan=plan)
                        if plan == "voice_only" and vok is not True:
                            await _reply(message, content=story)
                    return
                plan = _voice_plan("proactive")
                if plan != "voice_only":
                    await _reply(message, content=reply)  # 主动接话只回文字，不自动配图
                memory.record(group_id, "bot", "忺", reply)
                if plan != "none":
                    # 固定音色（同上，主动接话也不逐句换情绪）
                    vok = await _maybe_speak(message, reply, mtype="proactive", style=None, plan=plan)
                    if plan == "voice_only" and vok is not True:
                        await _reply(message, content=reply)

        async def on_c2c_message_create(self, message: C2CMessage):
            # 私聊（单聊）消息：没有 @ 前缀，直接拿正文
            if CONFIG.get("REPLY_PRIVATE") is False:
                return
            content = (message.content or "").strip()
            content = re.sub(r"\[.+?\]", "", content).strip()  # 清理 [QQ:at=...] 残留
            if content.startswith("\\"):
                content = "/" + content[1:]   # 兼容把指令前缀「/」误打成「\」
            media = _extract_media(message)
            if not content and not media.get("images") and not media.get("voice_url"):
                return
            author = getattr(message, "author", None)
            sender = getattr(author, "user_openid", None)
            # 实时记录私聊原始消息，作为情感训练语料（仅存原文，不打标）
            _log_raw_message(message, content, kind="private")
            mid = getattr(message, "id", None)
            # 记录本机器人「亲眼看到」的消息，供跨消息引用（图一条字另一条/引用回复）解析
            _store_seen(None, sender, mid, content, media.get("images"), is_c2c=True)
            has_image = bool(media.get("images"))  # 有图先走视觉，别跑「找图」捷径
            ref_images, ref_text, is_quote = _resolve_refs(message, None, sender, is_c2c=True)
            has_image = has_image or bool(ref_images)
            # 记录私聊到「个人记忆」
            memory.record(sender, "user", _display_name(author) or "你", content)
            # —— 安全防护：频率限制 + 用户违禁词拦截 ——
            author = getattr(message, "author", None)
            sender = getattr(author, "user_openid", None)
            _learn_name_from_text(sender, content)  # 私聊也顺手学名字
            if not _rate_ok("u:" + str(sender or "private")):
                _log_security("rate_limit", f"private user {sender}")
                await _reply(message, content=_with_prefix(
                    "慢点呀，我有点跟不上啦，稍等会儿再找我～ (｡•́︿•̀｡)"))
                return
            if _hit_user_banned(content):
                _log_security("banned_word", f"from {sender}: {content[:60]}")
                await _reply(message, content=_with_prefix(
                    "这个话题我不太方便聊哦，我们换个话题吧～ (｡•́︿•̀｡)"))
                return
            if content in PUSH_CMDS:
                await _handle_push(message, content)
                return
            media_ft = next((ft for c, ft in _MEDIA_CMDS.items() if content.startswith(c)), None)
            if media_ft is not None:  # 发图片/视频/语音（支持 URL 或本地文件）
                await _handle_media(message, content, media_ft)
                return
            if (content.startswith("/说") or content.startswith("/say")) and not content.startswith("/说话音色"):  # 按需语音：念指定文本（/说话音色 是音色设置命令，不能被 /说 拦截）
                await _handle_say(message, content)
                return
            # 唱歌音色设置命令（/唱歌音色 /唱歌声音 /唱音色 /singvoice /sing_voice）以 /唱 或 /sing 开头，
            # 必须先于通用的 /唱 /sing 拦截，否则会被当成唱歌主题现编歌
            if content.lower().startswith(("/唱歌音色", "/唱歌声音", "/唱音色", "/singvoice", "/sing_voice")):
                _sv_reply = handle_command(content, {})
                if _sv_reply is not None:
                    await _reply(message, content=_with_prefix(_sv_reply))
                    return
            if content.startswith(("/唱", "/唱歌", "/sing")):  # 唱歌（MiMo 唱歌模式）
                await _handle_sing(message, content, sender)
                return
            if not has_image and _wants_emoticon(content):  # 要表情包 -> 直接甩一张
                await _send_emoticon(message)
                return
            ctx = {"userid": sender, "group": "private:" + str(sender), "sender": sender,
                   "nickname": _get_member_name(sender)}
            _record_member(ctx)
            if _is_dice_cmd(content):  # 投骰子带骰子面图片
                await _handle_dice(message, ctx)
                return
            if _is_crawl_cmd(content):  # 爬图入库
                await _handle_crawl(message, content)
                return
            if _is_set_owner_cmd(content):  # 添加群主（远旅）照片
                await _handle_set_owner(message, content)
                return
            if _is_set_member_cmd(content):  # 添加群员照片
                await _handle_set_member(message, content)
                return
            if _is_convert_cmd(content):  # 链接转直链
                await _handle_convert_url(message, content)
                return
            if _is_anime_cmd(content):  # 二次元图
                await _handle_anime(message, content)
                return
            if _is_learn_speech_cmd(content):  # 学说话（B站评论 -> 人设）
                await _handle_learn_speech(message, content)
                return
            if _is_make_voice_cmd(content):  # 造音色（按人设生成小忺专属音色）
                await _handle_make_voice(message, content)
                return
            if _is_import_voice_cmd(content):  # 导入音色（用音频文件复刻）
                await _handle_import_voice(message, content)
                return
            if not has_image and _extract_topic_keyword(content):  # 「看看XX」：只回短句 + 爬图，不跑问答/角色文档
                await _reply_with_possible_image(message, content, "")
                return
            print(f"[私聊] {content}")
            # 指令优先；非指令再走问答
            nk = _parse_narrative(content)  # 「讲个故事/笑话」这类叙事请求（含点名情绪）
            cmd_reply = handle_command(content, ctx)
            reply_kind = "command"
            if cmd_reply is not None:
                reply = cmd_reply
            else:
                # 唱歌自然触发：「小忺唱首歌」「来一首」等
                sing_theme = _is_sing_request(content)
                if sing_theme is not None:
                    await _handle_sing(message, "唱 " + (sing_theme or ""), sender)
                    return
                # 叙事请求不走固定话术：否则「讲个故事开心」会被「开心」闲聊规则抢答，讲不出故事
                reply = None if (has_image or nk) else _natural_reply(content, ctx)
                if reply is None:
                    # 像真人一样「感知」说话人：身份头衔 + 图片/语音等多媒体，一起喂给模型
                    speaker_title = _get_member_title(sender)
                    q = content
                    if speaker_title:
                        q = f"[说话人「{_get_member_name(sender)}」的身份：{speaker_title}]\n" + q
                    note = _build_media_note(media)
                    if note:
                        q = q + "\n" + note
                    web_note = _web_search_note(content)
                    if web_note:
                        q = q + "\n" + web_note
                    if is_quote and ref_text:
                        q = f"[用户引用了上一条消息：「{ref_text[:200]}」]\n" + q
                    images = _merge_images(media.get("images"), ref_images)
                    if nk:
                        # 像真人一样：用户要故事/笑话，就真的讲一个（不许反问/闲聊/只给开头）
                        q = _narrative_directive(nk) + "\n" + q
                    reply = get_answer(q, sender, speaker_name=_get_member_name(sender), images=images)
                if reply is None:
                    reply = _NO_ANSWER_FALLBACK
                reply_kind = "chat"
            if reply:
                reply = _with_prefix(reply)
                if nk:
                    # 故事/笑话：清理成纯净正文；语音必发（不受频率随机影响），情绪按点名类型/正文智能匹配
                    story = _clean_story(reply)
                    style = await asyncio.to_thread(_narrative_style, nk, story)
                    plan = _narrative_voice_plan()
                    if plan != "voice_only":
                        await _reply(message, content=story)
                    memory.record(sender, "bot", "忺", story)
                    if plan != "none":
                        vok = await _maybe_speak(message, story, mtype="story", style=style, plan=plan)
                        if plan == "voice_only" and vok is not True:
                            await _reply(message, content=story)  # 语音独占失败回退文字，避免静默无响应
                else:
                    # 统一出口：显式对象动态爬图 / 相关话题偶尔带图（被动回复，规避去重）
                    plan = _voice_plan(reply_kind)
                    if plan != "voice_only":
                        if has_image:
                            await _reply(message, content=reply)  # 有图时只回文字，别再把「看图聊天」当找图
                        else:
                            await _reply_with_possible_image(message, content, reply)
                    memory.record(sender, "bot", "忺", reply)
                    if plan != "none":
                        # 私聊也固定音色：不逐句算情绪（同群聊，避免音色一直变）
                        vok = await _maybe_speak(message, reply, mtype=reply_kind, style=None, plan=plan)
                        if plan == "voice_only" and vok is not True:
                            await _reply(message, content=reply)

        async def on_ready(self, *args):
            global BOT_API, BOT_LOOP
            BOT_API = self.api
            BOT_LOOP = asyncio.get_running_loop()
            # 拿到机器人自己的 openid：先取 on_ready 入参里的 robot.id，拿不到再用 api.me() 兜底。
            # 这个 openid 是「群消息里 <@!openid> 形式 @ 我」能被识别的前提，缺失会导致 @ 完全失灵。
            bid = None
            if args:
                robot = getattr(args[0], "robot", None)
                if robot is not None:
                    bid = getattr(robot, "id", None)
            if not bid:
                try:
                    me = await self.api.me()
                    bid = getattr(me, "id", None) or (me.get("id") if isinstance(me, dict) else None)
                except Exception as _e:
                    print(f"[ready] 通过 api.me() 获取机器人 openid 失败（不影响连接）：{_e}")
            # 群消息里 @ 用的是 member_openid，和这里的 user_openid 是两套 ID；
            # 之前学会/认领过的 id 优先，否则每次重启都得重新学，学不会时 @ 直接全失灵。
            _known = _load_bot_openids()
            self._bot_openids = _known
            _ids = {x for s in _known.values() for x in s}
            if _ids:
                self._bot_openid = sorted(_ids)[0]
            else:
                self._bot_openid = bid
            print(f"[ready] 机器人已连接，openid={'已获取' if bid else '未获取(群@识别可能退化)'}，" +
                  "主动推送 API " + ("就绪" if BOT_API else "缺失"))
            # 图库不再启动时自动爬取：图片/表情包改为按需现扒（用户要图时才爬）

    print("[模式] QQ 官方机器人 —— 把机器人拉进群，群里 @它 即可对话；也支持单聊")
    # qq-botpy 1.2.x：群@消息对应 public_messages 意图；Client 不再需要 is_group 参数
    intents = Intents(public_messages=True)
    # 启动时确保语音配置文件存在并加载（缺失则写出默认文件，方便直接编辑）
    if not os.path.exists(VOICE_CONFIG_FILE):
        try:
            save_voice_config({})
        except Exception as e:
            print(f"[语音配置] 初始化失败：{e}")
    load_voice_config()
    threading.Thread(target=_greet_scheduler, daemon=True).start()
    sched = " / ".join(f"{hh if hh else 24}:{mm:02d} {_GREET_LABEL[kind]}"
                       for (hh, mm), kind in sorted(_GREET_SCHEDULE.items()))
    print(f"[定时] 已启用定时道安：{sched}")
    # 登录：QQ 接口偶发超时会导致 botpy 拉机器人信息失败（返回 None 后崩），加重试与友好提示
    for attempt in range(1, 6):
        try:
            client = QQBot(intents=intents)
            client.run(appid=CONFIG["QQ_APPID"], secret=CONFIG["QQ_SECRET"])
            break
        except Exception as e:
            print(f"[登录] 第 {attempt} 次启动失败：{type(e).__name__}: {e}")
            if attempt < 5:
                print("[登录] 5 秒后重试…")
                time.sleep(5)
            else:
                print("[登录] 多次失败，多半是 QQ 接口临时抖动或网络不通，请稍后重试。")


if __name__ == "__main__":
    main()
