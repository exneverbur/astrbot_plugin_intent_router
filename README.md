# 智能意图路由插件（Intent Router）

用低成本 LLM 判断群聊里哪些消息**值得 AI 主人格主动回复**，实现"零触发词"的主动感知，同时通过批量判断、噪音过滤、结果缓存控制 Token 消耗。

插件只做意图判断与消息路由，**不生成任何回复内容**。被判定"值得回复"的消息会带唤醒标记重新进入 AstrBot 消息管道，由主人格（AI 对话插件）正常回复，保证回复风格与上下文连贯。

## 特性

- **只处理群聊**：handler 仅注册群聊消息，私聊完全不参与；群聊中 @、回复机器人、唤醒前缀、命令、提到机器人称呼的消息直接放行。
- **低成本判断**：`judge_provider` 在面板下拉选择已配置的 LLM 供应商（每个供应商自带模型，选中即同时确定模型），判断 LLM 与主人格分离。
- **简略人设注入**：`persona` 一句话人设注入判断 Prompt，让模型按"这个人设会回复什么"来判断，不复制完整人设，省 Token。
- **按群批量判断**：每个群独立聚积消息、独立带上自己的群聊背景，一次调用判断最多 `max_batch` 条，只消耗一次 System Prompt。
- **随机自适应等待**：冷群只等随机初始时长（约 5~10 秒），火热群随新消息不断随机延长等待以攒更多对话，总等待不超过 `batch_interval`。
- **规则快速通道**：噪音（纯表情、纯标点、纯哈哈、过短消息、无信息量词汇）直接拦截，不调用 LLM。
- **谁对谁说**：模型根据消息内容与群聊上下文自行推断每条消息的发送者、接收对象和是否在对机器人说话，对机器人说的默认值得回复、两人之间的对话默认不插嘴。
- **结果缓存**：同会话相同文本在 `cache_ttl` 内复用判断结果，避免重复调用。
- **防刷屏**：默认一批只放行最新一条值得回复的消息（`release=latest`）。
- **其他插件优先（透传）**：可配置为不拦截消息，让表情包、签到等其他插件正常处理；其他插件已回复时 AI 不再重复回复。
- **与[虚拟世界](https://github.com/exneverbur/astrbot_plugin_virtual_world)插件联动**: 可以获取[虚拟世界](https://github.com/exneverbur/astrbot_plugin_virtual_world)插件的回复意愿实时调整本插件的回复阈值; 

## 安装与部署

1. 将 `astrbot_plugin_intent_router` 目录放入 AstrBot 的插件目录（如 `addons/plugins`）。
2. 在 AstrBot 面板重载/重启插件。
3. **重要**：关闭 AstrBot 内置的"群聊主动回复"（`平台设置 -> 群聊上下文`，即 `provider_ltm_settings.active_reply`），避免内置随机主动回复与本插件重叠出现双回复。
4. 配置 `judge_provider`：在面板下拉列表中选择一个已配置的 Provider, 建议使用低成本模型。
5. 在 `persona` 里写人设, 可以写明对哪些话题感兴趣, 更愿意回复，例如 `一只会吐槽的猫娘，喜欢科普和玩梗, 对AI相关话题感兴趣`。

## 工作原理

```
群聊消息到达
 ├─ 私聊（不经本插件，正常对话）              → 主人格正常回复
 ├─ @bot / 回复bot / 唤醒前缀 / 命令 / 提到别名 → 直接放行，主人格正常回复
 ├─ 规则噪音（表情/打卡/纯哈哈/过短）          → 拦截，零 LLM 成本
 └─ 待判断（无指向群消息）                    → 进该群缓冲
       └─ 攒满 batch_size / 到达动态截止时间 / 超过 batch_interval
             → 一次 LLM 批量判断（带本群最近 context_len 条背景）
             ├─ 不值得 → 丢弃
             └─ 值得   → 重注入最新一条（带 @ 唤醒标记 + 防循环标记）→ 主人格回复
```

### 多群隔离

- 缓冲、群聊背景、缓存均按 `unified_msg_origin`（会话/群）独立存储，互不混用；
- 每个群独立触发批量判断，Prompt 只包含该群自己的消息与背景；
- 不同群的判断调用串行执行（同一时刻只跑一个批量判断），对 Provider 限流友好。

### 自适应等待

每组缓冲维护一个动态截止时间：

- 首批消息入队：随机初始等待 `interval_min ~ 2×interval_min` 秒（默认 5~10 秒）；
- 每新加入一条消息：随机延长 `0 ~ interval_step` 秒；
- 硬上限：总等待不超过 `batch_interval`（默认 30 秒）；
- 触发刷新条件：攒满 `batch_size` 条、到达动态截止时间、或超过 `batch_interval`。

关闭 `adaptive` 后固定按 `batch_interval` 等待。

## 意图判断说明

判断 System Prompt 要求模型先判断"谁对谁说"，再输出简短原因和判断结果，输出格式：

```json
{"results":[
  {"idx":1,"from":"张三","to":"机器人","directed":true,"reason":"明确向机器人提问","worth":true,"confidence":0.9},
  {"idx":2,"from":"李四","to":"王五","directed":false,"reason":"两人之间的对话","worth":false,"confidence":0.8}
]}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `idx` | 消息序号，从 1 开始，与消息列表对应 |
| `from` | 发送者（抄自消息列表） |
| `to` | 模型推断的接收对象：机器人 / 某个用户昵称 / 大家 / 不确定 |
| `directed` | 是否明确在对机器人"我"说话 |
| `reason` | 简短判断原因（不超过 15 字） |
| `worth` | 是否值得回复 |
| `confidence` | 0~1 置信度 |

判定规则：

- `to=机器人` 或 `directed=true`：默认值得回复，除非明显不友好或纯噪音；
- `to=某个具体用户`（两人对话）：默认不值得回复，除非 AI 能明显补充价值；
- `to=大家` 或 `不确定`：按通用标准判断（提问、求助、分享、情绪等值得回；社交邀约、纯闲聊、隐私打听等不回）。
- 某个话题已在多人之间连续讨论多轮、且机器人从未参与时，属于人类之间的持续对话，默认不中途插入回复。

群聊中人们通常不会特意使用"回复"功能，因此 `to` 由模型根据消息内容与上下文自行推断；消息列表中的"(回复 xxx)"标记只作为辅助参考。另有 `bot_aliases` 配置，消息直接提到机器人称呼（如"机器人""bot"）时走规则快速通道直接放行，不经过 LLM。

自定义 `judge_prompt` 时支持 `{persona}` 占位符，且会自动追加上述"谁对谁说"指向性规则。

## 配置项

面板中每一项都有完整的 `hint` 说明，以下为简要说明：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | true | 总开关 |
| `judge_provider` | string | 空 | 判断用 LLM 供应商（面板下拉选择，含模型），留空用会话默认 Provider |
| `persona` | string | 乐于助人的AI助手 | 简略人设，注入判断 Prompt |
| `judge_prompt` | string | 空 | 自定义判断 System Prompt，支持 `{persona}` 占位符 |
| `enable_batch` | bool | true | 批量判断开关（false=单条即时判断） |
| `batch_size` | int | 5 | 触发批量判断的条数 |
| `batch_interval` | int | 30 | 批量等待硬上限（也是无指向群消息的最长回复延迟） |
| `adaptive` | bool | true | 随机动态等待：初始随机 + 每条新消息随机延长，上限 `batch_interval` |
| `interval_min` | int | 5 | 随机初始等待的下限（初始值取 `interval_min~2×interval_min`） |
| `interval_step` | int | 5 | 每条新消息随机延长 0~`interval_step` 秒 |
| `max_batch` | int | 20 | 单次最多判断条数 |
| `buffer_cap` | int | 100 | 每群缓冲上限，超出丢最旧 |
| `buffer_age` | int | 60 | 缓冲消息过期秒数，超时丢弃 |
| `threshold` | float | 0.5 | `worth=true` 且 `confidence≥阈值` 才放行 |
| `release` | string | latest | latest=只放行最新一条值得回的；all=全部放行 |
| `fail_mode` | string | block | LLM 不可用/解析失败时：block=拦截，pass=放行 |
| `min_length` | int | 2 | 有效字符数（中文/字母/数字）低于此值视为噪音 |
| `noise_words` | list | [...] | 无信息量词汇，整条精确匹配即拦截 |
| `pass_prefix` | list | [] | 自定义前缀，以此开头直接放行 |
| `bot_aliases` | list | [] | 机器人称呼别名，提到即视为对机器人说话，直接放行 |
| `cache` | bool | true | 判断结果缓存开关 |
| `cache_ttl` | int | 300 | 缓存秒数 |
| `cache_cap` | int | 2000 | 缓存上限，超出清理最旧 |
| `context_len` | int | 10 | 批量判断附带最近 N 条本群背景（0=不带） |
| `whitelist` | list | [] | 只对这些群生效，空=所有群 |
| `blacklist` | list | [] | 这些群不生效（隐私/合规用） |
| `json_mode` | bool | false | 以 JSON mode 请求（OpenAI 兼容 Provider），不支持的会自动降级重试 |
| `temperature` | float | 0 | 判断 LLM 温度 |
| `max_tokens` | int | 256 | 判断输出上限 |
| `concurrency` | int | 4 | 判断调用并发上限 |
| `debug` | bool | false | 调试模式：后台日志输出判断输入、输出与逐条结果（含群消息内容，注意隐私） |
| `passthrough` | bool | false | 其他插件优先（透传）：不拦截消息，其他插件已回复时 AI 不重复回复 |

## 行为说明与注意点

- **直接消息零延迟**：私聊不经本插件；群聊中的 @、回复机器人、唤醒前缀、命令、提到 `bot_aliases` 的消息直接放行，不判断、不拦截。
- **延迟**：无指向群消息从入队到回复约为随机初始等待（冷群 5~10 秒）至 `batch_interval`（火热群最多 30 秒）加上一次判断与主人格生成时间。想更快可调小 `batch_interval`/`interval_min`，或 `enable_batch=false` 走单条即时判断。
- **透传模式**：开启 `passthrough` 后，噪音和待判断消息不再拦截，其他插件可正常处理；若其他插件已回复该消息，AI 不再重复回复（`release=latest` 时还会自动回退到上一条值得回复的消息）。
- **防刷屏**：默认一批只回最新一条值得回复的消息；如需全部回复可设 `release=all`。
- **隐私**：待判断的群消息文本会发送给 `judge_provider` 对应的 LLM Provider，请确认其隐私政策；可用 `blacklist` 排除敏感群，`debug` 也会输出消息原文，排查完记得关闭。
- **成本**：噪音过滤、别名快速通道和缓存让大多数消息不调用 LLM；批量判断复用一次 System Prompt。插件卸载时日志会输出累计统计（调用次数、拦截/放行数、token 用量）。
- **已知限制**：纯图片（无文字）消息不参与判断，直接放行。

## 常见问题

**Q: 为什么群消息没有被回复？**

依次确认：1) `enable=true`；2) `judge_provider` 已选择或会话默认 Provider 可用；3) 该群不在 `blacklist`；4) 已关闭内置"群聊主动回复"；5) 消息被判断为"值得回复"且置信度达到 `threshold`。可打开 `debug` 在后台日志查看判断输入、输出和每条消息的原因。

**Q: 回复太慢了怎么办？**

调小 `interval_min`（冷群）或 `batch_interval`（硬上限），或 `enable_batch=false` 走单条即时判断。

**Q: 机器人刷屏怎么办？**

保持 `release=latest`，必要时调低 `batch_size` 减少积压。

**Q: 判断不准怎么办？**

优化 `persona` 使其更贴合场景，或自定义 `judge_prompt` 补充你的"值得/不值得"标准；打开 `debug` 查看每条消息的 `from/to/directed/reason`，针对性调整；调高 `threshold` 更保守、调低更话痨。

**Q: 我的其他插件（表情包、签到）收不到消息怎么办？**

开启 `passthrough=true` 改为透传模式，其他插件即可正常处理；若其他插件已回复，AI 不会重复回复。
