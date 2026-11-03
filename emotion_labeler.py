# -*- coding: utf-8 -*-
"""情绪人工标注桌面工具（Tkinter，无需额外安装）。

用法（在你自己电脑上双击或运行）：
    python emotion_labeler.py

数据：读取 data/emotions_tolabel.jsonl（由 training/label_emotion.py export 生成）。
标注：点情绪按钮多选；翻页/切换自动存盘。标完跑：python training/label_emotion.py apply

功能：
  - 标签集来自 assets/emotion_labels.json（与训练/推理共用同一份），支持 >8 类、多标签。
  - 「➕ 添加标签」：随时新增自定义标签（自动分配字母快捷键），写回 emotion_labels.json，
    之后训练时该标签即成为模型的一个类别（真正可被学习/预测）。
  - 每个按钮、以及底部「当前标签」都显示对应快捷键。
  - 快捷键：1-9 / 字母 切换情绪；← / → 翻页。
  - 「✂ 分割文本」按钮（或在消息正文上点鼠标右键）：在你点的位置把长消息切开，
    切点相当于自动补一个逗号，前半段 / 后半段成为两条独立条目，各自打情绪标签（前后情绪可不同）。
  - 「↩ 撤回」按钮（或 Ctrl+Z）：撤销上一步操作（拆分 / 打标签 / 取消标签均可撤回，最多 30 步）。
    箭头 ← / → 翻页切换条目。
"""
import json
import os
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "data", "emotions_tolabel.jsonl")
PROG = os.path.join(ROOT, "data", ".emotion_label_progress.json")
_LABELS_FILE = os.path.join(ROOT, "assets", "emotion_labels.json")
_DEFAULT_LABELS = ["开心", "温柔", "悲伤", "愤怒", "俏皮", "撒娇", "严肃", "神秘"]
# 快捷键池：前 9 个标签用数字 1-9，其余用字母（字母与翻页键不冲突，翻页仅用方向键）
_EXTRA_KEYS = "abcdefghijklmnopqrstuvwxyz"
ON_BG, ON_FG = "#ffd966", "#222222"
OFF_BG, OFF_FG = "#f0f0f0", "#333333"


def load_items():
    if not os.path.exists(SRC):
        messagebox.showerror("找不到数据",
            "未找到 %s\n请先运行：python training/label_emotion.py export" % SRC)
        sys.exit(1)
    items = []
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if "text" not in d:
                continue
            if not isinstance(d.get("labels"), list):
                d["labels"] = []
            items.append(d)
    return items


