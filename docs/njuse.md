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

2026-09-24 已验证 njuse 能主动访问 GitHub API、代码归档、Actions broker 和结果服务；无需本机中转，也不开放新的入站端口。GitHub release 安装包下载曾超时，runner 安装包经本机下载、SHA-256 校验后通过 SSH 传入。runner 已注册并主动连接 GitHub 领取任务，2026-09-24 的[首次完整 CI/CD](https://github.com/DuscWalk/Chatbot/actions/runs/36004240998)已成功部署 `99debe4`。部署不依赖本地电脑在线。

- CI 运行在 GitHub 托管机器；检查成功后，main 分支推送由专用 `chatbot-njuse-deploy` runner 部署。
- Runner 安装目录 `/home/ubuntu/actions-runner-chatbot`，用户服务 `qqbots2-actions-runner.service`。已注册，服务已启用并常驻运行；重启服务器后自动连接 GitHub。
- Runner 不使用通用 self-hosted/linux 标签；服务器上的任务启动钩子检查仓库、main 分支、工作流路径和事件类型，拒绝 PR 任务。
- 部署不需要 GitHub 中保存服务器 SSH 私钥。默认直接更新 `/home/ubuntu/qqBots2.0`，使用 `astrbot-wsl`。
- 若以后切换本机 WSL runner，可使用同一专用标签，将 `DEPLOY_TRANSPORT` 设为 `ssh`、`DEPLOY_HOST` 设为 `njuse`，复用 `duscwalk` 的 SSH 配置。部署时本机及内网连接须在线。

重建 runner 时首次注册：在仓库 Settings → Actions → Runners → New self-hosted runner 选择 Linux x64，把页面配置命令里的短期 token 写入本地被忽略的 `.env`，变量名 `GITHUB_RUNNER_REGISTRATION_TOKEN`。令牌通过 SSH 标准输入交给 `scripts/manage_runner.py register`，不进入 Git、命令参数或聊天记录。GitHub 的 Git SSH 密钥不能替代此注册令牌。准备命令为：

```bash
python scripts/manage_runner.py prepare
# register 从标准输入读取一次性令牌，由维护工具传入
systemctl --user status qqbots2-actions-runner.service
```

默认 main 推送通过 CI 后自动部署；仓库变量 `AUTO_DEPLOY=false` 可暂停自动部署。手动运行 Actions → CI / CD 并勾选 deploy 仍可部署。普通 PR 不进入部署阶段。

部署先在独立目录构建和校验插件、准备需要变更的依赖，然后停止 **AstrBot**，备份代码、插件、配置、数据库和原依赖版本，安装并做隔离验证，再启动并检查 WebUI 和 OneBot。NapCat 始终运行。失败时自动恢复代码、数据和本次修改的依赖；日志仅保存在服务器 `runtime/deploy/latest.log`，备份位于 `runtime/backups/deploy-*`。备份含私密内容，不上传 Actions。

运行版本记录在 `runtime/deployed-revision`；端到端执行结果可在 GitHub Actions 查看。首次部署已确认代码一致、四个托管插件加载、WebUI 与 OneBot 正常，NapCat 容器未重启。QQ 账号在线状态需在 NapCat 后台单独确认：登录失效时由账号持有人扫码，部署不会代替账号登录。API Key、QQ 登录态和聊天记录不进入 Git。
