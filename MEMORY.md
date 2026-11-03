# 小忺QQ机器人 · 记忆文档

> 用途：压缩长上下文，给后续会话/重启快速回顾。行号会漂移，以「函数名+作用」为准。
> 主程序：`e:/Desktop/jiqiren/qq_bot_relay.py`　配置：`config.json`　语音：`assets/voice_config.json`

---

## 0. 项目概览
- 机器人昵称 **小忺**（群内记作"忺"），主人/群友如 **远旅**、**薄荷奶绿**、**小鱼**（见截图疑似也是机器人）。
- 能力：群聊全量消息、@触发、私聊(c2c)、**主动接话**(proactive)、讲恐怖故事、定时道安、语音TTS(官方 MiMo)、表情包、视觉/ASR。
- QQ API 走本地代理 `SERVER_URL=http://127.0.0.1:8787`（早期从 sse 迁移而来，botpy 接管收发）。
- 词元预算（云端大模型累计估算）：约 **38503 / 50000（77%）**，快见底，情绪选择已改为免费关键词版以省词元。

## 1. 最近一轮已完成的修复（含根因）

### A. 致命 NameError —— 群消息入口整体崩溃
- **根因**：`on_group_message_create` 里 `[群消息]` 日志用了 `_get_member_name(sender)`，但 `sender` 在该行**之后**才定义 → 每条群消息抛 `NameError`，整个群入口崩掉。这是"讲恐怖故事没反应 + 不会认人"的同期元凶。
- **修法**：把 `author = ...` / `sender = ...` 提到日志前定义（现已验证 `sender` 赋值先于使用）。

### B. 「语音依旧文字+语音」的根因
- **根因**：`load_voice_config()` 只从文件拷贝 `enabled/frequency/voice/effect/style/max_chars`，**漏了 `voice_only_chance`** → 永远取默认 0 → 一旦发语音就是 `both`。
- **修法**：`_VOICE_CFG_FALLBACK` 增加 `"voice_only_chance": 0.0`，并在 `load_voice_config` / `save_voice_config` 的拷贝列表里补 `"voice_only_chance"`。现 `voice_only_chance=1.0` 生效，**发语音时不再带文字**。

### C. 忽略其他机器人消息（治"接小鱼的话 / 没参与却提到我"）
- 解析器（群+c2c）已 `setattr(_message.author, "bot", bool(_auth.get("bot")))` 标记来源是否为机器人。
- `on_group_message_create` / `on_group_at_message_create` 入口：`if getattr(author, "bot", False): return` —— 其他机器人消息不进记忆、不回复、不主动接话。
- **注意**：若截图里的「小鱼」其实是**真人**，bot-skip 拦不住，需另加"检测消息是否在跟别人说话(主人/你俩)而不插嘴"的逻辑（待用户确认小鱼身份）。

### D. 主动接话不配图（治"凭空发出一张图片"）
- 主动接话分支原本会调 `_reply_with_possible_image`（看到"图"字从图库贴图）+ 跨消息引用合图。
- **修法**：主动接话改为 `reply = get_answer(content, group_id)`，**只回文字、不自动配图**；并去掉 `_resolve_refs`/`_img_p` 拼图逻辑。

### E. 主动消息发库里表情 + 语音情绪自动选
- `config.json` 的 `EMOTICON_LIB` 已填入 `images/emoticons/` 的 **60 张** `emo_*.png`；新增 `PROACTIVE_EMOTICON_CHANCE: 0.25`。
- 新增 `async def _send_library_emoticon(message)`：从库里随机抽一张，**纯表情(msg_type=7)、不带文字**发出。
- 主动接话有 25% 概率直接甩一张库表情（替代文字回复）。
- 新增 `def _choose_voice_emotion(text)`：按文本**关键词**映射情绪（免费、零词元、零延迟），用于 **聊天/主动/私聊/定时道安**；故事(_choose_story_emotion)仍走大模型精修（频率低）。
- 所有语音调用点都已把 `style=await asyncio.to_thread(_choose_voice_emotion, reply)` 传入 `_maybe_speak`。

### F. @ 失灵根因（新一轮修复）
- **现象**：群友 `@小忺 ...` / `小忺讲个恐怖故事` / `/天气` 全都没反应；日志里 `[群消息] ... mention=False`。
- **根因**：`on_group_message_create` 用 `on_ready` 给的 `user_openid`（十进制，如 `16823651822791012887`）去匹配消息里 `<@F833E28C...>`（十六进制群 `member_openid`），必然不匹配；旧 `mentions` 兜底只查 `is_you`/`bot`，群消息负载里这个标记没命中 → `_mentioned=False`，@ 消息被当普通消息，又因 `@` 开头被 `_directed_to_other` 拦掉，最终静默丢弃。
  - 另：`_STORY_RE` 是 `讲\s*个?\s*故事`，要求「故事」紧贴「个」，但「讲个**恐怖**故事」中间夹了「恐怖」→ 正则匹配不到，连 `小忺讲个恐怖故事`(name=True) 也没进故事分支。
- **修法**：
  - 新增模块函数 `_leading_at_openid(raw)`，从消息开头提取 `<@openid>`。
  - `on_group_message_create` 的 @ 检测改为多管齐下：①全局 `_bot_openid` 字符串匹配；②**按群** `_bot_openids[group_id]` 集合匹配（多群各自记录，避免跨群串味）；③ `message.mentions` 里 `is_you/bot/is_robot/type==1/role==bot` 任一标记→学到并命中。
  - **⚠️ 原第④条「盲猜兜底」已移除（会抢答别人）**：旧版写的是「本群还没确认过机器人 openid 且消息以 `<@openid>` 开头 → 当作 @ 本机器人并把该 openid 记下」。**后果**：若群里第一条 @ 是 `@小鱼`，就会把小鱼的 openid 误学成小忺自己，之后小忺一直抢答 @小鱼 的消息（用户实测「@小鱼 /help 小忺却发我菜单」）。
  - **修法**：①**不做盲猜** —— 开头 @ 的 openid 不在已确认集合里就一律不认领；@本机器人由 `on_group_at_message_create`（AT 事件，**只在 @ 本机器人时触发**，属权威信号）处理；②新增 `_learn_bot_openid(group_id, oid)`，权威学习用**替换**（`_bot_openids[group_id] = {oid}`）而非累加，AT 事件/mentions-bot 标记都走它 → 能把已误学的 id 清掉（**自愈**）；③`_directed_to_other` 除比对 `_bot_openid` 外，也比对 `_bot_openids` 各群集合，避免 id 被覆盖后把 @小忺 误判成"在跟别人说话"。
  - 验证：全新群 `@小鱼`→不响应；AT 事件后 `@小忺`→响应、`@小鱼`→不响应；**已中毒**集合含小鱼 id 时，AT 事件一次即重置为只含小忺真身并恢复正确。
