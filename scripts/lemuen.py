"""Maintain, package and smoke-test Lemuen without enabling a QQ session."""

import argparse
import base64
import json
import shlex
import subprocess
import zipfile
from pathlib import Path

from lemuen_corpus import ROOT, import_payload, read_bundle, validate

PROJECT = Path(__file__).resolve().parents[1]

BUILD = PROJECT / "runtime/lemuen/build"
PLUGIN = PROJECT / "plugins/astrbot_plugin_lemuen"
FIXTURES = PROJECT / "tests/fixtures/lemuen.json"

# Merge these keys into the private profile, preserving unrelated user settings.
PRIVATE_CHAT_SETTINGS = {
    "platform_settings": {
        "segmented_reply": {
            "enable": True,
            "only_llm_result": True,
            "interval_method": "random",
            "interval": "0.8,1.8",
            "words_count_threshold": 600,
            "split_mode": "regex",
            "regex": r"\A(?=.*(?:```|~~~)).+\Z|\S.*?(?=\n[ \t]*\n|\Z)",
            "content_cleanup_rule": "",
        }
    },
    "provider_settings": {"streaming_response": False},
}


def check(cache=None):
    bundle = read_bundle()
    entries, sources = validate(bundle, cache)
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    for case in cases["retrieval"]:
        if not set(case["expected_entry_ids"]) <= entries.keys():
            raise ValueError(f"Unknown reference in {case['id']}")
    print(f"资料有效：{len(entries)} 条条目，{len(sources)} 个来源。")
    return bundle, entries, sources


def build(cache=None):
    bundle, entries, sources = check(cache)
    BUILD.mkdir(parents=True, exist_ok=True)
    payload = import_payload(bundle, entries, sources)
    (BUILD / "astrbot-import.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (BUILD / "persona.md").write_text((ROOT / "persona.md").read_text(encoding="utf-8"))
    with zipfile.ZipFile(BUILD / "astrbot_plugin_lemuen.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in [
            "main.py",
            "render.py",
            "proactive.py",
            "metadata.yaml",
            "_conf_schema.json",
            "README.md",
        ]:
            archive.write(PLUGIN / name, f"astrbot_plugin_lemuen/{name}")
        archive.write(ROOT / "voice-guide.json", "astrbot_plugin_lemuen/voice-guide.json")
    (BUILD / "private-chat-settings.json").write_text(
        json.dumps(PRIVATE_CHAT_SETTINGS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    chunks = sum(len(d["chunks"]) for d in payload["documents"])
    print(
        f"已生成：{BUILD.relative_to(PROJECT)}（人格、{chunks} 个原作块、插件 ZIP、私聊分段设置）。"
    )
    return payload


def smoke(args):
    if not args.live:
        raise ValueError("联调会调用嵌入与聊天 API；请显式添加 --live。")
    from dotenv import dotenv_values

    payload = build()
    values = dotenv_values(PROJECT / ".env", encoding="utf-8-sig")
    if not values.get("DASHSCOPE_API_KEY"):
        raise ValueError(".env 缺少 DASHSCOPE_API_KEY。")
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    selected = set(args.cases.split(","))
    if selected - {case["id"] for case in cases["dialogue"]}:
        raise ValueError("未知对话场景 ID。")
    request = {
        "project": args.remote_project,
        "embedding": {
            "id": "lemuen-embedding",
            "type": "openai_embedding",
            "enable": True,
            "embedding_api_key": values["DASHSCOPE_API_KEY"],
            "embedding_api_base": values.get("DASHSCOPE_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "embedding_model": values.get("DASHSCOPE_EMBEDDING_MODEL") or "qwen3.7-text-embedding",
            "embedding_dimensions": int(values.get("DASHSCOPE_EMBEDDING_DIMENSIONS") or 1024),
            "embedding_dimensions_mode": "always",
            "timeout": 12,
        },
        "plugin_zip": base64.b64encode((BUILD / "astrbot_plugin_lemuen.zip").read_bytes()).decode(),
        "persona": (BUILD / "persona.md").read_text(encoding="utf-8"),
        "knowledge": payload,
        "chat_settings": PRIVATE_CHAT_SETTINGS,
        "retrieval_cases": cases["retrieval"],
        "dialogue_cases": [c for c in cases["dialogue"] if c["id"] in selected],
    }
    worker = (PROJECT / "scripts/lemuen_smoke.py").read_text(encoding="utf-8")
    command = shlex.join([args.remote_python, "-c", worker])
    # SSH stdout is parsed JSON only. Worker and SSH errors never echo request credentials.
    process = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host, command],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if process.returncode:
        raise RuntimeError("隔离联调进程失败；SSH 输出已省略。")
    result = json.loads(process.stdout)
    if result.get("error"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    target = PROJECT / "runtime/lemuen/latest-smoke.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 蕾缪安接入联调（仅保留最近一次）", "", result["scope"], ""]
    lines += [f"模型：{result['model']}；原配置未改动：{result['production_unchanged']}。", ""]
    for reply in result["replies"]:
        lines += [
            f"## {reply['case']} · 第 {reply['turn']} 轮",
            "",
            reply["user"],
            "",
            reply["reply"],
            "",
            f"检索：{', '.join(reply['entry_ids'])}；模拟发送：{len(reply['messages'])} 条。",
            "",
        ]
    target.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(f"完成：{len(result['replies'])} 次原生流程回复，结果见 {target.relative_to(PROJECT)}。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ["check", "build"]:
        sub.add_parser(name).add_argument("--cache-dir", type=Path)
    preview = sub.add_parser("smoke")
    preview.add_argument("--live", action="store_true")
    preview.add_argument("--host", default="njuse")
    preview.add_argument("--remote-project", default="/home/ubuntu/qqBots2.0")
    preview.add_argument(
        "--remote-python", default="/home/ubuntu/miniforge3/envs/astrbot-wsl/bin/python"
    )
    preview.add_argument("--cases", default="D01,D07,R03,X06,G01")
    args = parser.parse_args()
    try:
        if args.action == "check":
            check(args.cache_dir)
        elif args.action == "build":
            build(args.cache_dir)
        else:
            smoke(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"未完成：{error}\n")


if __name__ == "__main__":
    main()
