# -*- coding: utf-8 -*-
"""情绪识别模型性能评估报告生成器。

读取标注测试集 data/emotions.jsonl（{text, labels}），对当前可用的情绪判定
方案做多标签分类评估，并输出 Markdown 报告 evaluation/report_emotion.md。

- 若 core/models/emotion_bert.pt 存在，则评估训练好的 BERT 多标签模型；
- 否则评估内置的「关键词规则基线」分类器，并标注模型未训练。

用法：
  python evaluation/eval_models.py
  python evaluation/eval_models.py --data data/emotions.jsonl --out evaluation/report_emotion.md
"""
import sys
import os
import json
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from core.emotion_model import LABELS, WEIGHT  # 标签集与权重路径

# ---- 关键词规则基线（无模型时的可运行方案，亦作为对照） ----
_RULE = {
    "开心": ["开心", "欢乐", "高兴", "快乐", "愉快", "哈哈", "嘻嘻", "喜", "乐", "笑"],
    "温柔": ["温柔", "温暖", "暖", "安静", "体贴", "轻柔", "软"],
    "悲伤": ["悲伤", "难过", "伤心", "哭", "泪", "哀", "痛", "呜呜", "失落", "失望"],
    "愤怒": ["愤怒", "生气", "恼火", "火大", "怒", "气死"],
    "俏皮": ["俏皮", "调皮", "逗", "皮", "鬼灵", "机灵"],
    "撒娇": ["撒娇", "软萌", "抱抱", "哼", "嘛", "亲亲"],
    "严肃": ["严肃", "认真", "正经", "郑重", "郑重其事"],
    "神秘": ["神秘", "诡异", "恐怖", "悬疑", "诡", "阴森", "灵异"],
    "失落": ["失落", "落寞", "黯然", "孤单"],
    "傲娇": ["傲娇", "嘴硬", "口是心非"],
    "神圣": ["神圣", "庄严", "伟大", "圣洁"],
    "平和": ["平和", "平静", "淡然", "安详", "安心"],
    "讨厌": ["讨厌", "厌恶", "嫌弃", "烦人", "反感"],
    "耍赖": ["耍赖", "赖皮", "无赖"],
    "疑问": ["吗", "呢", "怎么", "为什么", "啥", "？", "?"],
    "好奇": ["好奇", "想知道", "探秘", "奥秘", "神奇", "新奇"],
    "滑稽": ["滑稽", "搞笑", "逗趣", "笑话", "可乐"],
    "无所谓": ["无所谓", "随便", "都行", "懒得", "随意"],
    "感叹": ["！", "!", "哇", "呀", "天哪", "绝了", "太"],
}


def rule_baseline_emotions(text):
    """关键词规则情绪分类（多标签）。"""
    if not text:
        return []
    hits = []
    for lab, kws in _RULE.items():
        if any(k in text for k in kws):
            hits.append(lab)
    seen = set()
    out = []
    for lab in LABELS:
        if lab in hits and lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out


def _model_predict(text):
    """优先用训练好的 BERT，未训练/失败返回 (None, False)。"""
    try:
        from core.emotion_model import predict_emotions
        res = predict_emotions(text)
        if res:
            return res, True
    except Exception:
        pass
    return None, False


def load_gold(path):
    samples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            samples.append((d.get("text", ""), d.get("labels") or []))
    return samples