- **⚠️ 后续（真实 payload 复现）：移除兜底后 @小忺 也失灵了，真因有两个**：
  1. **AT 事件根本不触发**（日志只有 `[群消息]` 没有 `[AT事件]`）→ 不能依赖 AT 事件做唯一入口。
  2. **`message.mentions` 拿不到可用值**：注入里 `setattr(_message,"mentions",_d.get("mentions"))` 若因 mentions 是只读 property 而静默失败（`except: pass`），`message.mentions` 会是 botpy 自己解析的 **UserPayload 对象**（不是 dict），旧代码 `isinstance(m, dict)` 直接 `continue` → 整个漏判。
- **最终修法**：
  - 新增 `_mention_fields(m)`：**dict 与对象两种形态通吃**（对象用 getattr 取 id/member_openid/bot/is_you/username…）。
  - 注入里再挂一份 `setattr(_message, "_raw_payload", _d)`；判定取 mentions 的顺序：`message.mentions` → `_raw_payload["mentions"]` → []。
  - **判定"是我"只认两种证据**：`is_you=True`（payload 明说"就是你"）或 `username` 命中 `_BOT_NAMES`。
  - **❗绝不能用 `bot/is_robot/type/role` 当证据**：那只表示"被@的是个机器人"，群里另一个机器人（小鱼）同样是 `bot=True` —— 用它判断会把 `@小鱼` 误认成 `@小忺`（测试第5例实测踩到，已修）。
  - 命中后 `_learn_bot_openid()` 记下真身（替换语义），之后即使 mentions 缺失也能靠 `_at in _botset` 命中。
- **验证（用真实 payload）**：dict 形态→True、对象形态→True（本次真因）、拿不到时靠 `_raw_payload`→True、只有 username 匹配→True；`@小鱼`(bot=True+username=小鱼)→**False**、仅 bot=True→**False**；学成后再 @ 即使 mentions 为空→True。
- ⚠️ **更正（曾给错过判断依据）**：日志里 `bot_openid=16823651822791012887` 是 `on_ready` 给的 user_openid（十进制），和群里的 member_openid `F833E28C…`（128bit 十六进制）**不是同一个数值**（实测 `format(16823651822791012887,'X')='E9799E32824FB217'`，与 F833 无关）。所以 `bot_openid` 保持十进制是**正常的**，不能拿它判断有没有重启。判断新代码是否在跑，看日志有没有 `[@调试]` 这一行。
- **❗本轮真因（2026-10-30 日志实锤）**：`[@调试] 开头@了 F833… 未认领；mentions来源=message.mentions 条数=1` —— mentions 取到了（条数1）却仍不认领，说明 **`is_you` 和 `username` 在这个代理环境下都取不到**（botpy 把 mentions 解析成对象，或代理不下发这些字段）。而 AT 事件又不触发 ⇒ **没有任何路径能学到自己的 id ⇒ @ 永远失灵**。这是 @ 反复修不好的根本原因。
- **本轮修法（三管齐下）**：
  1. **持久化**：新增 `assets/bot_openids.json` + 模块级 `_load_bot_openids()` / `_save_bot_openids()`。`_learn_bot_openid()` 学完立即落盘；`on_ready` 与 `on_group_message_create` 启动时从文件加载（群内 id 优先于 on_ready 的 user_openid）。**学会一次，重启不丢**。
  2. **`/认领` 命令（人工权威确认）**：用户 `@小忺 /认领` → 把本条消息开头 `<@openid>` 强制记成小忺自己（替换语义 + 落盘）。别名：认领id/这是你/记住id/绑定id/这是你的id。已进 `/help`。
     - **挂载点在 `@ 判定之外**（`on_group_message_create` 里 `_learn_name_from_text` 之后），否则会出现「认不出@ → 进不了命令分支 → 用不了认领」的死锁。
     - 只认用户亲手 @ 的那一个 id ⇒ 绝不会误认到小鱼（比任何自动猜测都可靠）。
  3. **调试日志加明细**：`[@调试] … 明细={UserPayload id=F833… user='' is_you=None bot=True}`，一眼看出 is_you/username 到底取没取到。
- **验证**：落盘往返一致；模拟重启后仍记得；`/认领` 与 `/导入音色` 不互误触；认错 id 时替换而非累加（旧 id 清掉）。
- **排错顺序**：看到 `[@调试] … 未认领` → 直接发 `@小忺 /认领` 一次即可永久修好，不用再猜 payload。

### L. 纯表情消息触发主动接话（顺带修）
- **现象**：日志 `[主动] … 主动接话：<faceType=6,faceId="` —— 对纯表情消息乱接话。
- **根因**：主动接话的「有实质文本」判断用 `re.search(r"[\u4e00-\u9fffA-Za-z0-9]", content)`，而 QQ 的表情占位符 `<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">` 里自带 `faceId="0"` 等英数字 → 被误判成有内容。
- **修法**：判断前先 `re.sub(r"<[^<>\n]{0,200}>", "", raw)` 剥掉平台占位符（`<@openid>`/`<faceType=…>`），再用剥离后的 `_plain` 做长度与字符检查。
- **验证**：纯表情/空消息/只有 @ → 不接话；「我不在电脑旁边」「户外调研」→ 照常接话。

### K. 用音频文件复刻音色（/导入音色）
- **入口**：`@小忺 /导入音色 <音频文件路径>`（别名 用音色/设置音色/音色文件/训练音色），群聊+私聊均已挂载，已进 `/help`。走 `mimo-v2.5-tts-voiceclone`，把音频作为参考音色（`audio.voice = data:audio/wav;base64,...`）。
- **⚠️ 改为「只读登记」，绝不改动用户文件**（用户明确要求：机器人不能动我的文件，他自己把音频放项目里）。
  - 处理函数改为 `_adopt_voice_sample(path)`：**只校验不写盘** —— 检查存在 / 必须是 wav 或 mp3 / 体积 ≤7MB（base64 须 ≤10MB），然后把**原路径**写进 `voice_sample` 并清缓存。**不复制、不转码、不压缩、不覆盖、不移动**。
  - 已删除不再使用的 `_ffmpeg_audio()`（原用于转码/压缩）。
  - 不满足条件时明确回绝并说明原因（格式不对就让用户自己转；太大就让用户自己挑一段），**不会擅自处理**。
  - TTS 侧 `_load_voice_sample()` 本来就只是读取+base64，属只读，安全。
