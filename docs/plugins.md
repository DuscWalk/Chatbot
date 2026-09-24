# 插件与旧版功能迁移

以下配置用于 njuse 的「蕾缪安 · QQ私聊」。第三方代码放在被 Git 忽略的 `data/plugins/`，仓库只维护 [固定版本与配置](../plugins/lock.json)、安装脚本、蕾缪安插件和 Rolebot 功能插件。升级前重新做隔离验证，不自动跟随上游最新版。

| 功能 | 实现 | 当前设置 |
| --- | --- | --- |
| 连发消息合并 | continuous_message 2.9.1 | 停顿 2.5 秒后合并处理；命令直接通过，各好友分开排队 |
| 图片理解与出处核验 | Rolebot 0.1.0 调用 Qwen3-VL-Plus | 多图编号、引用图、24 小时缓存；需要时文字搜索；DeepSeek 负责人格回复 |
| 按需联网、时间 | Rolebot 0.1.0 | 使用现有百炼密钥检索，返回实际来源；当前时间按北京时间 |
| 长期记忆 | LivingMemory 2.7.0-beta.2 | 每 10 轮总结；最多召回 3 条；按私聊会话和人格隔离 |
| 低频主动聊天 | 蕾缪安插件 0.2.0 | 仅指定好友；周一、周三、周六北京时间 19–21 点随机一次 |

合并输入和把回复拆成 2–4 条是两件事：后者仍使用 AstrBot 原生分段发送。群聊不加载新增的记忆、防抖插件。

记忆从启用后的新对话逐步建立，不回填旧聊天。只记录对方明确表达的身份、偏好、计划和纠正，不把角色设定或助手编写的内容当成共同经历，不设好感度。记忆与原作知识库使用各自独立的数据文件。总结仍依赖模型，必要时可在 LivingMemory 的管理页面查看和删除错误记忆。

主动聊天至少距最近一次收发消息两小时；生成期间收到新消息、会话改变、人格不再是蕾缪安时跳过。重启后也等待两小时；跳过的时段不补发，每天最多尝试一次。复用私聊人格、原作检索和表达参考，仅把实际发出的内容作为助手消息保存，不伪造一条用户消息。当前只对已确认的那一位好友启用，未来加好友不会自动加入主动聊天列表。

图片识别和搜索复用现有百炼提供商，主聊天模型仍为 DeepSeek。图像最多处理 4 张，1–2 张总时限 50 秒，3–4 张 70 秒；单次模型调用最多 20 秒。视觉摘要保留在当前会话，支持后续追问；联网全文为临时参考，不保存为用户经历。缓存、排错记录均保留 24 小时；默认排错记录只含状态、数量和耗时。

### 从 qqBots 迁移的功能

| 旧版能力 | 当前实现与状态 |
| --- | --- |
| 人格、记忆、分段发送 | 复用原生人格与知识库、LivingMemory、原生分段发送；蕾缪安材料不变 |
| 工具路由、实时时间、Tavily 搜索 | Rolebot 提供北京时间；明确搜索或实时信息请求才联网，避免“你现在心情怎么样”等误触发。百炼 qwen-plus 的原生搜索接口返回可核对来源；有原生 Tavily Key 时可直接使用 |
| 多图识别、Lens、条件复判 | 移植原有证据管线；视觉模型提取，DeepSeek 表达。没有 SerpApi Key 时使用视觉模型和文字检索，**尚未启用 Lens 以图搜图**。不能保证每次认出角色 |
| GIF、视频 | GIF 抽取首/中/尾帧，只描述可见内容；视频使用 QQ 提供的 HTTP(S) 链接，受链接有效期和模型支持限制。不据此确认身份 |
| 群聊开关、静默、随机与关键词唤醒 | 已实现，**群白名单为空**。加入白名单后由 AstrBot 管理员 `/bot on` 启用；默认随机概率 0% |
| 同人追问、连续复读 | 90 秒同群同人追问窗口；至少两位用户连续相同消息触发一次，同内容冷却 10 分钟；保留安全 Face、带 subtype 的 Image、完整 mface 元数据，跳过未知卡片及不完整数据 |
| 群聊引用、发送失败处理 | 每人引用间隔 60 秒；同次回复有一段发送失败后停止后续发送 |
| 按需语音 | 接入原生 TTS 提供商，明确要求语音才触发，生成/发送失败回退文字；**尚未配置音色，默认关闭** |
| 表情库与 NapCat 收藏注册 | 移植清单、权重、发送元数据和去重注册；**旧素材已丢失，默认关闭** |
| 看门狗、邮箱告警、登录管理 | 服务重启交给 systemd，QQ 登录通过 NapCat WebUI；旧 SMTP 凭据与服务器已不可用，未启用邮件、远程邮件指令或自动扫码通知 |

