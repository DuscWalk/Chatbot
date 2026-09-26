# 蕾缪安资料

知识版本 0.3.2、人格 0.3.1、表达参考 0.3.5。已收录用户提供的《Sanctuary Inside》完整歌词；接入步骤和现状见 [使用说明](../../docs/lemuen.md)。

本目录只维护资料源：

| 文件 | 用途 |
| --- | --- |
| [persona.md](persona.md) | AstrBot 原生人格，默认博士身份，可自然理解现实身份 |
| [entries.json](entries.json) | 80 个条目：54 条事实、14 条人物自述、1 条展示、1 条 EP 歌词、8 条演绎分析、2 条项目约定 |
| [sources.json](sources.json) | 66 个来源记录及具体定位、版本与核读状态 |
| [aliases.json](aliases.json) | 人物别名与条目关联 |
| [voice-lines.json](voice-lines.json) | 38 条游戏语音原文及场景标题；每轮加入系统提示词，出处对应来源 V |
| [voice-guide.json](voice-guide.json) | 有出处的表达参考；手写样例明确标注，不当作官方经历 |
| [review-notes.md](review-notes.md) | 梦境、康复、人物关系等易错点 |

原作依据是固定快照 `bb8f9ac8db143a661577ed6ef5184d3c6e93d1d0`（2026-09-20，游戏数据 `26-09-18-14-18-57_b6fbbc`）中的可追溯文本。社区镜像与 BWIKI 用于取材，不是官方发布渠道。已核对本人档案、38 条台词、模组与三部重点活动的相关文本；尚未穷尽全部剧情、音频和官方补充资料。

《Sanctuary Inside》全文保存在 `entries.json` 的 L108，来源 UEP1 标为用户提供；52 行英文、大小写和重复副歌均按输入保留，未与外部歌词册或音频校对。构建后单独生成「蕾缪安-角色EP与歌曲.md」，以一个完整资料块导入，歌词意象按歌曲语境理解。

在仓库根目录、`astrbot-wsl` 环境中：

```bash
python scripts/lemuen.py check
python scripts/lemuen.py build
# 可选：用已有原文缓存核查出处
python scripts/lemuen.py check --cache-dir runtime/research/lemuen
```

生成的原生知识库导入文件、人格副本、插件 ZIP 和私聊分段设置 均放在 `runtime/lemuen/build/`，不进入 Git。导入 69 个原作资料块及 1 个 EP 歌词块；演绎分析与项目约定不混入事实库。每块保留时期、现实层、知情范围和出处。

修改资料时直接改本目录的源文件，再检查、打包；不用同步多套 Markdown 或评测报告。旧评测过程已清理，只保留 `tests/fixtures/lemuen.json` 的少量回归用例和运行目录里最近一次联调结果。