- **坑**：`os.path.relpath(dst, BASE_DIR)` **跨盘符会抛 ValueError**（如文件在 C:、BASE_DIR 在 E:）→ 已 try/except 兜底为存绝对路径（`_load_voice_sample` 本身支持绝对路径）。`_make_voice_sample` 同样修。
- 建议给 5~15 秒清晰人声、无明显背景音；太大/格式不支持会明确回复原因，不会崩。
- **验证**：wav/mp3 小文件正常导入且 mime 正确；9MB 超限被明确拒绝；.txt 被拒；文件不存在/空文件有提示；`/导入音色` 与 `/造音色` 互不误触。
- **❗读取范围硬约束（用户最新明确要求，勿放宽）**：机器人**只读项目内固定目录 `assets/训练音频/` 的音频，绝不读项目外任何文件，也不接受任意路径参数**。
  - 常量 `_VOICE_TRAIN_DIR = BASE_DIR/assets/训练音频`；`_list_train_audio()` 只扫该目录下的 `.wav/.mp3`。
  - `_handle_import_voice` 只接受**纯文件名**：含 `\ / :` 或 `..` 一律拒绝；无参数时列出目录内音频，唯一文件直接用，多个则提示指定文件名。
  - `_safe_path_within(path, parent)`：`realpath + commonpath` 做越界检查（防 `..` 穿越、绝对路径外逃、跨盘符）。
  - 双重保险：`_adopt_voice_sample()` 登记前校验；`_load_voice_sample()`（TTS 读取侧）也加安全闸，配置若被填成项目外路径直接忽略并打印 `[音色] 样音路径不在项目内，已忽略`。
  - **验证**：C盘/系统目录/`..` 穿越路径全部拒绝；目录内 wav/mp3 正常登记；`_safe_path_within` 4/4 通过；读取侧越界返回 None。
  - 用法：用户自己把 wav/mp3 放进 `assets/训练音频/` → `@小忺 /导入音色`（或 `/导入音色 xxx.wav`）。
  - `on_group_at_message_create` 里把开头 `<@openid>` 记进本群 openid 集合（AT 事件是 @ 本机器人的权威信号），全量入口据此也能识别。
  - 扩展 `_STORY_RE`：加入 `讲\s*个?\s*(?:恐怖|鬼|吓人|灵异|悬疑)?\s*故事` / `恐怖\s*故事` / `鬼\s*故事` / `讲个?鬼`，让「讲个恐怖故事 / 两个恐怖故事 / 讲恐怖故事」都能进故事分支。
- **验证**：模拟测试 `@F833`(机器人)→True、`@9293`(同群其他bot)在已学 F833 后→False（无误触发）、`mentions` 带 `bot` 标记→True；`_STORY_RE` 对「讲个恐怖故事/恐怖故事/鬼故事/小忺讲个恐怖故事」均命中。

### G. 讲故事/讲笑话像真人（新一轮修复）
- **现象**（截图实锤）：`@小忺 讲个故事恐怖` 正常讲；`@小忺 讲个故事开心` 却只回 3 秒语音「看他开心，我也开心了」——没讲故事。
- **根因**：`_NATURAL_RULES` 的「开心|高兴|快乐…」固定话术（约4880行）抢在 LLM 之前命中「讲个故事**开心**」里的"开心"关键词，直接闲聊回去了；叙事请求全靠 LLM 自觉，一被固定话术/闲聊拦截就废。语音情绪也不随请求点名的类型走。
- **修法**（三层，覆盖群聊/私聊/主动接话三条入口）：
  - 新增 `_parse_narrative(content)`：识别「讲个故事/讲个笑话/编个故事/我想听故事/来个段子」等自然表达（限定词可前可后，如「讲个故事恐怖」「讲个故事开心」），返回 `{'kind': 'story'|'joke', 'mood': 点名情绪}`。
  - 新增 `_narrative_directive(nk)`：给 LLM 的强指令——必须直接输出完整故事/笑话正文，禁止反问/闲聊/只给开头（注入到 get_answer 的 q 前）。叙事请求**跳过 `_natural_reply` 固定话术**（根治抢答）。
  - 新增 `_narrative_style(nk, text)`：用户点名情绪→确定性映射语音情绪（恐怖/鬼/悬疑→神秘，开心/搞笑→开心，悲伤→悲伤，童话→俏皮，睡前/温馨→温柔）；未点名→沿用 `_choose_story_emotion` 智能判断。新增 `_narrative_voice_plan()`：**只要语音开着就必发语音**（不受 frequency 随机影响），voice_only_chance 生效。
  - `/笑话` 指令和「讲个笑话」现在都走叙事管线→语音+俏皮/开心情绪；`_clean_story` 兼容笑话引导语。
- **验证**：22 个用例离线全过——截图两个问题场景分别映射到 神秘/开心 语音；「今天天气真好/我是远旅/看图」等不误触发；语音方案 frequency=0 也必发、语音关时回退文字。

### H. 音色一致性：聊天固定音色，只有故事/笑话变情绪（新一轮）
- **现象**：平时聊天语音音色每条都在变，听着很难受。
- **根因**：`_tts_mimo` 里「只要传了 style 就切 `mimo-v2.5-tts-voicedesign` 模型，且不再用配置里的 `voice` 音色」；而聊天/主动接话/私聊**每句都现算** `_choose_voice_emotion(reply)` 当 style 传进去 → 每句一个情绪 → 音色一直变。
- **修法**：聊天（群聊 else 分支）/主动接话/私聊/定时道安一律改为 `style=None`（不再逐句算情绪）→ 走 `mimo-v2.5-tts` + 配置的 `voice`(mimo_default) **固定音色**；只有故事/笑话走 `_narrative_style` 才传情绪（voicedesign，允许变）。
- **顺带修 bug**：定时道安 `_push_voice(g, body, style=_vstyle)` 里的 `_vstyle` **从未赋值**，一旦触发语音就是 NameError；已改为 `style=None`。
- **注意**：若用户用 `/情绪 温柔` 设了全局 style，聊天也会走 voicedesign（但情绪固定、音色仍一致），属预期。
- **验证**：静态检查 3 个故事/笑话调用点传 `style=style`、3 个聊天/主动/私聊调用点传 `style=None`；复刻 `_tts_mimo` 判定确认聊天→`mimo-v2.5-tts`+固定音色、故事/笑话→voicedesign。`_choose_voice_emotion` 已无调用点（保留备用）。

