# 小忺 QQ 智能机器人（XiaoXian QQ Bot）

> 一个可个性化、带情绪感知与语音能力的 QQ 群聊 / 私聊机器人。

小忺（XiaoXian）是一个基于 [botpy](https://github.com/tencent-connect/botpy) 的 QQ 机器人，具备群聊全量消息处理、`@` 触发、私聊、主动接话、定时道安、语音 TTS / ASR、表情包、视觉理解等能力；并内置基于 BERT 的中文情绪 / 意图识别，以及「自动从 B 站学习说话风格」的活人感训练闭环。

## 功能特性

- 群聊 / 私聊 / `@` 触发 / 主动接话
- 情绪感知回复、定时道安
- 语音合成（本地 MiMo 复刻专属音色 / 可选阿里云 Voice Studio 后端）、语音唱歌
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
├── core/                  # 核心 NLP 模型（情绪 / 意图 / 语义理解）
├── training/              # 模型训练与数据集构建脚本
├── assets/                # 人设、词典、语料与本地配置（含被忽略的隐私文件）
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

## 隐私与部署

以下文件 / 目录**均通过 `.gitignore` 排除**，不会上传到仓库：

- 密钥配置 `config.json`、用量统计 `usage.json`
- 本地运行时数据（`checkin.json` / `games.json` / `weather.json` 等）
- 群友档案 `assets/members.json`、openid `assets/bot_openids.json`、语音配置、自动学习状态
- 训练数据 `data/`、模型权重 `core/models/`、音频 / 图片等大文件
- 备份文件（`*.bak*`）、Python 缓存（`__pycache__/`、`*.pyc`）

部署时请确保 `config.json` 仅在本地 / 服务器保留，切勿提交或公开。

## 许可证

本项目仅供学习与个人使用。
