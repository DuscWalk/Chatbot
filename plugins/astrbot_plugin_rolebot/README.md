# Rolebot 功能迁移

从本机旧 `qqBots` 迁移 AstrBot 尚未覆盖的行为。使用 AstrBot 4.27.4，支持 OneBot / NapCat。

功能：全部群/黑白名单、逐群管理员控制与回复限流、指定管理员使用 /reset 清空当前群聊/私聊上下文、最近 6 条或 3 分钟群消息背景、按需搜索和时间、通用多图识读、按问题提取视觉信息与可选 Lens、连续安全复读、引用冷却、显式请求语音、可配置表情、发送失败停止后续分段。人格与知识库独立维护。

配置、迁移对照和验证说明见仓库 [docs/plugins.md](../../docs/plugins.md)。可选功能没有凭据或素材时不启用。生产数据存于 `data/plugin_data/astrbot_plugin_rolebot/`，不写入插件源码。