### I. 刷B站评论学「活人感」闭环（新功能）
- **入口**：`@小忺 /学说话 <B站BV号或链接>`（别名：学说话风格/学梗/活人感），群聊+私聊都已挂载（挂在 `_is_anime_cmd` 之后、`_extract_topic_keyword` 之前）。已加入 `/help`。
- **链路**：`_parse_bili_input`（抠 BV/av/aid）→ `_bili_bvid_to_aid`（`x/web-interface/view`）→ `_bili_comments`（`x/v2/reply?type=1&oid=aid&sort=2`，**每页 sleep 1s 低频**）→ `_filter_comments`（去广告/违禁/过短/纯颜文字/@，复用 `banned_words.json`，按点赞降序）→ `_save_corpus`（`assets/comments_corpus.jsonl`，**只存正文+点赞，不存用户名/UID，脱敏**）→ `_distill_style`（大模型提炼，JSON 容错解析 `_parse_json_loose`）→ `_apply_style`（写回 `persona.json` 的 `说话风格.口头禅` + `示例对话`，追加到 `网络热梗精选合集.txt`，并置 `_persona_cache` 失效实现热更新）。
- **要点**：人设靠 `load_persona()` 按 mtime 热更新，`_apply_style` 里显式把缓存置 `-1` 保证下一条回复即生效（无需重启）；写回前会保留 `_说明` 等元字段；口头禅/示例对话上限 20/12 且去重。
- **抖音**：暂不做（无公开评论 API，a_bogus 签名+强风控，易失效有风险），只做 B站。
- **🤖 自动版：`auto_learn.py`（无需手动指定 BV）**
  - 独立常驻脚本，复用主程序同一套逻辑（`_bili_hot_bvids` → `_bili_bvid_to_aid` → `_bili_comments` → `_filter_comments` → `_save_corpus` → `_distill_style` → `_apply_style`），写回 persona.json 后机器人靠 mtime 热更新**自动生效，不用重启机器人**。
  - 命令：`python auto_learn.py`（常驻）/ `--once`（跑一次）/ `--dry`（试运行不写回）/ `--force`（忽略已学记录）/ `--status`（看配置与进度）。
  - 新增主程序函数 `_bili_hot_bvids(source, rid, limit)`：popular=热门视频、ranking=排行榜（rid 分区），返回 `[(bvid, title)]`。
  - 三个文件：**配置** `assets/auto_learn.json`（首次运行自动生成默认；每次循环重读 → 改完即时生效，含 `enabled`/`interval_hours`/`source`/`rid`/`videos_per_run`/`pages_per_video`/`min_comments`/`bv_list`/`skip_learned`/`sleep_between_videos`）、**进度** `assets/auto_learn_state.json`（已学 BV、累计次数、上次结果）、**日志** `logs/auto_learn.log`。
  - 防风控：评论每页 sleep 1s（主程序已有）+ 视频之间 `sleep_between_videos` 5s；`skip_learned` 避免重复学同一个视频。
  - 常驻循环分段 sleep（60s 一段）并每轮重读配置，所以改 `enabled=false` 或调 `interval_hours` 很快生效。
  - **❗B站评论接口现状（2026-10-30 实测，很关键）**：
  - **未登录只能拿到 3 条热门评论预览**：`x/v2/reply` 的 `sort=2` 恒返回 3 条、`sort=0` 返回 0 条（code=0，不是报错），加 `buvid3` cookie 无效，`nohot=1`/`reply/main` 变体也一样。热门视频明明有几千条评论（stat.reply=6632）却只给 3 条。
  - **wbi 签名走不通**：`x/v2/reply/wbi/main` 需要 wbi key，但 key 来源 `x/web-interface/nav` 返回 **-101 账号未登录**（裸请求/带 buvid/完整 header 都不行）→ 拿不到 mixin_key → 签名后仍是 -403。
  - **结论：想拿完整评论必须登录态**。已在 `config.json` 加 **`BILI_COOKIE`** 字段（空=不登录）；新增 `_bili_headers()` 自动带上它，`_bili_bvid_to_aid`/`_bili_comments`/`_bili_hot_bvids` 都已改用（改 config 即时生效，脚本不用重启）。
  - **取法**：浏览器登录 bilibili.com → F12 → Network → 任一 api.bilibili.com 请求 → 复制 Request Headers 的 Cookie 整行粘进去。
  - **不登录也能跑的折中**：每个视频凑 3 条 × 多取几个视频（默认 `videos_per_run=12`、`min_total=15`），实测能凑到 15~18 条可用语料并正常提炼。已验证两轮真实学习成功。
- **其它三个坑**：
  1. `popular` 接口 **ps 过大会翻脸**（ps=36 直接取不到），固定 `ps=20`。
  2. **`popular` 的 `rid` 参数是无效的**（0/1/3/4/5/36/188 返回的都是同样那 20 个视频）→ 靠分区扩容必须改用 **`ranking`（`x/web-interface/ranking/v2?rid=X&type=all`），它的 rid 真的生效，每个分区约 100 个视频且各不相同**。所以 `source` 默认已改成 `ranking`。
  3. 榜单前几名短时间不变 → `pick_videos` **先过滤近期已学的再挑**（否则反复取同样几个视频，第二轮永远 0 条）；并按 `stat.reply` 评论数降序挑（评论多的才有得学）。
- **候选扩容方案（未登录也能持续学）**：`rids` 轮换分区 + 当前分区凑不够就继续换下一个分区（去重后取够 `videos_per_run`）；`relearn_after_hours`(24) 后允许重学（评论区会更新）。实测一轮能凑到 **40~46 条**可用语料。
- **顺手修**：`_apply_style` 的摘要里「示例对话现共 N」原先报的是截断前长度，已改为截断后的真实值。
- **BILI_COOKIE 已生效（2026-10-30）**：用户在 config.json 填了真实 `SESSDATA`（后面几个占位符字段「这里粘xxx」没换但被容错忽略，SESSDATA 足够登录态）。实测**登录后每视频能抓 60 条**（3 页×20），一轮累计 **600+ 条**语料（19 个视频 686 条），对比未登录的每视频 3 条是质变。
  - 为此加了 `config.json` 的 **mtime 热重载** `_config_fresh()`：`_bili_headers()` 每次调用现读最新配置 → **改 BILI_COOKIE 不用重启脚本/机器人**（原 `CONFIG=load_config()` 只在启动时读一次）。
  - `_bili_headers()` 加容错：cookie 含非 ASCII（如没替换的「这里粘」占位符）时先提示再剔除，避免难懂的 latin-1 报错。
  - 新增 `python auto_learn.py --check`：抓一个热门视频看评论条数，>5 条即 cookie 生效。
  - 常驻进程改完代码后必须**重启**（旧进程 import 的是改前模块）；重启方式：`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ?{$_.CommandLine -like '*auto_learn*'} | %{Stop-Process -Id $_.ProcessId -Force}` 再 `Start-Process python -ArgumentList "-X","utf8","auto_learn.py" -WorkingDirectory "E:\Desktop\jiqiren" -WindowStyle Minimized`。
  - 配置现状：`pages_per_video:3`、`relearn_after_hours:6`（让登录态尽快重刷高质量语料；原 24h 太浪费 cookie）。
  - **口头禅/示例对话上限已放宽（2026-10-30）**：`config.json` 新增 `STYLE_MAX_CATCH:60`（原 20）、`STYLE_MAX_EX:30`（原 12），`_apply_style` 改为从 `_config_fresh()` 读取（热生效）；单条口头禅长度限制 8→12。实测一轮从 20/12 涨到 23/17 且旧的没被顶掉。改完记得重启进程加载新代码。

