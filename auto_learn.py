#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动「学说话」：定时从B站热门视频评论区采样 → 提炼活人感 → 写回 persona.json。

和机器人里的「@小忺 /学说话 <BV号>」走的是同一套逻辑（清洗/禁词/提炼/热更新），
只是**不用你手动指定视频**：自动去抓 B站热门/排行榜的评论。

用法（PowerShell，先 cd 到项目目录）：
    python auto_learn.py            常驻：按配置里的 interval_hours 循环执行
    python auto_learn.py --once     只跑一次（手动/调试用）
    python auto_learn.py --dry      只采集+提炼并打印，**不写回人设**
    python auto_learn.py --reset    丢弃之前学到的说话方式，回到手写本色基线（persona.base.json）
    python auto_learn.py --status   看当前配置和学习进度

配置：assets/auto_learn.json（首次运行自动生成，随时可改，**下一次循环自动生效**）
日志：logs/auto_learn.log
进度：assets/auto_learn_state.json（记下哪些视频已经学过，避免重复学同一个）

数据源：默认 source=recommend（**首页个性化推荐**，按你账号算法推的好内容，更贴合「好的视频」，
        需 config.json 填 BILI_COOKIE 才能拿到；没登录取不到就改回 ranking）。每轮只学
        **点赞最高的评论**（评论已按点赞降序，提炼时取前 sample_n 条）。

防糟粕累积：人设里"学到的说话方式"不再无限累加历史——每轮写回 = 手写本色基线(persona.base.json)
            + 本轮新提炼的精华。要彻底清掉以前学的，跑一次 --reset 即可。

人设写回后，机器人靠 persona.json 的修改时间热更新，**不用重启机器人**。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import qq_bot_relay as B  # noqa: E402  复用主程序里的采集/清洗/提炼/写回逻辑

CONFIG_FILE = os.path.join(BASE_DIR, "assets", "auto_learn.json")
STATE_FILE = os.path.join(BASE_DIR, "assets", "auto_learn_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs", "auto_learn.log")

DEFAULT_CFG = {
    "enabled": True,              # 总开关；关掉后循环空转（不抓不写）
    "interval_hours": 12,         # 每隔多少小时学一次
    "run_at_startup": False,      # 启动时立刻跑一次（false=先等第一个间隔）
    "source": "recommend",       # recommend=首页个性化推荐（按你账号算法推的视频，更贴合「好的内容」，
                                  # 需 config.json 填 BILI_COOKIE 才能拿到；无 cookie 取不到就改回 ranking）
                                  # / ranking=排行榜 / popular=热门 / bvlist=用下面的固定列表
    "rids": [0, 1, 3, 4, 5, 36, 188],  # 每轮轮换一个分区：0 全部/1 动画/3 音乐/4 游戏/
                                  # 5 娱乐/36 影视/188 科技 —— 候选池大得多，不容易没得学
    "rid": 0,                     # rids 为空时使用的单一分区
    "videos_per_run": 12,         # 每次学几个视频（没登录时每个视频只给 3 条，所以要多凑几个）
    "pages_per_video": 1,         # 每个视频抓几页评论（填了 BILI_COOKIE 后可调到 2~3）
    "per_page": 20,               # 每页几条
    "min_comments": 2,            # 单个视频清洗后少于这个数就跳过它（累计够 min_total 才提炼）
    "min_total": 12,              # 整轮累计清洗后至少这么多条才值得提炼
    "relearn_after_hours": 24,    # 学过的视频过这么久可以重学（评论区会更新，能学到新东西）
    "sample_n": 20,               # 喂给大模型提炼的条数（取清洗后按点赞降序的前 N 条 = 点赞最高的评论）
    "bv_list": [],                # source=bvlist 时填 BV号/链接，例如 ["BV1xx411c7mD"]
    "skip_learned": True,         # 跳过已经学过的视频
    "sleep_between_videos": 5.0,  # 视频之间的间隔秒数（别猛刷）
    "_说明": "改完保存即可，下一次循环自动生效（不用重启脚本）。想手动跑一次：python auto_learn.py --once",
}


# ---------------- 基础读写 ----------------
def log(msg):
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_cfg():
    """读配置（缺字段用默认补齐）；文件不存在就生成一份默认的。"""
    data = {}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        log(f"[配置] 读取失败，改用默认：{e}")
    if not isinstance(data, dict):
        data = {}
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CFG})
    if not os.path.exists(CONFIG_FILE):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
            log(f"[配置] 已生成默认配置：{CONFIG_FILE}")
        except Exception as e:
            log(f"[配置] 默认配置写出失败：{e}")
    return cfg


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("learned", {})
                return d
    except Exception as e:
        log(f"[状态] 读取失败，重新开始：{e}")
    return {"learned": {}, "runs": 0, "last_run": "", "last_summary": ""}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[状态] 保存失败：{e}")


