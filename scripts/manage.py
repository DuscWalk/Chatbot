#!/usr/bin/env python3
"""Local and Ubuntu service management. Never sends messages to QQ."""

import argparse
import fcntl
import importlib.metadata
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
DATA = ROOT / "data"
NAPCAT = RUNTIME / "napcat" / "config"
SERVICE = "qqbots2-astrbot.service"
ASTRBOT_VERSION = "4.27.4"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".qqbot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def compose(args, **kwargs):
    if not shutil.which("docker"):
        raise RuntimeError("未安装 Docker，请按 README 的 Ubuntu 步骤安装。")
    env = dict(
        os.environ,
        QQBOT_ROOT=str(ROOT),
        QQBOT_UID=str(os.getuid()),
        QQBOT_GID=str(os.getgid()),
    )
    return run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), *args],
        env=env,
        **kwargs,
    )


def systemctl(*args, **kwargs):
    return run(["systemctl", "--user", *args], **kwargs)


def setup():
    try:
        installed = importlib.metadata.version("AstrBot")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed != ASTRBOT_VERSION:
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    for directory in (DATA, NAPCAT, RUNTIME / "napcat/qq", RUNTIME / "napcat/plugins"):
        directory.mkdir(parents=True, exist_ok=True)
    config_file = DATA / "cmd_config.json"
    credentials_file = RUNTIME / "credentials.json"
    if not credentials_file.exists():
        if config_file.exists() or (NAPCAT / "webui.json").exists():
            raise RuntimeError(
                "已有配置但 runtime/credentials.json 缺失；请恢复该文件，避免重置已有凭据。"
            )
        write_json(
            credentials_file,
            {
                "astrbot_username": "astrbot",
                "astrbot_initial_password": "Ab9" + secrets.token_urlsafe(24),
                "napcat_webui_token": secrets.token_urlsafe(32),
                "onebot_token": secrets.token_urlsafe(32),
            },
        )
    credentials_file.chmod(0o600)
    credentials = read_json(credentials_file)
    pending = RUNTIME / "setup.pending"
    if not config_file.exists():
        pending.touch(mode=0o600)
    fresh = pending.exists()
    if fresh or not (ROOT / ".astrbot").exists():
        env = dict(
            os.environ,
            ASTRBOT_ROOT=str(ROOT),
            ASTRBOT_DASHBOARD_INITIAL_PASSWORD=credentials["astrbot_initial_password"],
        )
        run([str(Path(sys.executable).parent / "astrbot"), "init", "--yes"], env=env)
    if fresh:
        config = read_json(config_file)
        config["dashboard"].update(host="127.0.0.1", port=6185)
        config["platform"] = [
            {
                "id": "napcat",
                "type": "aiocqhttp",
                "enable": True,
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": 6199,
                "ws_reverse_token": credentials["onebot_token"],
            }
        ]
        config["platform_settings"].update(
            enable_id_white_list=False,
            ignore_bot_self_message=True,
            ignore_at_all=True,
            unique_session=True,
            path_mapping=[f"/app/.config/QQ:{ROOT}/runtime/napcat/qq"],
        )
        config["timezone"] = "Asia/Shanghai"
        write_json(config_file, config)
    # Relocate only the path mapping owned by this project after server migration.
    installation_file = RUNTIME / "installation.json"
    if installation_file.exists():
        previous_root = read_json(installation_file)["project_root"]
        if previous_root != str(ROOT):
            config = read_json(config_file)
            old_mapping = f"/app/.config/QQ:{previous_root}/runtime/napcat/qq"
            mappings = config["platform_settings"].get("path_mapping", [])
            config["platform_settings"]["path_mapping"] = [
                f"/app/.config/QQ:{ROOT}/runtime/napcat/qq" if m == old_mapping else m
                for m in mappings
            ]
            write_json(config_file, config)
    write_json(installation_file, {"project_root": str(ROOT)})
    # The image copies bundled defaults over config/ if napcat.json is missing.
    # Create it first so the generated OneBot and WebUI configs survive first boot.
    defaults = {
        "napcat.json": {},
        "webui.json": {
            "host": "127.0.0.1",
            "port": 6099,
            "token": credentials["napcat_webui_token"],
            "loginRate": 5,
            "autoLoginAccount": "",
            "disableWebUI": False,
        },
        "onebot11.json": {
            "network": {
                "httpServers": [],
                "httpSseServers": [],
                "httpClients": [],
                "websocketServers": [],
                "plugins": [],
                "websocketClients": [
                    {
                        "name": "astrbot",
                        "enable": True,
                        "url": "ws://127.0.0.1:6199/ws",
                        "messagePostFormat": "array",
                        "reportSelfMessage": False,
                        "reconnectInterval": 5000,
                        "heartInterval": 30000,
                        "token": credentials["onebot_token"],
                        "debug": False,
                    }
                ],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        },
    }
    for name, value in defaults.items():
        path = NAPCAT / name
        if not path.exists():
            write_json(path, value)
    pending.unlink(missing_ok=True)
    print(
        "初始化完成，已有 WebUI 配置与凭据会保留。用 ./bot up 启动，./bot credentials 查看登录信息。"
    )


def require_setup():
    if (
        (RUNTIME / "setup.pending").exists()
        or not (ROOT / ".astrbot").exists()
        or not (NAPCAT / "webui.json").exists()
    ):
        raise RuntimeError("请先运行 ./bot setup。")


def unit_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_unit():
    unit = Path.home() / ".config/systemd/user" / SERVICE
    marker = f"# Project: {ROOT}"
    if unit.exists() and marker not in unit.read_text():
        raise RuntimeError(f"{unit} 属于另一个项目，请先停用并移走旧服务文件。")
    executable = Path(sys.executable).parent / "astrbot"
    content = f"""{marker}
[Unit]
Description=AstrBot QQ bot (astrbot-wsl)
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={str(ROOT).replace("%", "%%")}
ExecStart={unit_quote(executable)} run
Environment={unit_quote("PATH=" + str(executable.parent) + ":/usr/local/bin:/usr/bin:/bin")}
Environment={unit_quote("ASTRBOT_ROOT=" + str(ROOT))}
Environment=PYTHONUNBUFFERED=1
Environment=DASHBOARD_HOST=127.0.0.1
UMask=0077
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    systemctl("daemon-reload")
    return unit


def urls():
    config = read_json(DATA / "cmd_config.json")
    webui = read_json(NAPCAT / "webui.json")
    return {
        "AstrBot": f"http://127.0.0.1:{config['dashboard']['port']}/",
        "NapCat": f"http://127.0.0.1:{webui['port']}/webui/",
    }


def http_ready(url):
    try:
        with HTTP.open(url, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def check():
    require_setup()
    config = read_json(DATA / "cmd_config.json")
    webui = read_json(NAPCAT / "webui.json")
    platform = next((p for p in config["platform"] if p["id"] == "napcat"), None)
    failures = []
    if not platform or not platform["enable"]:
        failures.append("AstrBot 的 napcat 平台未启用")
    else:
        if platform["ws_reverse_host"] != "127.0.0.1":
            failures.append("OneBot 接口未绑定 127.0.0.1")
        expected_url = f"ws://127.0.0.1:{platform['ws_reverse_port']}/ws"
        # Check both the default and account-specific configs created after login.
        for path in sorted(NAPCAT.glob("onebot11*.json")):
            clients = read_json(path).get("network", {}).get("websocketClients", [])
            matched = any(
                c.get("enable")
                and c.get("url") == expected_url
                and c.get("token") == platform.get("ws_reverse_token")
                and c.get("token")
                for c in clients
            )
            if not matched:
                failures.append(f"{path.name} 的 OneBot URL/令牌与 AstrBot 不一致")
    if config["dashboard"]["host"] != "127.0.0.1" or webui["host"] != "127.0.0.1":
        failures.append("管理后台未绑定 127.0.0.1")
    for name, url in urls().items():
        ready = http_ready(url)
        print(f"{name}: {'HTTP 200' if ready else '未就绪'}  {url}")
        if not ready:
            failures.append(f"{name} 页面不可访问")
    if failures:
        for problem in failures:
            print(f"检查失败: {problem}", file=sys.stderr)
        return 1
    print("后台及 OneBot 配置检查通过。QQ 登录状态和模型回复需在扫码、配置模型后验证。")
    return 0


def credentials():
    require_setup()
    saved = read_json(RUNTIME / "credentials.json")
    config = read_json(DATA / "cmd_config.json")
    webui = read_json(NAPCAT / "webui.json")
    print("AstrBot 用户名:", config["dashboard"]["username"])
    print("AstrBot 初始密码:", saved["astrbot_initial_password"])
    print("（如已在 WebUI 改密，请使用新密码。）")
    print("NapCat WebUI Token:", webui["token"])
    print("OneBot 令牌已自动配对，无需填写 QQ 密码。")


def main():
    os.chdir(ROOT)
    os.umask(0o077)
    if os.getuid() == 0:
        raise RuntimeError("请以普通用户运行，勿使用 sudo ./bot。")
    parser = argparse.ArgumentParser(description="AstrBot + NapCat 管理命令")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in {
        "setup": "初始化（保留已有配置）",
        "up": "启动并启用服务",
        "down": "停止服务（保留全部数据）",
        "restart": "重启两个组件",
        "status": "查看进程及页面状态",
        "check": "检查页面和 OneBot 配置",
        "credentials": "查看初始后台登录信息",
    }.items():
        commands.add_parser(command, help=help_text)
    log_parser = commands.add_parser("logs", help="查看日志")
    log_parser.add_argument(
        "component", choices=["astrbot", "napcat"], default="astrbot", nargs="?"
    )
    log_parser.add_argument("-f", "--follow", action="store_true")
    compose_parser = commands.add_parser("compose", help="运行 Docker Compose，例如 compose ps")
    compose_parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    # Serialize service/config changes made through this launcher.
    with (RUNTIME / "manage.lock").open("w") as lock:
        if args.command in {"setup", "up", "down", "restart"}:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == "setup":
            setup()
        elif args.command == "credentials":
            credentials()
        elif args.command == "compose":
            require_setup()
            compose(args.args)
        elif args.command == "up":
            require_setup()
            compose(["config", "--quiet"])
            install_unit()
            systemctl("enable", "--now", SERVICE)
            compose(["up", "-d"])
            for name, url in urls().items():
                print(f"{name}: {url}")
            print("首次启动约需 30–90 秒，稍后运行 ./bot check。")
        elif args.command == "down":
            systemctl("disable", "--now", SERVICE)
            compose(["down"])
        elif args.command == "restart":
            require_setup()
            systemctl("restart", SERVICE)
            compose(["restart", "napcat"])
        elif args.command == "status":
            require_setup()
            print(
                subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        SERVICE,
                        "--property=ActiveState,SubState,MainPID",
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
            )
            compose(["ps"])
            for name, url in urls().items():
                print(f"{name}: {'可访问' if http_ready(url) else '未就绪'}  {url}")
        elif args.command == "check":
            return check()
        elif args.command == "logs":
            if args.component == "astrbot":
                command = [
                    "journalctl",
                    "--user",
                    "-u",
                    SERVICE,
                    "-n",
                    "80",
                    "--no-pager",
                ]
                if args.follow:
                    command.append("-f")
                run(command)
            else:
                compose(["logs", "--tail", "80", *(["-f"] if args.follow else []), "napcat"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"错误: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
