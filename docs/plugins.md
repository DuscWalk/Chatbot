# 私聊插件

以下配置用于 njuse 的「蕾缪安 · QQ私聊」。第三方代码放在被 Git 忽略的 `data/plugins/`，仓库只维护 [固定版本与配置](../plugins/lock.json)、安装脚本和蕾缪安插件。升级前重新做隔离验证，不自动跟随上游最新版。

| 功能 | 实现 | 当前设置 |
| --- | --- | --- |
| 连发消息合并 | continuous_message 2.9.1 | 停顿 2.5 秒后合并处理；命令直接通过，各好友分开排队 |
| 图片理解 | 同一插件调用 Qwen3-VL-Plus | 先描述图片，再交给 DeepSeek 按蕾缪安人格回复 |
| 长期记忆 | LivingMemory 2.7.0-beta.2 | 每 10 轮总结；最多召回 3 条；按私聊会话和人格隔离 |
| 低频主动聊天 | 蕾缪安插件 0.2.0 | 仅指定好友；周一、周三、周六北京时间 19–21 点随机一次 |

合并输入和把回复拆成 2–4 条是两件事：后者仍使用 AstrBot 原生分段发送。群聊不加载新增的记忆、防抖插件。

记忆从启用后的新对话逐步建立，不回填旧聊天。只记录对方明确表达的身份、偏好、计划和纠正，不把角色设定或助手编写的内容当成共同经历，不设好感度。记忆与原作知识库使用各自独立的数据文件。总结仍依赖模型，必要时可在 LivingMemory 的管理页面查看和删除错误记忆。

主动聊天至少距最近一次收发消息两小时；生成期间收到新消息、会话改变、人格不再是蕾缪安时跳过。重启后也等待两小时；跳过的时段不补发，每天最多尝试一次。复用私聊人格、原作检索和表达参考，仅把实际发出的内容作为助手消息保存，不伪造一条用户消息。当前只对已确认的那一位好友启用，未来加好友不会自动加入主动聊天列表。

图片识别使用百炼现有密钥，只有收到图片才调用视觉模型；文字聊天模型仍为 DeepSeek。能够描述图片并不等于总能认出角色；针对立绘的身份识别和按需联网检索留待后续完善。本次不新增搜索服务。

没有叠加第二套记忆，也没有启用链接抓取、群聊共存、礼貌性沉默、配额管理、新闻推送、语音、QQ 空间或生活模拟。当前私聊不需要这些功能。

## 在前端调整

SSH 隧道打开后访问 <http://127.0.0.1:16185>：

- 「插件管理」→ continuous_message：调整等待时间和识图提供商。
- 「插件管理」→ LivingMemory：调整总结频率、召回数量；通过插件扩展页面管理记忆。
- 「插件管理」→ 蕾缪安 →「低频主动私聊」：开关、收件会话、星期、时段和空闲间隔。时间始终按北京时间解释；主动收件会话必须精确填写，不能用通配符。
- 「配置」选择「蕾缪安 · QQ私聊」：管理此配置的插件列表。直接关闭对应插件也能停用。

## 维护与恢复

```bash
conda activate astrbot-wsl
python scripts/lemuen.py build
python scripts/manage_plugins.py stage
python scripts/plugins_smoke.py
python -m unittest discover -s tests -v
```

`stage` 下载固定提交的归档并校验 SHA-256；`plugins_smoke` 使用临时根目录、假模型和模拟发送，无 QQ 适配器。初次运行需在同一 Conda 环境安装两项上游插件的 `requirements.txt`。服务器上可额外运行 `python scripts/plugins_smoke.py --live`，使用服务器已配置的模型和嵌入 API 测试合成对话，仍不连接 QQ、不读真实聊天。最近一次结果覆盖在 `runtime/plugins/latest-check.json`。

将构建产物、维护脚本和 `plugins/lock.json` 同步到目标仓库后，在目标机停止 **AstrBot**，执行 `python scripts/manage_plugins.py install`，再启动 `qqbots2-astrbot.service`。安装脚本要求已有蕾缪安私聊配置、嵌入提供商和明确的单一主动收件人；读取会话标识来选择收件人，不读取聊天正文。它会应用本文所列的配置，手动调整后再次执行安装会覆盖这些受维护的设置。

安装前自动备份配置、插件、插件数据和 AstrBot 数据库，位置记录在 `runtime/plugins/activation.json`；依赖升级前的列表保存在 `runtime/plugins/pip-before.txt`。回退时停止 AstrBot，将备份中的文件恢复，移除备份中不存在的新插件目录，再启动 AstrBot；不要重启 NapCat。备份和运行目录含私密信息，不提交 Git。

上游：[消息防抖](https://github.com/aliveriver/astrbot_plugin_continuous_message)、[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)。自动 CD 仍受 njuse 内网 runner 的可达性限制。