# ---------------- 选视频 ----------------
def pick_videos(cfg, skip=None, rid=None, page=None):
    """按配置挑出本次要学的视频，返回 [(标识, 标题), ...]。标识可以是 BV号 或 av号/aid。

    skip：近期已学过的标识集合，先过滤掉再挑——否则热门榜前几名一直没变时会反复取到同样的视频。
    rid：本次用哪个分区（轮换用）。page：首页推荐的分页（避免总学同一批）。
    """
    n = max(1, int(cfg.get("videos_per_run") or 1))
    src = (cfg.get("source") or "popular").lower()
    if src == "bvlist":
        out = []
        for s in (cfg.get("bv_list") or []):
            kind, val = B._parse_bili_input(str(s))
            if kind == "bvid":
                out.append((val, f"自选 {val}"))
            elif kind == "aid":
                out.append((f"av{val}", f"自选 av{val}"))
    else:
        if src == "recommend":
            # 首页个性化推荐：按 page 取不同批次，避免总学同一批首页视频
            out = B._bili_recommend_bvids(limit=min(20, n * 3), page=page or 1)
        else:
            # 多要一些候选，过滤掉已学的之后还能凑够 n 个
            use_rid = rid if rid is not None else (cfg.get("rid") or 0)
            out = B._bili_hot_bvids(source=src, rid=use_rid, limit=min(20, n * 3))
    if skip:
        out = [x for x in out if str(x[0]) not in skip]
    return out[:n]


def _recent_learned(st, cfg, force=False):
    """近 relearn_after_hours 小时内学过的视频（超时的可以重学：评论区会更新，能学到新东西）。"""
    if force or not cfg.get("skip_learned"):
        return set()
    hours = float(cfg.get("relearn_after_hours") or 0)
    now = time.time()
    out = set()
    for k, ts in (st.get("learned") or {}).items():
        try:
            t = datetime.strptime(str(ts), "%Y-%m-%d %H:%M")
        except Exception:
            out.add(str(k))   # 时间格式认不出来就保守跳过
            continue
        if hours <= 0 or (now - t.timestamp()) < hours * 3600:
            out.add(str(k))
    return out


def to_aid(ident):
    """标识（BV号 / av号 / 纯数字 aid）-> aid。"""
    kind, val = B._parse_bili_input(str(ident))
    if not kind:
        return None
    return val if kind == "aid" else B._bili_bvid_to_aid(val)


