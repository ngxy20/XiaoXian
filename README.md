# 小忺 QQ 智能机器人（XiaoXian QQ Bot）

> 一个可个性化、带情绪感知与语音能力的 QQ 群聊 / 私聊机器人。

小忺（XiaoXian）是一个基于 [botpy](https://github.com/tencent-connect/botpy) 的 QQ 机器人，具备群聊全量消息处理、`@` 触发、私聊、主动接话、定时道安、语音 TTS / ASR、表情包、视觉理解等能力；并内置基于 BERT 的中文情绪 / 意图识别，以及「自动从 B 站学习说话风格」的活人感训练闭环。

## 功能特性

- 群聊 / 私聊 / `@` 触发 / 主动接话
- 情绪感知回复、定时道安
- 语音合成（本地 MiMo 复刻专属音色 / 可选阿里云 Voice Studio 后端）、语音唱歌
- 语音拟人化：场景情绪识别 → 多情绪标签 → 参数化语音控制 → 人设驱动的风格输出
- 语音转文字（ASR）
- 表情包发送、视觉理解
- 群友身份档案（手动教学式记忆）
- 可训练的多标签中文情绪识别模型（BERT）
- 自动从 B 站评论学习「活人感」说话风格（`auto_learn`）

## 目录结构

```
.
├── qq_bot_relay.py        # 主程序（机器人核心）
├── auto_learn.py          # 自动从 B 站学习说话风格
├── emotion_labeler.py     # 情绪标注桌面工具（Tkinter）
├── config.example.json    # 配置样例（复制为 config.json 后填写）
├── core/                  # 核心 NLP 模型（情绪 / 意图 / 语义理解 / 语音拟人化）
│   └── voice_persona.py   #   场景情绪识别 + 多标签参数模型 + 人设偏置
├── training/              # 模型训练与数据集构建脚本
├── evaluation/            # 模型评估、对话策略 A/B、语音拟人化 A/B 报告
├── assets/                # 人设、词典、语料与本地配置（含被忽略的隐私文件）
│   └── voice_emotion_params.json  # 语音拟人化参数模型（可热更新，无隐私）
└── prompts/               # 提示词模板
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- 依赖：`torch`、`transformers`、`botpy` 等（按代码中的 `import` 安装对应版本）

### 2. 配置

```bash
cp config.example.json config.json
# 编辑 config.json，填入你的 QQ 机器人 AppID / AppSecret 及各 API Key
```

> ⚠️ `config.json` 含敏感密钥，**已被 `.gitignore` 忽略，不会进入版本库**。请勿手动将其加入提交。

### 3. 运行

```bash
python qq_bot_relay.py
```

### 4. 可选：训练情绪模型

详见 `training/` 目录，流程为：标注（`training/label_emotion.py`）
→ 安装 `torch`/`transformers` → 训练（`training/train_emotion.py`）
→ 重启机器人加载权重。

## 语音拟人化

默认开启，作用在**每一次逐句 TTS**（群聊 / 私聊 / 主动接话 / 定时道安）。四个环节：

1. **场景情绪识别**：结合消息类型（`chat` / `proactive` / `story` / `greeting` / 私聊 / `@`）、意图、最近消息上下文，判定 `comfort` / `celebrate` / `story` / `private` 等场景并给出情绪先验。
2. **参数模型判别情绪类型**：`关键词 / NLP 模型 / 场景先验 / 上下文延续 / 人设共情` 五路信号按权重加权打分，阈值 + top-k 产出**多情绪标签**（最多 3 个，带置信度）。NLP 权重未训练时自动回退规则，不影响使用。
3. **多情绪标签应用到语音合成**：按得分加权混合 19 类情绪的语速 / 音高 / 音量 / 风格，生成一句中文 `instruction`（MiMo 走 `messages[0].content`、Voice Studio 走 `input.instruction`）；语速落到现有 ffmpeg `atempo` 后处理，音高音量写成指令文字（两个后端都不提供数值情绪字段）。
4. **人设驱动改进输出**：实时读取 `assets/persona.json` 的 `说话风格.语气` 作风格底座、`情感.共情规则` 作情绪 / 语速偏置、`性格.核心特质` 作强度修正、`说话风格.禁止` 作输出约束（如「不要说教腔」），并固定要求「保持原有音色不变」，避免逐句换音色。

配置文件 `assets/voice_emotion_params.json`（**mtime 热更新，改完即生效，无需重启**）：

| 开关 | 说明 |
|---|---|
| `enabled` | 设为 `false` 整体回退到原固定音色逻辑 |
| `preserve_explicit_style` | 默认 `true`：故事点名情绪、`/情绪` 指令等显式指定时保留原样，仅补做判别 |
| `decision.threshold` / `max_labels` | 判别阈值与最多标签数 |
| `decision.weights` | 五路信号权重（主要调参入口） |
| `emotions.*.spd` | 各情绪相对 `voice_speed` 的语速倍率 |
| `scenes.*` | 各场景的情绪先验、语速偏置、风格提示 |

评估与复跑：

```bash
python evaluation/eval_voice_params.py   # 生成 evaluation/voice_params_report.md
```

该 A/B 报告对比「固定中性语音」与「拟人化语音」在情绪命中率、多标签覆盖率、故事场景召回、人设注入率、语速区分度上的差异，可直接验证调参效果。

## 隐私与部署

以下文件 / 目录**均通过 `.gitignore` 排除**，不会上传到仓库：

- 密钥配置 `config.json`、用量统计 `usage.json`
- 本地运行时数据（`checkin.json` / `games.json` / `weather.json` 等）
- 群友档案 `assets/members.json`、openid `assets/bot_openids.json`、语音配置、自动学习状态
- 训练数据 `data/`、模型权重 `core/models/`、音频 / 图片等大文件
- 备份文件（`*.bak*`）、Python 缓存（`__pycache__/`、`*.pyc`）

部署时请确保 `config.json` 仅在本地 / 服务器保留，切勿提交或公开。

> `assets/voice_emotion_params.json`、`assets/persona.json` 与 `evaluation/` 下的报告不含密钥，会随仓库公开；真正含密钥的 `config.json`、以及含本地音色路径的 `assets/voice_config.json` 已被忽略，不会上传。

## 许可证

本项目仅供学习与个人使用。