- **🏠 来源改为「首页个性化推荐」（`source="recommend"`，2026-10-31）**：用户要求「从首页的视频学起，按算法推的更贴合我的性格爱好」。
  - 关键：首页推荐接口 `x/web-interface/wbi/index/top/feed/rcmd` **必须 wbi 签名**（之前拿不到 key 是因为没登录；现在填了 BILI_COOKIE，nav 返回 code=0 能拿到 key）。**坑**：wbi key 在 `data.wbi_img`（不是 `data.wbi`，后者是 null）；`/web-interface/nav` 成功但 `data.wbi` 是 null 时别慌，看 `wbi_img`。
  - 新增主程序函数：`_get_wbi_key()`（nav 拿 `wbi_img` 的 img_url/sub_url → MIXIN_KEY_ENC_TAB 重排 → 32 位 mixin_key，缓存 1 小时）、`_wbi_sign(params, key)`（加 `wts`、过滤 `!'()*`、排序、`md5(query+key)` 得 `w_rid`）、`_bili_recommend_bvids(limit, page)`（filter `goto=="av"` 过滤直播/广告，按推荐流原始顺序返回）。
  - auto_learn.py：`source="recommend"` 时按 `runs` 递增 `page=(runs%10)+1` 取不同批次首页视频（避免总学同一批），不再走 rids 分区轮换。
  - 实测：首页推荐返回的视频全是用户兴趣（异环/影之刃零等），一轮 735 条语料，示例对话涨到上限 30、口头禅 31、热梗 +7。
  - **wbi 签名算法常量 MIXIN_KEY_ENC_TAB 要完整**（64 项），缺失会算错 key → -403。
- **验证**（mock，不联网）：配置自动生成与缺字段补齐；`--dry` 不改动 persona.json；正式跑写回口头禅+示例对话+热梗、缓存失效、进度记录正确；第二次因 skip_learned 无新视频；`--force` 可重学；`--status` 正常。
  - 启动说明 `assets/启动.txt` 已补上这一行（注释形式，可选）。