def _load_labels():
    if os.path.exists(_LABELS_FILE):
        try:
            d = json.load(open(_LABELS_FILE, encoding="utf-8"))
            if isinstance(d, list) and all(isinstance(x, str) for x in d) and d:
                return list(d)
        except Exception:
            pass
    # 首次：用默认 8 类初始化文件
    json.dump(list(_DEFAULT_LABELS), open(_LABELS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return list(_DEFAULT_LABELS)


def _save_labels(labels):
    json.dump(list(labels), open(_LABELS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


class App:
    def __init__(self, root, items):
        self.root = root
        self.items = items
        self.emotions = _load_labels()
        self.key_of = {}        # 标签 -> 快捷键字符
        self.label_of_key = {}  # 快捷键字符 -> 标签
        self._assign_keys()
        self.btns = []
        self.undo_stack = []      # 撤回栈：每次改动前压入 (items 快照, idx)

        root.title("情绪标注工具")
        root.geometry("860x560")
        root.minsize(620, 420)

        # 左侧导航：标签列表（可滚动）；标记变色 + 显示快捷键；新建标签自动出现
        sidebar = tk.Frame(root)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        tk.Button(sidebar, text="➕ 添加标签", command=self.add_label).pack(fill=tk.X, pady=(0, 6))
        self.canvas = tk.Canvas(sidebar, width=150)
        vsb = tk.Scrollbar(sidebar, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.Y)
        self.side_inner = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.side_inner, anchor="nw")
        self.side_inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        # 滚轮滚动：光标停在标签栏任意位置（canvas / 内部容器 / 各按钮）都能带动标签滚动
        self._on_wheel = lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.side_inner.bind("<MouseWheel>", self._on_wheel)

        # 右侧主区
        right = tk.Frame(root)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top = tk.Frame(right)
        top.pack(fill=tk.X, padx=10, pady=8)
        self.prog = tk.Label(top, text="", font=("Microsoft YaHei", 10))
        self.prog.pack(side=tk.LEFT)
        self.hint = tk.Label(top, text="", font=("Microsoft YaHei", 9), fg="#1a7f37")
        self.hint.pack(side=tk.LEFT, padx=8)
        tk.Button(top, text="↩ 撤回", command=self.undo).pack(side=tk.RIGHT, padx=3)
        tk.Button(top, text="✂ 分割文本", command=self.split_current).pack(side=tk.RIGHT, padx=3)
        tk.Button(top, text="← 上一条", command=self.prev).pack(side=tk.RIGHT, padx=3)
        tk.Button(top, text="下一条 →", command=self.next).pack(side=tk.RIGHT, padx=3)
        tk.Button(top, text="跳到未标注", command=self.jump_unlabeled).pack(side=tk.RIGHT, padx=3)

        mid = tk.Frame(right)
        mid.pack(fill=tk.BOTH, expand=True, padx=10)
        self.txt = tk.Text(mid, wrap=tk.WORD, font=("Microsoft YaHei", 14),
                           relief=tk.SOLID, borderwidth=1, padx=8, pady=8)
        self.txt.pack(fill=tk.BOTH, expand=True)
        self.txt.bind("<Key>", lambda e: "break")  # 只读展示
        self.txt.bind("<Button-3>", self._on_right_click)  # 右键点哪切哪：切点补逗号、前后分别打标签

        self.status = tk.Label(right, text="", font=("Microsoft YaHei", 9), fg="#666")
        self.status.pack(fill=tk.X, padx=10, pady=(0, 6))

        self._build_buttons()

        root.bind("<Left>", lambda e: self.prev())
        root.bind("<Right>", lambda e: self.next())
        root.bind("<Control-z>", lambda e: self.undo())   # Ctrl+Z 撤回
        for ch in "123456789" + _EXTRA_KEYS:
            root.bind(ch, self._on_key)

        self.idx = self._load_idx()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 标签 / 快捷键管理 ----------
    def _assign_keys(self):
        self.key_of.clear()
        self.label_of_key.clear()
        for i, em in enumerate(self.emotions):
            k = str(i + 1) if i < 9 else (_EXTRA_KEYS[i - 9] if i - 9 < len(_EXTRA_KEYS) else "")
            self.key_of[em] = k
            if k:
                self.label_of_key[k] = em

    def _build_buttons(self):
        for b in self.btns:
            b.destroy()
        self.btns = []
        for em in self.emotions:
            k = self.key_of.get(em, "")
            label = ("[%s] %s" % (k, em)) if k else em
            b = tk.Button(self.side_inner, text=label, width=18, height=1, anchor="w",
                          bg=OFF_BG, fg=OFF_FG,
                          command=lambda em=em: self.toggle_label(em))
            b.bind("<MouseWheel>", self._on_wheel)
            b.pack(fill=tk.X, padx=2, pady=2)
            self.btns.append(b)
        self.side_inner.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_key(self, event):
        ch = (event.char or "").lower()
        if ch in self.label_of_key:
            self.toggle_label(self.label_of_key[ch])

    def add_label(self):
        name = (simpledialog.askstring("添加标签", "输入新标签名称：") or "").strip()
        if not name:
            return
        if name in self.emotions:
            messagebox.showinfo("提示", "标签已存在：" + name)
            return
        self.emotions.append(name)
        self._assign_keys()
        _save_labels(self.emotions)
        self._build_buttons()
        self.refresh_buttons()

    # ---------- 文本分割：在点击处切开，切点相当于自动加一个逗号，前后各打标签 ----------
    def _click_offset(self, event):
        """把鼠标右键点击位置换算成消息原文的字符偏移。"""
        idx = self.txt.index("@%d,%d" % (event.x, event.y))
        return self.txt.count("1.0", idx, "chars")[0]

    def split_current(self, offset=None):
        self._push_undo()
        item = self.items[self.idx]
        text = item.get("text", "")
        if offset is None:
            # 按钮触发：优先用光标位置，否则取中点
            try:
                cur = self.txt.count("1.0", "insert", "chars")[0]
                offset = cur if 0 < cur < len(text) else len(text) // 2
            except Exception:
                offset = len(text) // 2
        offset = max(1, min(offset, len(text) - 1))
        left = text[:offset].rstrip()
        right = text[offset:].lstrip()
        if not left or not right:
            self._flash("切点太靠边，无法分割")
            return
        if left[-1] not in "，,。！？!?；;":   # 切点处自动补一个逗号
            left = left + "，"
        self.items[self.idx:self.idx + 1] = [
            {"text": left, "labels": []},
            {"text": right, "labels": []},
        ]
        self._flush()
        self.refresh()
        self._flash("✂ 已在点击处拆为两段，前后分别打标签即可")

    def _on_right_click(self, event):
        self.split_current(self._click_offset(event))
        return "break"   # 屏蔽原生右键菜单

    def _flash(self, msg):
        self.hint.config(text=msg)
        self.root.after(2600, lambda: self.hint.config(text=""))

    # ---------- 撤回 ----------
    def _push_undo(self):
        snap = ([{k: (list(v) if k == "labels" else v) for k, v in it.items()}
                 for it in self.items], self.idx)
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 30:
            self.undo_stack.pop(0)

    def undo(self):
        if not self.undo_stack:
            self._flash("没有可撤回的操作")
            return
        items, idx = self.undo_stack.pop()
        self.items = [{k: (list(v) if k == "labels" else v) for k, v in it.items()}
                      for it in items]
        self.idx = min(idx, len(self.items) - 1)
        self._flush()
        self.refresh()
        self._flash("↩ 已撤回上一步")

    # ---------- 标注逻辑 ----------
    def toggle_label(self, em):
        self._push_undo()
        if em in self.cur_labels:
            self.cur_labels.discard(em)
        else:
            self.cur_labels.add(em)
        self._save_item()
        self.refresh_buttons()
        self.refresh_status()

    def refresh_buttons(self):
        for i, em in enumerate(self.emotions):
            if em in self.cur_labels:
                self.btns[i].config(bg=ON_BG, fg=ON_FG, relief=tk.SUNKEN)
            else:
                self.btns[i].config(bg=OFF_BG, fg=OFF_FG, relief=tk.RAISED)

    def refresh(self):
        item = self.items[self.idx]
        self.cur_labels = set(item.get("labels") or [])
        self.txt.delete("1.0", tk.END)
        self.txt.insert("1.0", item["text"])
        self.refresh_buttons()
        self.refresh_status()
        self.refresh_progress()

    def refresh_status(self):
        cur = "  ".join("%s.%s" % (self.key_of.get(em, ""), em)
                        for em in self.emotions if em in self.cur_labels)
        self.status.config(text="当前标签：" + (cur or "（未标注）"))

    def refresh_progress(self):
        total = len(self.items)
        done = sum(1 for it in self.items if it.get("labels"))
        self.prog.config(text="第 %d/%d 条　已标注 %d/%d" % (self.idx + 1, total, done, total))

    def _save_item(self):
        self.items[self.idx]["labels"] = sorted(
            self.cur_labels, key=lambda x: self.emotions.index(x))
        self._flush()

    def _flush(self):
        with open(SRC, "w", encoding="utf-8") as f:
            for it in self.items:
                f.write(json.dumps({"text": it["text"], "labels": it.get("labels") or []},
                                   ensure_ascii=False) + "\n")
        json.dump({"idx": self.idx}, open(PROG, "w", encoding="utf-8"))

    def _load_idx(self):
        if os.path.exists(PROG):
            try:
                d = json.load(open(PROG, encoding="utf-8"))
                i = int(d.get("idx", 0))
                if 0 <= i < len(self.items):
                    return i
            except Exception:
                pass
        return 0

    def next(self):
        if self.idx < len(self.items) - 1:
            self.idx += 1
            self.refresh()

    def prev(self):
        if self.idx > 0:
            self.idx -= 1
            self.refresh()

    def jump_unlabeled(self):
        n = len(self.items)
        for off in range(1, n + 1):
            j = (self.idx + off) % n
            if not self.items[j].get("labels"):
                self.idx = j
                self.refresh()
                return
        messagebox.showinfo("提示", "全部标注完啦～")

    def on_close(self):
        self._flush()
        self.root.destroy()


def main():
    items = load_items()
    if not items:
        messagebox.showerror("空数据", "没有可标注的条目。")
        return
    root = tk.Tk()
    App(root, items)
    root.mainloop()


if __name__ == "__main__":
    main()