# ---------------- 跑一轮 ----------------
def run_once(cfg, dry=False, force=False):
    """执行一轮学习。返回 (是否写成, 说明文本)。dry=True 时只打印不写人设。"""
    st = load_state()
    if not (getattr(B, "CONFIG", {}).get("BILI_COOKIE") or "").strip():
        log("[提示] config.json 没填 BILI_COOKIE：B站对未登录只给每条视频 3 条热门评论预览，"
            "样本偏少；填了 cookie 才能拿到完整评论")
    # 按来源挑视频
    src = (cfg.get("source") or "ranking").lower()
    want_n = max(1, int(cfg.get("videos_per_run") or 1))
    start = int(st.get("runs") or 0)
    skip = _recent_learned(st, cfg, force)

    if src == "recommend":
        # 首页个性化推荐：按 runs 换 page（1~10），避免总学同一批首页视频
        page = (start % 10) + 1
        videos = pick_videos(cfg, skip=skip, page=page)
        used_tag = f"首页推荐 page={page}"
    else:
        # ranking/popular：按分区轮换取，当前分区凑不够就换下一个分区
        try:
            rids = [int(x) for x in (cfg.get("rids") or [])]
        except Exception:
            rids = []
        if not rids:
            rids = [int(cfg.get("rid") or 0)]
        videos, seen_bv, used_rids = [], set(), []
        for i in range(len(rids)):
            rid = rids[(start + i) % len(rids)]
            used_rids.append(rid)
            for v in pick_videos(cfg, skip=skip, rid=rid):
                if str(v[0]) not in seen_bv:
                    seen_bv.add(str(v[0]))
                    videos.append(v)
            if len(videos) >= want_n:
                break
            time.sleep(1.0)   # 别猛刷
        videos = videos[:want_n]
        used_tag = f"分区 {used_rids}"

    if not videos:
        if skip:
            return False, (f"{used_tag} 暂时没新视频（近期学过的 {len(skip)} 个都跳过了），"
                           f"下一轮会换一批；想立刻重学用 --force")
        return False, "没取到视频（接口可能被限流，或 bv_list 是空的）"
    log(f"[开始] 本次 {used_tag}，候选 {len(videos)} 个视频")

    min_n = max(1, int(cfg.get("min_comments") or 3))
    min_total = max(1, int(cfg.get("min_total") or 15))
    collected, used = [], []

    for ident, title in videos:
        key = str(ident)
        if key in skip:
            log(f"[跳过] {key} 已学过（{title}）")
            continue
        aid = to_aid(ident)
        if not aid:
            log(f"[跳过] {key} 拿不到 aid（{title}）")
            continue
        items = B._bili_comments(
            aid,
            pages=max(1, int(cfg.get("pages_per_video") or 2)),
            per_page=max(1, int(cfg.get("per_page") or 20)),
        )
        cleaned = B._filter_comments(items)
        log(f"[采集] {key} {title}：抓到 {len(items)} → 清洗后 {len(cleaned)}")
        if len(cleaned) < min_n:
            log(f"[略过] {key} 清洗后只有 {len(cleaned)} 条（< {min_n}）")
            continue
        collected.extend(cleaned)
        used.append(key)
        log(f"[累计] 已收集 {len(collected)} 条（来自 {len(used)} 个视频）")
        time.sleep(max(0.0, float(cfg.get("sleep_between_videos") or 0)))

    if len(collected) < min_total:
        return False, (f"这一轮只凑到 {len(collected)} 条（要 {min_total} 条才值得提炼）"
                       f"——多半是没登录被限流，试试在 config.json 填 BILI_COOKIE")

    collected.sort(key=lambda x: -x["like"])
    B._save_corpus(collected)   # 语料留档（只有正文+点赞，脱敏）
    d = B._distill_style(collected, sample_n=max(5, int(cfg.get("sample_n") or 40)))
    if not isinstance(d, dict):
        return False, f"提炼失败（大模型没返回可解析的 JSON），本轮语料 {len(collected)} 条已留档"

    if dry:
        log(f"[试运行] 口头禅={d.get('口头禅')} 梗={d.get('梗')} 示例={len(d.get('示例对话') or [])} 组")
        return True, f"（试运行，未写回）本轮语料 {len(collected)} 条，来自 {len(used)} 个视频"

    summary = B._apply_style(d)   # 写回 persona.json + 热梗库，并让人设缓存失效（热更新）
    for k in used:
        st.setdefault("learned", {})[k] = datetime.now().strftime("%Y-%m-%d %H:%M")
    st["runs"] = int(st.get("runs") or 0) + 1
    st["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["last_summary"] = summary
    save_state(st)
    log(f"[完成] {summary}")
    return True, summary


# ---------------- cookie 自检 ----------------
def check_cookie(cfg):
    """抓一个热门视频看评论条数：>5 条说明登录态生效；<=3 条说明还是未登录预览。"""
    has_ck = bool(str(getattr(B, "CONFIG", {}).get("BILI_COOKIE") or "").strip())
    print(f"config.json 的 BILI_COOKIE：{'已填（长度 %d）' % len(str(B.CONFIG.get('BILI_COOKIE') or '')) if has_ck else '没填'}")
    print("正在抓一个热门视频验证……")
    try:
        hot = B._bili_hot_bvids(source=(cfg.get("source") or "ranking"),
                                rid=(cfg.get("rids") or [cfg.get("rid") or 0])[0], limit=1)
        if not hot:
            print("⚠️ 热门列表都没取到，接口可能被限流，稍后再试")
            return
        aid = to_aid(hot[0][0])
        items = B._bili_comments(aid, pages=1, per_page=20)
    except Exception as e:
        print(f"⚠️ 验证失败：{e}")
        return
    if len(items) > 5:
        print(f"✅ 抓到 {len(items)} 条评论 —— cookie 生效，之后每轮都能拿完整评论了")
    else:
        print(f"⚠️ 只抓到 {len(items)} 条 —— 还是未登录预览。检查："
              f"1) SESSDATA 是不是复制全了；2) 是不是浏览器登错的号；3) cookie 是否过期，重新复制一遍")


# ---------------- 入口 ----------------
def show_status(cfg):
    st = load_state()
    print("【自动学说话 · 当前配置】", CONFIG_FILE)
    for k, v in cfg.items():
        if k != "_说明":
            print(f"  {k} = {v}")
    print(f"  已学视频 {len(st.get('learned') or {})} 个，累计跑 {st.get('runs', 0)} 次，"
          f"上次 {st.get('last_run') or '—'}")
    if st.get("last_summary"):
        print(f"  上次结果：{st['last_summary']}")


def reset_learned():
    """把之前「学说话」学到的东西全扔了，回到手写本色基线（persona.base.json）。

    - persona.json 的 说话风格.口头禅 / 常用颜文字 / 示例对话 回退到基线
    - 清空热梗库（网络热梗精选合集.txt）
    - 清空学习状态（auto_learn_state.json 的 learned，让所有视频可重学）
    不碰用户手写的 名字/身份/性格/情感/兴趣/行为准则 等。
    之后建议跑一次：python auto_learn.py --once
    """
    data = B.load_persona()
    base = B.load_persona_base()
    if not base:
        print("⚠️ 找不到 assets/persona.base.json（手写基线），无法 reset")
        return
    style = dict(data.get("说话风格") or {})
    base_style = base.get("说话风格") or {}
    style["口头禅"] = list(base_style.get("口头禅") or [])
    if "常用颜文字" in base_style:
        style["常用颜文字"] = list(base_style.get("常用颜文字") or [])
    data["说话风格"] = style
    data["示例对话"] = list(base.get("示例对话") or [])
    B.save_persona(data)
    # 清空热梗库
    try:
        if os.path.exists(B._MEME_FILE):
            open(B._MEME_FILE, "w", encoding="utf-8").close()
    except Exception:
        pass
    # 清空学习状态（让所有视频可重学）
    save_state({"learned": {}, "runs": 0, "last_run": "", "last_summary": ""})
    print("✅ 已丢弃之前学到的说话方式，回到手写本色基线（口头禅/示例对话/热梗已清空）。")
    print("   下一步：python auto_learn.py --once   （会从首页重新学，且之后每轮都基于本色+本轮精华，不再越积越脏）")


def main():
    ap = argparse.ArgumentParser(description="自动学说话（B站热门评论 → 人设）")
    ap.add_argument("--once", action="store_true", help="只跑一次就退出")
    ap.add_argument("--dry", action="store_true", help="试运行：采集+提炼，但不写回人设")
    ap.add_argument("--force", action="store_true", help="忽略「已学过」记录，强制重学")
    ap.add_argument("--reset", action="store_true",
                    help="丢弃之前学到的说话方式，回到手写本色基线（persona.base.json）")
    ap.add_argument("--status", action="store_true", help="只看配置与进度")
    ap.add_argument("--check", action="store_true",
                    help="验证 config.json 的 BILI_COOKIE 是否生效（能否拿到完整评论）")
    args = ap.parse_args()

    cfg = load_cfg()
    if args.reset:
        reset_learned()
        return

    if args.status:
        show_status(cfg)
        return

    if args.check:
        check_cookie(cfg)
        return

    if args.once or args.dry:
        ok, msg = run_once(cfg, dry=args.dry, force=args.force)
        print(("✅ " if ok else "⚠️ ") + msg)
        return

    # 常驻：每次循环都重读配置，改完立即生效
    log(f"[启动] 自动学说话已就绪（每 {cfg.get('interval_hours')} 小时一次，"
        f"配置 {CONFIG_FILE} 可随时改）")
    if cfg.get("run_at_startup"):
        try:
            ok, msg = run_once(cfg)
            log(("✅ " if ok else "⚠️ ") + msg)
        except Exception:
            log(f"[异常] 启动那轮失败：\n{__import__('traceback').format_exc()}")

    while True:
        # 分段 sleep：这样改了 interval_hours / enabled 能很快生效
        hours = max(0.1, float(load_cfg().get("interval_hours") or 12))
        wait = int(hours * 3600)
        log(f"[等待] 下次学习在 {hours} 小时后（{datetime.fromtimestamp(time.time() + wait):%m-%d %H:%M}）")
        for _ in range(max(1, wait // 60)):
            time.sleep(60)
            if not load_cfg().get("enabled"):
                continue
        cfg = load_cfg()
        if not cfg.get("enabled"):
            log("[暂停] enabled=false，本轮跳过（改回 true 即恢复）")
            continue
        try:
            ok, msg = run_once(cfg)
            log(("✅ " if ok else "⚠️ ") + msg)
        except Exception:
            log(f"[异常] 本轮失败：\n{__import__('traceback').format_exc()}")


if __name__ == "__main__":
    main()