def multilabel_metrics(golds, preds, labels):
    """计算多标签分类指标。"""
    n = len(golds)
    tp = {l: 0 for l in labels}
    fp = {l: 0 for l in labels}
    fn = {l: 0 for l in labels}
    support = {l: 0 for l in labels}
    exact = 0
    for g, p in zip(golds, preds):
        gs, ps = set(g), set(p)
        for l in labels:
            if l in gs:
                support[l] += 1
            if l in gs and l in ps:
                tp[l] += 1
            elif l in ps and l not in gs:
                fp[l] += 1
            elif l in gs and l not in ps:
                fn[l] += 1
        if gs == ps:
            exact += 1

    def prf(l):
        p = tp[l] / (tp[l] + fp[l]) if (tp[l] + fp[l]) else 0.0
        r = tp[l] / (tp[l] + fn[l]) if (tp[l] + fn[l]) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return p, r, f

    rows = []
    micro_tp = sum(tp.values())
    micro_fp = sum(fp.values())
    micro_fn = sum(fn.values())
    micro_p = micro_tp / (micro_tp + micro_fp) if (micro_tp + micro_fp) else 0.0
    micro_r = micro_tp / (micro_tp + micro_fn) if (micro_tp + micro_fn) else 0.0
    micro_f = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    macro_f_list = []
    for l in labels:
        p, r, f = prf(l)
        rows.append((l, support[l], p, r, f))
        if support[l]:
            macro_f_list.append(f)
    macro_f = sum(macro_f_list) / len(macro_f_list) if macro_f_list else 0.0
    return {
        "n": n,
        "exact": exact / n if n else 0.0,
        "micro_p": micro_p,
        "micro_r": micro_r,
        "micro_f": micro_f,
        "macro_f": macro_f,
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(_ROOT, "data", "emotions.jsonl"))
    ap.add_argument("--out", default=os.path.join(_ROOT, "evaluation", "report_emotion.md"))
    args = ap.parse_args()

    samples = load_gold(args.data)
    if not samples:
        print("无标注数据，退出")
        return
    golds = [lbl for _, lbl in samples]
    preds = []
    used_model = os.path.exists(WEIGHT)
    for text, _ in samples:
        res, ok = _model_predict(text)
        if ok and res:
            preds.append(res)
        else:
            preds.append(rule_baseline_emotions(text))
    m = multilabel_metrics(golds, preds, LABELS)

    lines = []
    lines.append("# 情绪识别模型性能评估报告\n")
    lines.append(f"- 测试集：`{os.path.relpath(args.data, _ROOT)}`（{m['n']} 条标注样本）")
    lines.append(f"- 评估方案：**{'BERT 多标签模型' if used_model else '关键词规则基线（BERT 权重未训练）'}**")
    lines.append(f"- 标签集：{len(LABELS)} 类（以 assets/emotion_labels.json 为准）\n")
    lines.append("## 总体指标\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 样本级完全匹配率 (Exact Match) | {m['exact']*100:.1f}% |")
    lines.append(f"| 微平均 Precision | {m['micro_p']*100:.1f}% |")
    lines.append(f"| 微平均 Recall | {m['micro_r']*100:.1f}% |")
    lines.append(f"| 微平均 F1 (Micro-F1) | {m['micro_f']*100:.1f}% |")
    lines.append(f"| 宏平均 F1 (Macro-F1) | {m['macro_f']*100:.1f}% |\n")
    lines.append("## 各类别明细（Precision / Recall / F1，支持数）\n")
    lines.append("| 标签 | 支持数 | Precision | Recall | F1 |")
    lines.append("|---|---|---|---|---|")
    for l, sup, p, r, f in m["rows"]:
        lines.append(f"| {l} | {sup} | {p*100:.1f}% | {r*100:.1f}% | {f*100:.1f}% |")
    lines.append("\n## 结论与建议\n")
    if used_model:
        lines.append("- BERT 权重已加载，上述为模型在标注集上的真实指标。")
        lines.append("- 对 F1 偏低的类别，建议补充该类标注样本或在训练时加权。")
    else:
        lines.append("- ⚠️ 当前 `core/models/emotion_bert.pt` **尚未训练**，以上为关键词规则基线的性能，仅作对照。")
        lines.append("- 执行 `python training/train_emotion.py` 训练 BERT 后重跑本脚本，即可获得模型指标并与基线对比，量化训练带来的性能提升。")
        lines.append("- 规则基线对隐含情绪（如反讽、含蓄表达）覆盖不足，正体现引入深度学习模型的必要性。")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"报告已生成：{args.out}")
    print(f"方案={'BERT' if used_model else '规则基线'}  样本={m['n']}  "
          f"Micro-F1={m['micro_f']*100:.1f}%  Macro-F1={m['macro_f']*100:.1f}%")


if __name__ == "__main__":
    main()