- **验证**：离线全链路通过——解析 4 种输入格式；清洗 9→5（广告/违禁/过短/纯符号正确剔除）；JSON 容错解析（带 ```json 代码块+废话）；写回后口头禅 7→10（去重）、示例对话 3→5、`_说明` 保留、缓存已失效；热梗追加成功。

### J. 小忺专属音色（voicedesign 造样音 → voiceclone 长期复刻）
- **诉求**：按小忺人设生成一个专属音色用于聊天，且音色要恒定（之前 voicedesign 逐句换情绪会换模型→音色一直变）。
- **关键认知（来自 MiMo 官方文档）**：`mimo-v2.5-tts-voiceclone` **支持在 user 消息里传自然语言语气/情绪指令、也支持音频标签**，所以「音色」和「情绪」可以解耦——音色由样音锁定、情绪由指令切换，不必再切 voicedesign。
- **方案**：`voicedesign` 按人设**造一次**样音并存盘 → 之后**全部**用 `voiceclone` 复刻该样音（音色恒定+专属），聊天用 `chat_style`、故事用情绪指令。
- **改动**：
  - `voice_config.json` 新增 4 个字段：`voice_sample`(样音路径，相对 BASE_DIR)、`voice_sample_mime`(留空按扩展名)、`chat_style`(聊天固定语气指令)、`voice_design_prompt`(手动覆盖音色描述)。**注意**：`load_voice_config`/`save_voice_config` 的字段白名单元组已同步加上这 4 个，否则读不到。
  - 新增 `_load_voice_sample()`（读样音→`data:audio/wav;base64,...`，带 mtime 缓存）、`_tts_post()`（抽出单次请求，便于回退）。
  - `_tts_mimo` 改为**三级优先级**：①有样音→voiceclone（style 当语气指令，失败会打印并回退）②无样音但有 style→voicedesign ③都没有→预置音色。**向后兼容**：没配样音时行为与旧版完全一致。
  - 新增 `_build_voice_design_prompt()`：从 `persona.json` 的 说话风格.语气 / 性格.核心特质 拼出 1-4 句音色描述（只写人本身特征，不写混响/EQ 等后期词、不写矛盾特征；可用 `voice_design_prompt` 覆盖）。
  - 新增 `/造音色`（别名 生成音色/定制音色/音色生成）：合成样音→存 `assets/voice_xiaoxin.wav`→写回 `voice_sample` 配置→发一段试听。群聊+私聊均已挂载，已进 `/help`。样音台词 `_VOICE_SAMPLE_TEXT` 用小忺自我介绍（贴合温柔音色，符合官方「合成文本要贴合音色」建议）。
  - 已给 `assets/voice_config.json` 预填 `chat_style`（温柔轻柔的朋友闲聊语气）。
- **验证**：离线测试全过——音色描述正确生成(124字、无后期词)；造样音写盘+写配置成功；聊天与故事**都**走 voiceclone 且 `has_ref=True`（同一份样音=音色恒定），故事额外带上「神秘」情绪指令；voiceclone 失败能回退预置音色；没配样音时保持旧行为。
- **语速过慢修复（后续）**：根因是我写在两处的措辞都带「语速中等偏慢」——`_build_voice_design_prompt()` 的音色描述（会被 voicedesign 烤进样音）+ `voice_config.json` 的 `chat_style`（每次请求的语气指令）。已把描述改成「语速适中、轻快不拖沓」、`chat_style` 改成「语速适中偏轻快…不要拖长音」。
  - 新增 **`voice_speed`** 配置项（ffmpeg `atempo`，**只变速度不变音高**，0.5~2.0，1.0=原速），在 `_send_voice`/`_push_voice` 里于音效之后应用；无 ffmpeg 或非法值会安全跳过不崩溃。当前设为 **1.15**。
  - 注意：样音本身是用旧「偏慢」描述烤出来的，voiceclone 会继承其节奏 → 若改完仍偏慢，应重跑一次 `/造音色` 让新描述重新生成样音。
- **人设年龄 18 岁 / 女（音色一并改）**：`persona.json` 的 `身份` 段新增 `"年龄": 18`、`"性别": "女"`（小忺自己也知道自己是18岁女生）。`_build_voice_design_prompt()` 改为**按人设的年龄/性别自动生成**音色描述（读 `身份.年龄`/`身份.性别`，缺省 18/女；≤17 用「清亮明亮、稚嫩朝气」，18~24 用「清亮甜美、青春朝气」，>24 用「清亮柔和、干净稳重」），不再写死「二十岁出头」。已按新描述**重新生成**样音 `assets/voice_xiaoxin.wav`。
  - 以后改年龄只需改 `persona.json` 的 `身份.年龄` 再 `/造音色`；想完全自定义描述就填 `voice_config.json` 的 `voice_design_prompt`（优先级最高）。
- **❗每次 `/造音色` 都按「当前」人设重新生成描述**（用户诉求：人设一直在改，音色要跟着改）：
  - `voice_config.json` 的 `voice_design_prompt` 若被手填过会**永久固化**——改人设也不生效。这是本条要解决的核心隐患。
  - 新增 `_load_persona_fresh()`：**跳过 `_persona_cache` 直接重读 persona.json**，并顺带刷新缓存（造音色专用）。
  - 新增 `_voice_design_desc(force_persona=False)` → 返回 `(描述文本, 来源说明)`；`force_persona=True` 时**忽略手动覆盖值**、强制按人设重生成。`_build_voice_design_prompt(force_persona=False)` 保留为只返回文本的兼容包装。
  - `_make_voice_sample(save_path=None, force_persona=False)` 新增透传参数；`_handle_make_voice` 一律按 `force_persona=True` 调用。
  - `/造音色 重置`（别名 重来/清覆盖/按人设/重新生成）：清空 `voice_design_prompt` 覆盖值，之后也一直按人设走。带其他未知参数会提示而不执行。
  - 每次造音色后把描述+来源+时间落盘 `assets/voice_design_last.txt`，并打印 `[音色] 本次音色描述（来源）：…`，回复里也带上来源（"按人设生成（18岁的少女…）" / "手动覆盖"），一眼看出用的是哪套。
  - **验证**：18岁→14岁→30岁描述逐次变化（稚嫩/甜美/稳重切换正确）、语气与特质变化也生效、缓存同步刷新；有覆盖值时普通调用走覆盖、force 走人设。

### M. 唱歌功能（MiMo 唱歌模式，新功能）
- **诉求**：让小忺能「唱歌」。
- **关键技术（来自 MiMo 官方文档）**：`mimo-v2.5-tts`（**预置精品音色**）**支持唱歌模式**——在合成文本最开头加 `(唱歌)` 标签即可（等效 `sing`/`singing`）；而 `voicedesign`/`voiceclone` 两个模型**都不支持唱歌**。所以唱歌**只能用预置音色**，复刻的小忺专属音色用不了（平台硬限制，会自动回退到预置「茉莉」）。
- **改动**（全在 `qq_bot_relay.py`）：
  - `_tts_mimo(text, style=None, sing=False, voice=None)` 新增 `sing` 分支：命中时强制走 `mimo-v2.5-tts` + 预置音色 + `(唱歌)`+文本，user 指令作为唱歌情绪/语气。
  - 新增 `_send_song(message, lyrics, voice=None, style=None)`：复用 `_get_file_info(file_type=3, data=audio)` + `_reply(media, msg_type=7)` 发语音（与 `_send_voice` 同机制），长歌词按 120 字分段。
  - 新增 `_make_song_lyrics(theme, group_id, persona)`：LLM 按小忺人设现编 2~4 句中文歌词（主题=用户给的词/歌名；空则随机可爱主题），并清理掉会干扰 MiMo 标签的括号符号。
  - 新增 `_handle_sing(message, content, group_id=None)`：命令/自然触发统一入口。给歌词（多行/带句号/较长有逗号）直接唱；给歌名/主题则先回「现编一段唱给你🎤」再 LLM 现编；不给则即兴唱。
  - 新增 `_is_sing_request(content)` + `_SING_RE`：自然触发正则，**只命中明确『唱歌』祈使**（唱首歌/来一首/唱个歌/给我唱/想听你唱/点首歌/唱首…），**排除「这首歌好听」「喜欢这首歌」**等误触发（已验证 exclusion 正确）。
  - 命令分发：`on_group` + `on_c2c` 的 `/说` 之后都挂了 `/唱`/`/唱歌`/`/sing`；自然语言分支（`_natural_reply` 之前）插了 `_is_sing_request` 自然触发。
- **用法**：`@小忺 /唱 今天真开心，阳光暖暖的` 或 直接说「小忺唱首歌」「来一首歌」（不指定就即兴唱，给歌名就现编）。
- **验证**：`_tts_mimo(sing=True)` 实测返回 **47568 bytes** 音频（无报错），证明唱歌模式真能用；正则 exclusion 用例全对。`assets/启动.txt` 已补「/唱」说明。
- **首次版「效果太差」根因 + 改进（2026-10-31）**：最初 `_send_song` 用 `_split_for_tts(lyrics, 120)` 把歌词**切成多段、每段独立合成**——MiMo 每次调用是独立推理，**旋律无法跨段连贯**，用户听到的是几段破碎的"念歌词"。歌词生成也没限制长度，容易超长被切。
  - **改进**：①`_send_song` 改为**优先整首一次性合成**（`len(lyrics) <= SING_MAX_CHARS=120` 就整首 `(唱歌)`+全歌词一次合成），仅超长才按乐句（逗号/句号/换行）切；②`_make_song_lyrics` 限制生成 **4~6 句、整首≤90 字、押韵、有重复副歌**的规整短歌（实测"猫"主题生成 53 字带"喵喵喵喵叫个不休"副歌，判定整首合成）；③`_tts_mimo` sing 默认 user 指令改成更"演唱化"（"当作一首轻快的中文歌来演唱，注意旋律起伏和节奏感"）；④`_handle_sing` 在开头提示唱歌用的是预置音色（复刻音色不支持，自动回退茉莉）。
  - 验证：歌词质量合格、整首合成判断正确；MiMo 唱歌 API 此前已实测返回 47568 字节音频（可用）。改完需**重启主机器人 qq_bot_relay.py** 加载。
- **唱歌可从预置音色里独立选（2026-10-31）**：用户问"唱歌能不能从预置音色里选"。原本唱歌音色只能跟着全局 `/音色`（会连带改掉聊天音色，而聊天是复刻小忺、改了就丢了），或硬回退茉莉。现在新增**独立**的唱歌音色设置：
  - `voice_config.json` 加 `sing_voice`（默认「茉莉」），`load/save_voice_config` 白名单都已加该字段。
  - 新增命令 `/唱歌音色 <名称>`（别名 唱歌声音/唱音色/singvoice/sing_voice）：写 `sing_voice` 字段，**只影响唱歌、不动聊天的小忺音色**；不带参列出可选+当前。`_tts_mimo` 唱歌分支已读 `cfg.get("sing_voice")`，回退链 `voice or sing_voice or voice or 茉莉`。
  - `_handle_sing` 的开头提示改成读取实际唱歌音色（`「{sv}」音色唱的`），不再写死茉莉。
  - 验证：命令可列出/切换/容错，且 `_tts_mimo` 实测读到切换后的 `sing_voice`。注意验证脚本曾误把 `sing_voice` 写成苏打，已重置回茉莉并重启主进程。
- **已知小瑕疵**："唱个关于猫的歌"这类「唱个…歌」会被 `唱一?个` 分支把"唱个"当触发词、剩"关于猫的歌"当主题——不影响功能（小忺会现编一段关于它的歌），主题词多带个"歌"字而已。

### N. 自动学说话 auto_learn 改进（2026-10-31）
- **诉求**：之前 auto_learn 学了一堆网络烂梗（好家伙/绷/巴适/事已至此/谁懂/孩子们…），用户要「把之前学的全扔了，重新从首页学习点赞最高的评论的说话方式」（吐槽学到了很多糟粕）。
- **根因**：`_apply_style`（qq_bot_relay.py）是**累加模式**——每次学都把新词追加到 persona 的 口头禅/示例对话/热梗，只截断上限、**旧词永不删**，糟粕越积越多且无忘却机制。
- **改动**：
  1. 新增 `assets/persona.base.json`（手写本色基线：从当时 persona 提取——口头禅留前 7 条手写、示例对话留前 3 条手写，其余字段全量保留）；并备份 `persona.json.bak`、热梗库 `.bak`。
  2. qq_bot_relay.py：`_apply_style` 改为**「基线 + 本轮精华」覆盖**——写回时 口头禅/示例对话/热梗 以 `load_persona_base()` 为起点叠加本轮 `d`，不再读历史 persona 累加；用户手写「语气/字数倾向/称呼」等子字段保留。新增 `PERSONA_BASE_FILE` 常量、`load_persona_base()`、`save_persona()`。
  3. auto_learn.py：默认 `source` 改 `recommend`（首页个性化推荐，需 BILI_COOKIE，已确认已填）；`sample_n` 40→20（更聚焦高赞）；新增 `--reset` 命令（persona 说话风格回退 base + 清空热梗库 + 清空 state.learned，保留名字/身份/性格等手写）。顶部文档补 --reset 与「防糟粕累积」说明。
- **验证**：编译通过；`python auto_learn.py --reset` 实跑成功（口头禅回到 7 条手写、示例对话回到 3 条、热梗库清空，已替用户跑过一次）；隔离测试确认 `_apply_style` 在 persona 含 6 条历史糟粕时写回**只含 base+本轮、不含旧糟粕**且保留「语气」字段。config.json 的 BILI_COOKIE 已填 → recommend 首页可取视频。
- **用法**：`python auto_learn.py --reset`（已跑）；之后 `--once`/常驻从首页重新学；每轮都基于本色+本轮，不再滚雪球。

### O. 接入阿里云 Voice Studio TTS 作为可选聊天语音后端（2026-11-01）
- **诉求**：用户给了阿里云 convai-platform Voice Studio 的 TTS HTTP 接口文档，要求「接入另一个语音合成」。
- **方案**：把 voice_studio 做成**可切换的聊天语音后端**（独立于现有 MiMo 聊天/唱歌音色，唱歌仍走 MiMo）。
  - config.json 新增 `VOICE_STUDIO_API_KEY` / `VOICE_STUDIO_BASE_URL`(默认 https://convai-platform.aliyuncs.com) / `VOICE_STUDIO_MODEL`(默认 cosyvoice-v3-flash)。
  - voice_config.json 新增 `tts_backend`（默认 mimo）与 `voice_studio_voice`（默认 longxiaochun_v2）；fallback + load/save 白名单同步加入。
  - 新增 `qq_bot_relay._tts_voice_studio(text, voice=None, style=None)`：同步模式 POST /v1/tts/synthesize（Bearer 鉴权 + X-TTS-TN: enable 文本归一化），解析 `output.audio.url` 再下载字节返回；instruction 透传 style（部分模型支持）。
  - `_send_voice` 按 `tts_backend` 分发：voice_studio 调 `_tts_voice_studio`，否则仍 `_tts_mimo`（唱歌 `_send_song` 永远走 MiMo，未动）。
  - 新增辅助 `_tts_backend_key_ok()` / `_tts_backend_missing_msg()`，修正 `_handle_say` / `_cmd_voice` 的 key 校验（不再硬查 MIMO_API_KEY）。
  - 新增命令：`/tts引擎 mimo|voice_studio`（别名 语音引擎/ttsengine）、`/说话音色 <voice_id>`（别名 vs音色/vsvoice），注册进 COMMANDS。
- **验证**：编译通过；mock HTTP 验证 `_tts_voice_studio` 请求构造正确（URL/model/voice/auth/TN/format/sr 均符合文档）、响应解析与下载、缺 key 优雅报错；后端选择辅助函数三态正确。
- **用法**：在 config.json 填 `VOICE_STUDIO_API_KEY`，群里 `/tts引擎 voice_studio` 切换，再 `/说话音色 <你的音色ID>`。注意：voice_studio 当前用**同步整段返回**（已满足发语音场景）；SSE 流式逐句播放对「发语音文件」无收益，故未实现。

### P. 群友身份档案（手动教学：@群友 介绍 TA 是谁）（2026-11-01）
- **诉求**：用户要「在群里手动 @ 一个群友并说明他是谁（如『这是小明，我的同桌』），机器人就记住这个人是谁」。
- **实现**：
  - 新增存储 `assets/members.json`：`openid -> {name, note, by, updated}`（reader: `_load_member_profiles` / `_remember_member`）。
  - 新增入口拦截（在 `on_group_message_create` 顶部、跨事件去重闸之后）：`_try_remember_member(message, content, sender)` 与 `_try_recall_member(message, content, sender)`。
  - 识别被 @ 的非机器人、非发送者群友 openid：`_mentions_of(message)` 取 mentions，`_mention_fields` 过滤 `is_you/bot/self`，`_teach_openids` 返回列表。
  - **记住**两种写法：① 自然式 `@群友 这是/他是/她是/这是我/他是我/她是我/是我的/就是我的 <描述>`；② 命令式 `/记人|记群友|这是|他是|谁是谁 @群友 <描述>`。描述去掉 @昵称与昵称文字、去掉首尾标点、超 60 字或含疑问词(谁/什么/吗/？/?) 则不记（避免把提问当教学）。昵称优先用 mention.username。
  - **查询**：`/群友 @某群友` 回显我记下的 TA 身份；不带 @ 则列出我认识的群友。
  - 命令注册进 COMMANDS（`记人/记群友/这是/他是/谁是谁/群友/who` -> `_cmd_member`，兜底用法说明）；帮助菜单补充两行。
- **验证**：mock 消息单测——自然式/命令式/尾 @ /疑问句(不记)/空 @(不记)/查询/列表 全部正确；落盘 JSON 正确。测试产生的 `assets/members.json` 已清理。
- **注意**：依赖 `message.mentions`（botpy 提供）。若某环境拿不到 mentions，识别会静默失效但不报错。两条语音/双入口问题已由跨事件去重（section O 的 `_hkey` 机制）一并覆盖，本功能在其之后拦截、单次执行。

### Q. 情绪权重训练 + 人工标注工作流（2026-11-02 起，毕设核心）
- **标签集可扩展（>8 类、多标签）**：用户明确要「超过 8 种标签 + 单文本多情绪」。已把标签集从写死的 8 类改成**共享可配置文件 `assets/emotion_labels.json`**（JSON 数组，顺序即模型类别顺序）。训练/推理/标注工具三者共用同一份。
  - `core/emotion_model.py`：`LABELS` 改由 `_load_labels_impl()` 从 `assets/emotion_labels.json` 载入（缺文件回退默认 8 类）；`load_model` 成功后用权重里存的 `labels` 顺序覆盖 `LABELS`，保证推理维度与训练一致；`predict_emotions` 多标签（BCE，阈值 0.5），`predict_emotion_style` 对未知标签用标签名本身当语气。
  - `training/train_emotion.py`：标签集 = `assets/emotion_labels.json` 现有 ∪ 默认 8 类 ∪ 数据集 `emotions.jsonl` 里出现的新标签；训练完把最终 `all_labels` 写回 JSON **并存入权重 `{"labels": all_labels}`**，多标签向量按该顺序生成。
  - `emotion_labeler.py`（Tkinter 桌面标注工具，用户本机跑）：标签直接读写 `assets/emotion_labels.json`（首次用 8 默认初始化）；「➕ 添加标签」会实时写入该文件 → 之后训练时该标签即成为模型一个真实类别（真正可学习/预测）。
    - **布局**：左侧可滚动导航栏列出所有标签，`[快捷键] 标签名`，命中变色（黄/凹陷），新建标签自动出现；右侧正文 + 顶部翻页/跳未标注 + 底部"当前标签"。
    - **快捷键**：前 9 个标签用数字 1-9，其余字母；1-9/字母 切换情绪，←/→ 翻页。
    - **分割功能**（按用户澄清改版）：不是按标点自动断句，而是**在消息正文上点鼠标右键的任意位置**（或点「✂ 分割文本」按钮，按钮用光标位置/否则取中点）→ 在点击处的字符偏移把长消息切开，**切点自动补一个逗号**（若切点已是标点则不重复补），分成「前半段 / 后半段」两条独立条目，各自打情绪标签（前后情绪可不同），更利于"一段一情绪"。`←/→` 翻页切换条目。切太靠边（任一半为空）会提示无法分割。拆完自动存盘。
    - **撤回功能**：「↩ 撤回」按钮或 `Ctrl+Z`，撤销上一步（拆分 / 打标签 / 取消标签均可撤回，快照栈最多 30 步；每次改动前压入 `(items 浅拷贝快照, idx)`）。空栈时提示"没有可撤回的操作"。
- **权重文件**：`core/models/emotion_bert.pt`（截至 2026-11-02 仍未训练，机器人回退关键词规则）。推理：`predict_emotions` / `predict_emotion_style`（qq_bot_relay 的 auto 情绪路径在 3130 调用，与 `_EMOTION_PRESETS` 手动 `/情绪` 命令互不干扰）。
- **语料来源**：群消息自动存 `data/chatlog_raw.jsonl`（4741 条）。
- **标注工作流**（`training/label_emotion.py`）：`export` → 抽群消息去占位/去重/过滤 → `data/emotions_tolabel.jsonl`；`apply` → 并入 `data/emotions.jsonl`（按 text 去重）。标注格式 `{"text":"...","labels":["开心","温柔"]}`（多标签数组）。
- **训练生效步骤**：`python training/label_emotion.py apply` → `pip install torch transformers` → `HF_ENDPOINT=https://hf-mirror.com` → `python training/train_emotion.py`（生成 `core/models/emotion_bert.pt`）→ **重启机器人**（`load_model` 有 `_loaded` 缓存，必须重启）。
- **环境坑**：训练需 torch/transformers；国内 `HF_ENDPOINT=https://hf-mirror.com` 防 HuggingFace 拉不到。

