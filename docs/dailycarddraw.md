# 每日抽卡

插件固定为 i000stea/astrbot_plugin_dailycarddraw 0.2.1（aa283982），与 Node.js 后端、MySQL 8.4 一起部署到 njuse。私聊和群聊均启用；不需要聊天模型参与抽卡。

## 使用

| 指令 | 功能 |
| --- | --- |
| `/抽卡`、`/寻访` | 单抽 |
| `/抽卡 十连`、`/十连寻访` | 十连 |
| `/今日抽卡` | 今日次数和结果 |
| `/抽卡历史`、`/抽卡统计` | 历史和统计 |
| `/抽卡帮助` | 完整帮助 |
| `/卡池列表` | 管理员查看卡池 |
| `/重置抽卡次数 QQ号 卡池ID` | 管理员重置当日次数 |

njuse 使用 **全干员娱乐池，共 431 张卡**：国服 2026-09-20 数据中的 429 名可获得干员，加上阿米娅的近卫、医疗职业形态。包括限定、联动和赠送干员，排除敌人、召唤物及不可获得单位；蕾缪安也在池中。每张卡都有对应头像。

同星级内等概率，没有 UP，不模拟官方寻访或保底。默认星级概率如下，可在后台修改：

| 星级 | 六星 | 五星 | 四星 | 三星 | 二星 | 一星 |
| --- | --- | --- | --- | --- | --- | --- |
| 概率 | 2% | 8% | 50% | 30% | 8% | 2% |

默认每人每池每天一次单抽、一次十连，同一 QQ 在不同群及私聊共享次数，按北京时间自然日重置。完整池沿用原 `normal_pool` 的 ID，已有抽卡记录和已用次数保留。普通群聊的冷却、静默和关闭规则仍然适用。

运行 `./scripts/tunnel.sh njuse` 后，抽卡管理页为 <http://127.0.0.1:13100/admin>。如果已有 AstrBot 隧道，可以另开：

```bash
ssh -N -L 127.0.0.1:13100:127.0.0.1:3100 njuse
```

本机可在 `runtime/dailycarddraw/njuse-panel.json` 查看后台账号和密码；服务器完整凭据保存在项目目录的 `runtime/dailycarddraw/credentials.json`，字段是 `panel_username` 和 `panel_password`。密钥不写入 Git，也不会在部署日志中打印。首次安装沿用 AstrBot 管理员作为抽卡管理员，之后可在 AstrBot 插件配置修改。卡牌 JSON 导入、卡池权重和每日次数通过抽卡管理页调整。

## 部署与数据

- `plugins/lock.json` 固定插件归档、校验和及默认配置；第三方代码存放在被忽略的运行目录。
- `compose.dailycarddraw.yaml` 单独管理数据库与后端，服务器仅监听 `127.0.0.1:3100`，MySQL 不发布端口。内存上限分别为 512 MiB、256 MiB。
- `deploy/dailycarddraw/` 保存固定基础镜像、补全 sharp 依赖的 npm 锁文件，以及恢复卡牌导入接口的最小补丁。
- `deploy/dailycarddraw/catalog.json` 保存完整卡牌、娱乐池权重及游戏数据来源的固定提交与校验和。数据来自 Kengxxiao/ArknightsGameData 的国服解包，头像来自固定版本插件附带资源。
- `scripts/manage_dailycarddraw.py` 负责构建、独立启停、连通性检查与显式卡池导入；常规部署不会重新导入卡池，也不会重置后台调整的权重。
- MySQL 数据存放在 Docker 卷 `qqbots2-dailycarddraw_mysql`，图片缓存使用单独的 `qqbots2-dailycarddraw_images` 卷。部署与重启不会重新导入示例卡牌或清空已有抽卡记录。**生产环境不要使用 `docker compose down --volumes`。**
- 后端账号和数据库密码只在首次准备时生成。常规部署不修改数据库结构；后续上游 schema 升级需单独备份和迁移。

```bash
conda activate astrbot-wsl
python scripts/manage_plugins.py stage
python scripts/manage_dailycarddraw.py up
python scripts/manage_dailycarddraw.py check
```

新安装的数据库仍从上游 7 张示例卡牌开始。需要完整池时，在后端所在服务器运行一次：

```bash
python scripts/manage_dailycarddraw.py check-catalog
python scripts/manage_dailycarddraw.py import-catalog
```

导入先校验全部头像与图标，再按 `card_key` 更新或新增卡牌，并替换 `normal_pool` 的成员、星级权重、名称和描述。它保留既有卡牌 ID、人工禁用状态、说明及卡池每日额度，不操作用户历史和次数。再次执行会恢复清单里的成员与概率，因此后台调整后不要随意重复执行。导入前的卡牌和卡池配置保存在 `runtime/dailycarddraw/catalog-before-import.json`，最近一次结果保存在 `latest-import.json`；两者不入 Git。

CI 使用临时数据库验证完整池导入、再次导入后的 ID 与已用次数保留、抽卡、跨群次数、历史统计、单抽/十连图片和后台鉴权；测试数据库用后销毁，不发送 QQ 消息。后端镜像在 AstrBot 停止前准备，部署失败时恢复旧镜像；数据库卷始终保留。更新过程不重启 NapCat。

服务器无法下载基础镜像或 npm 依赖时，可以在本机运行 `python scripts/manage_dailycarddraw.py build`，再将输出标签对应的镜像通过 `docker save`、SSH 和 `docker load` 传到 njuse。镜像标签由上游归档、补丁、Dockerfile 和依赖锁文件的内容共同决定，服务器已有同一标签时直接复用。
