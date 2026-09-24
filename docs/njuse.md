# njuse 部署与访问

服务部署在 SSH 别名 `njuse` 指向的 Ubuntu 24.04 服务器上，由普通用户 `ubuntu` 运行。

| 项目 | 配置 |
| --- | --- |
| 部署目录 | `/home/ubuntu/qqBots2.0` |
| Conda 环境 | `/home/ubuntu/miniforge3/envs/astrbot-wsl` |
| AstrBot | `qqbots2-astrbot.service` 用户服务，监听 `127.0.0.1:6185` |
| NapCat | `qqbots2-napcat-1` Docker 容器，后台监听 `127.0.0.1:6099` |
| OneBot | `ws://127.0.0.1:6199/ws`，仅服务器内部使用 |

本地数据和 NapCat 配置已迁移到服务器；模型与 QQ 登录已由用户配置。本地机器人实例保持停止，防止同一账号同时登录两个实例。2026-09-24 已启用[蕾缪安人格与知识库](lemuen.md)，覆盖全部 QQ 私聊，群聊沿用原配置。用户重新扫码后，已确认 QQ 登录成功、NapCat 与 AstrBot 的 OneBot 连接建立，服务状态正常。

## 本地浏览器

- AstrBot：http://127.0.0.1:16185
- NapCat：http://127.0.0.1:16099/webui/

本地 WSL 的 `qqbots2-njuse-tunnel.service` 用户服务负责 SSH 隧道，断线后自动重连。访问需要本机能够通过 SSH 连接 `njuse`；内网/VPN 不可达时，隧道会等待网络恢复。

```bash
systemctl --user status qqbots2-njuse-tunnel.service
systemctl --user restart qqbots2-njuse-tunnel.service
# 停用本地自动隧道：
# systemctl --user disable --now qqbots2-njuse-tunnel.service
```

其他本地机器先配置自己的 `njuse` SSH 别名，然后在项目目录运行 `./scripts/tunnel.sh njuse`。该脚本在前台运行，需保持终端打开；不要同时启动两个占用相同本地端口的隧道。

## 登录和管理

在本地终端查看服务器上的登录凭据（不会写入 GitHub）：

```bash
ssh njuse 'cd /home/ubuntu/qqBots2.0 && ./bot credentials'
```

打开 NapCat 页面填写 Token，再用机器人 QQ 扫码确认登录。在 AstrBot 页面添加模型提供商、API Key 和默认聊天模型。真实 QQ 收发需要完成这两步后验证。

```bash
ssh njuse 'cd /home/ubuntu/qqBots2.0 && ./bot status'
ssh njuse 'cd /home/ubuntu/qqBots2.0 && ./bot check'
ssh njuse 'cd /home/ubuntu/qqBots2.0 && ./bot logs astrbot'
ssh njuse 'cd /home/ubuntu/qqBots2.0 && ./bot restart'
```

服务器已启用用户服务 linger，退出 SSH 不影响机器人运行。关闭本地 SSH 隧道也不会停止服务器机器人。服务器后台没有开放公网端口。

## 更新与 CI/CD

CI 继续由 GitHub 托管 runner 执行。`njuse` 是内网服务器，GitHub 托管 runner 无法直接 SSH 到达，因此当前不启用自动 CD。

要启用 GitHub 部署，需要注册能访问 `njuse` 的自托管 runner，在仓库级变量设置 `DEPLOY_RUNNER_LABELS`（例如 `["self-hosted", "linux", "x64", "njuse-network"]`），并按主 README 配置 `production` 变量与 Secrets。`DEPLOY_USER` 使用 `ubuntu`，`DEPLOY_PATH` 使用 `/home/ubuntu/qqBots2.0`；`DEPLOY_HOST` 填 runner 可访问的服务器地址，而不是仅本机 SSH 配置里的别名。

服务版本记录在服务器 `runtime/deployed-revision`。运行数据、API Key、QQ 登录状态和后台凭据仅保存在服务器及本地迁移备份中，不进入 Git。