## 2. 上一轮已验证的修复（勿回退）
- **@ openid 类型不一致**：`on_ready` 拿到的是 `user_openid`，群里 `@` 用的是 `group member_openid`，导致 `self._is_mention_bot` 字符串匹配失败。修法：@命中时把群里 `member_openid` 记回 `self._bot_openid`，后续 `<@openid>` 可直接命中。
- **voice_only 失败静默无响应**：`_maybe_speak` 现在返回发送结果；4 个调用点（群/私聊 × 故事/聊天）在 `plan=="voice_only"` 且语音失败时自动 `await _reply(content=reply)` 补文字。

## 3. 关键函数速查
| 函数 | 作用 | 位置提示 |
|---|---|---|
| `on_group_message_create` | 群消息主入口（记录+@+主动接话+bot过滤） | 搜索 `async def on_group_message_create` |
| `on_group_at_message_create` | @专属事件（已加 bot 过滤） | 搜索同名 |
| `on_c2c_message_create` | 私聊入口 | 搜索同名 |
| `_maybe_speak(message,text,mtype,style,plan)` | 补语音，返回 True/错误串 | 搜索同名 |
| `_voice_plan(mtype)` | 返回 none / voice_only / both | 搜索同名 |
| `_choose_voice_emotion(text)` | 免费关键词情绪选择 | 2789 行附近 |
| `_choose_story_emotion(text)` | 故事情绪(大模型精修，贵) | 搜索同名 |
| `_send_library_emoticon(message)` | 发库里表情 | 3292 行附近 |
| `_send_static_media` / `_reply_with_media` | 带图/带表情回复 | 搜索同名 |
| `load_voice_config` / `save_voice_config` | 读/写语音配置 | 搜索同名 |
| `_store_seen` | 跨消息引用缓存 | 搜索同名 |
| `_get_member_name` / `_display_name` / `_live_nick` | 认人 | 搜索同名 |