新功能集中在 `plugins/astrbot_plugin_rolebot/`，纯规则、搜索、媒体和视觉管线分模块维护，来源记录在 `provenance.json`。旧仓库未修改，也未复制旧 NoneBot 运行时。日常验证只保留最近一次结果，不积累对话评测档案。

## 在前端调整

SSH 隧道打开后访问 <http://127.0.0.1:16185>：

- 「插件管理」→ continuous_message：调整等待时间。专用识图提供商留空、图片本地化关闭，由 Rolebot 统一处理。
- 「插件管理」→ Rolebot 功能迁移：搜索开关、视觉提供商、可选 SerpApi Key、群白名单、TTS 和表情概率。
- 「插件管理」→ LivingMemory：调整总结频率、召回数量；通过插件扩展页面管理记忆。
- 「插件管理」→ 蕾缪安 →「低频主动私聊」：开关、收件会话、星期、时段和空闲间隔。时间始终按北京时间解释；主动收件会话必须精确填写，不能用通配符。
- 「配置」选择「蕾缪安 · QQ私聊」：管理此配置的插件列表。直接关闭对应插件也能停用。

## 维护与恢复

```bash
conda activate astrbot-wsl
python scripts/lemuen.py build
python scripts/manage_plugins.py build
python scripts/manage_plugins.py stage
python -m pip install -r runtime/plugins/build/requirements.txt
python scripts/plugins_smoke.py
python -m unittest discover -s tests -v
```

`stage` 下载固定提交的归档并校验 SHA-256；`plugins_smoke` 使用临时根目录、假模型和模拟发送，无 QQ 适配器。`stage` 同时生成依赖清单。服务器上可额外运行 `python scripts/plugins_smoke.py --live`，使用服务器已配置的模型和嵌入 API 测试合成对话，仍不连接 QQ、不读真实聊天。最近一次结果覆盖在 `runtime/plugins/latest-check.json`。

将构建产物、维护脚本和 `plugins/lock.json` 同步到目标仓库后，在目标机停止 **AstrBot**，执行 `python scripts/manage_plugins.py install`，再启动 `qqbots2-astrbot.service`。安装脚本要求已有蕾缪安私聊配置、嵌入提供商和明确的单一主动收件人；读取会话标识来选择收件人，不读取聊天正文。首次升级会把图片识别交给 Rolebot；以后重装保留已保存的插件选项与提供商配置。仓库维护的记忆总结提示词仍会重新应用。

安装前自动备份配置、插件、插件数据和 AstrBot 数据库，位置记录在 `runtime/plugins/activation.json`；依赖升级前的列表保存在对应备份目录的 `pip-freeze.txt`。回退时停止 AstrBot，将备份中的文件恢复，移除备份中不存在的新插件目录，再启动 AstrBot；不要重启 NapCat。备份和运行目录含私密信息，不提交 Git。

上游：[消息防抖](https://github.com/aliveriver/astrbot_plugin_continuous_message)、[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)。自动 CD 仍受 njuse 内网 runner 的可达性限制。

## 可选语音、表情与群聊

语音：先在 AstrBot 添加 TTS 提供商（如已有的 GPT-SoVITS / CosyVoice 服务），再在 Rolebot 填该 Provider ID 并启用语音。原生「自动文字转语音」保持关闭，避免每条回复都转成语音。当前没有可恢复的蕾缪安音色，不用其他音色冒充。

表情：把素材放在服务器 `data/plugin_data/astrbot_plugin_rolebot/stickers/`，同目录建立清单：

```yaml
items:
  - id: hello
    file: hello.png
    tags: [reply]
    weight: 1
    summary: "[打招呼]"
```

开启表情后，管理员可 `/rolebot reload` 重读素材、`/rolebot register` 注册到 NapCat 收藏、`/rolebot status` 查看状态。完整商城表情可另填 `type: mface`、`emoji_id`、`emoji_package_id`、`key`；缺字段时仍以带 subtype 的本地图片发送。注册不是日常回复的前置要求。

群聊：在原生配置中选择需要的人格和模型，开启 Rolebot、添加群白名单，再在群里 `/bot on`。若要使用蕾缪安知识检索，也要把群会话加入蕾缪安插件的允许列表。管理指令为 `/bot on|off|mute 10m|prob 0-100|clear|status`；`clear` 重置当前短期会话，不删除长期记忆。当前私聊范围保持不变，不自动开启任何群。

百炼搜索接口依据：[阿里云官方联网搜索说明](https://help.aliyun.com/zh/model-studio/web-search)。使用纯文本 `qwen-plus`、`max` 策略和结构化 `search_info`；没有真实来源时不把模型输出伪装成已核验结果。联网与按需视觉核验会产生少量额外 API 费用。