## 4. 配置旋钮
- **config.json**
  - `PROACTIVE_EMOTICON_CHANCE`（0.25）：主动接话甩库表情概率。
  - `EMOTICON_LIB`：库表情路径列表（60 张，已写）。
  - `IMAGE_LIB` / `AUTO_REPLY` 等见文件。
- **assets/voice_config.json**
  - `enabled`：总开关；`frequency`（0.3）：发语音概率；`voice_only_chance`（1.0）：只发语音不带文字概率。
  - `message_types`：chat/proactive/story/command 各自开关。

## 5. 验证方法（曾用，可复跑）
```powershell
cd E:\Desktop\jiqiren
python -X utf8 -c "import py_compile; py_compile.compile('qq_bot_relay.py', doraise=True); print('compile ok')"
```
- mock 已验证：① `voice_only_chance=1.0` 加载正确且 `voice_plan` 不再出现 `both`；② `_choose_voice_emotion` 关键词映射正常（难过→悲伤低沉、搞笑→开心轻快、恐怖→神秘耳语）；③ `_send_library_emoticon` 发 `msg_type=7`；④ `sender` 先于使用定义；⑤ 主动分支不含 `_reply_with_possible_image`。

## 6. 待确认 / 已知陷阱
- ❓ **小鱼是机器人还是真人？** 决定 bot-skip 是否足够。用户尚未回复。
- ⚠️ 词元预算逼近上限，情绪选择刻意用关键词免费版；若想更细腻可换 `_choose_story_emotion`（更贵）。
- ⚠️ 改完 `on_group_message_create` 务必 `sender` 在使用前定义（曾因顺序错踩坑）。
- ⚠️ 改 `load_voice_config` 的拷贝列表时，新增键要同时加进 `_VOICE_CFG_FALLBACK` 和 load/save 两处列表。
- 重启后建议发一句 `@小忺 讲个恐怖故事`，盯日志 `mention=True`、 `who='远旅'` 确认认人与响应恢复。

## 7. 用户原始诉求清单（本任务）
1. 学会主动消息发库里表情（配置我来写）✅
2. 语音依旧文字+语音 → 修 ✅（voice_only_chance 漏加载）
3. 语音情绪自动按原输出文本选择 ✅（关键词免费版）
4. 越界：没参与却提到我 / 凭空发图 / 接了小鱼的话 ✅（bot过滤+主动不配图+主动发表情）
5. 上一轮遗留"讲恐怖故事没反应/不会认人" → 同因为 NameError 已修 ✅
