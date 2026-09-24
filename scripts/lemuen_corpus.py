"""Validate Lemuen sources and build native import data; no network or bot access."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "knowledge/lemuen"
TOPICS = {
    "identity": ("01-identity.md", "身份与当前状态"),
    "family": ("02-family.md", "家庭与姐妹"),
    "relationships": ("03-relationships.md", "旧友与同僚"),
    "interests": ("04-interests.md", "生活兴趣"),
    "timeline": ("05-timeline.md", "时间线与状态变化"),
    "guide_ahead": ("06-guide-ahead.md", "吾导先路"),
    "hortus": ("07-hortus.md", "空想花庭"),
    "march_on": ("08-march-on.md", "众生行记"),
    "world": ("09-world.md", "世界背景与知情范围"),
    "behavior": ("10-behavior.md", "演绎分析与项目约定"),
}
KINDS = {
    "canon_fact": "原作事实摘要",
    "character_statement": "人物自述或观点",
    "official_presentation": "官方展示文本摘要",
    "interpretation": "编写者演绎分析",
    "project_adaptation": "项目互动约定",
}
STATUSES = {
    "full_text_reviewed": "全文语义文本已读（未核对音频或演出）",
    "character_related_records_reviewed": "选定角色相关记录已读",
    "relevant_dialogue_reviewed": "相关对话及邻近上下文已摘读",
    "excerpt_reviewed": "指定片段已核对",
    "fetched_not_reviewed": "已下载，内容待审读",
}
TABLE_FILES = {
    "H": "handbook-info.json",
    "V": "charword_table.json",
    "M": "uniequip_table.json",
    "K": "skin_table.json",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def line_span(locator):
    match = re.fullmatch(r"L(\d+)-L(\d+)", locator)
    require(match is not None, f"无效剧情行号：{locator}")
    return tuple(map(int, match.groups()))


def unique_index(records, label):
    indexed = {record["id"]: record for record in records}
    require(len(indexed) == len(records), f"{label} ID 重复")
    return indexed


def validate(bundle, cache):
    entries = unique_index(bundle["entries"]["entries"], "条目")
    sources = unique_index(bundle["sources"]["sources"], "来源")
    commit = bundle["sources"]["repository_commit"]
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "快照提交格式错误")
    tables = {}
    for sid, source in sources.items():
        path = source["resource_path"]
        expected = f"https://github.com/Kengxxiao/ArknightsGameData/blob/{commit}/{path}"
        require(source["url"] == expected, f"{sid} 未使用指定快照 URL")
        require(re.fullmatch(r"[0-9a-f]{64}", source["sha256"]), f"{sid} 哈希格式错误")
        require(source["review_status"] in STATUSES, f"{sid} 未知审读状态")
        if not cache:
            continue
        local = (
            cache / TABLE_FILES[sid] if sid in TABLE_FILES else cache / "stories" / Path(path).name
        )
        raw = local.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == source["sha256"], f"{sid} SHA256 不一致")
        if "git_blob_sha" in source:
            blob = f"blob {len(raw)}\0".encode() + raw
            require(
                hashlib.sha1(blob).hexdigest() == source["git_blob_sha"], f"{sid} Git blob 不一致"
            )
        if "line_count" in source:
            require(
                len(raw.decode("utf-8").splitlines()) == source["line_count"], f"{sid} 行数不一致"
            )
        else:
            tables[sid] = json.loads(raw)
    for eid, entry in entries.items():
        require(re.fullmatch(r"L\d{3}", eid), f"条目 ID 格式错误：{eid}")
        require(entry["topic"] in TOPICS and entry["kind"] in KINDS, f"{eid} 类型或主题错误")
        for key in ("title", "summary", "period", "world_state", "awareness"):
            require(isinstance(entry[key], str) and entry[key].strip(), f"{eid} 缺少 {key}")
        require(
            entry["entities"] and all(isinstance(x, str) for x in entry["entities"]),
            f"{eid} 缺少人物",
        )
        require(entry["evidence"], f"{eid} 缺少依据")
        for ref in entry["evidence"]:
            sid, locator = ref["source_id"], ref["locator"]
            require(sid in sources, f"{eid} 来源不存在：{sid}")
            require(
                sources[sid]["review_status"] != "fetched_not_reviewed", f"{eid} 引用了未审读来源"
            )
            if sid not in TABLE_FILES:
                start, end = line_span(locator)
                require(
                    1 <= start <= end <= sources[sid]["line_count"], f"{eid} 行号越界：{locator}"
                )
            elif cache and sid == "V":
                require(locator in tables[sid]["charWords"], f"{eid} 台词键不存在：{locator}")
            elif cache:
                record_id, field = locator.split(" / ")
                if sid == "H":
                    records = tables[sid]["handbookDict"][record_id]["storyTextAudio"]
                    require(field in {s["storyTitle"] for s in records}, f"{eid} 档案段落不存在")
                else:
                    table = tables[sid]["equipDict" if sid == "M" else "charSkins"]
                    require(field in table[record_id], f"{eid} 资源字段不存在")
            if entry["kind"] == "project_adaptation":
                require(
                    ref.get("role") == "characterization_reference_only",
                    f"{eid} 项目约定混入原作依据",
                )
        for referenced in re.findall(r"\bL\d{3}\b", entry.get("notes", "")):
            require(referenced in entries, f"{eid} 说明中的条目不存在：{referenced}")
    seen_aliases = {}
    for name, record in bundle["aliases"].items():
        for alias in record["aliases"]:
            key = alias.casefold()
            require(key not in seen_aliases or seen_aliases[key] == name, f"别名冲突：{alias}")
            seen_aliases[key] = name
        require(
            record["entry_ids"] and set(record["entry_ids"]) <= entries.keys(),
            f"{name} 引用条目错误",
        )
    guide = bundle["voice-guide"]
    patterns = unique_index(guide["patterns"], "谈话方式")
    require(guide["editorial_rules"], "谈话方式缺少表达约定")
    require(isinstance(guide["tone"], str) and guide["tone"].strip(), "缺少语气说明")
    for pattern in patterns.values():
        for key in ("title", "when", "observation", "direction", "scope"):
            require(
                isinstance(pattern[key], str) and pattern[key].strip(),
                f"{pattern['id']} 缺少 {key}",
            )
        require(pattern["evidence"], f"{pattern['id']} 缺少原作依据")
        for ref in pattern["evidence"]:
            sid, locator = ref["source_id"], ref["locator"]
            require(
                sid in sources and sources[sid]["review_status"] != "fetched_not_reviewed",
                f"{pattern['id']} 未审读来源",
            )
            if sid not in TABLE_FILES:
                start, end = line_span(locator)
                require(
                    1 <= start <= end <= sources[sid]["line_count"], f"{pattern['id']} 剧情行号越界"
                )
            elif cache and sid == "V":
                require(locator in tables["V"]["charWords"], f"{pattern['id']} 台词不存在")
            elif sid == "M":
                if cache:
                    record_id, field = locator.split(" / ")
                    require(
                        field in tables["M"]["equipDict"][record_id],
                        f"{pattern['id']} 模组字段不存在",
                    )
            elif sid != "V":
                require(False, f"{pattern['id']} 未支持的表达依据类型")
        require(
            set(pattern.get("activation_entry_ids", [])) <= entries.keys(),
            f"{pattern['id']} 触发条目不存在",
        )
        excerpt = pattern["tone_excerpt"]
        require(excerpt["text"].strip(), f"{pattern['id']} 语气短句为空")
        require(
            any(
                ref["source_id"] == excerpt["source_id"] and ref["locator"] == excerpt["locator"]
                for ref in pattern["evidence"]
            ),
            f"{pattern['id']} 短句来源未列入依据",
        )
        if cache:
            if excerpt["source_id"] == "V":
                original = tables["V"]["charWords"][excerpt["locator"]]["voiceText"]
            elif excerpt["source_id"] == "M":
                record_id, field = excerpt["locator"].split(" / ")
                original = tables["M"]["equipDict"][record_id][field]
            else:
                source = sources[excerpt["source_id"]]
                local = cache / "stories" / Path(source["resource_path"]).name
                start, end = line_span(excerpt["locator"])
                original = "\n".join(local.read_text().splitlines()[start - 1 : end])
            require(excerpt["text"] in original, f"{pattern['id']} 短句与原文不符")
    require(guide["editorial_examples_notice"], "缺少手写样例与事实的区别说明")
    examples = unique_index(guide["editorial_examples"], "表达样例")
    for example in examples.values():
        for key in ("purpose", "user", "assistant", "basis"):
            require(
                isinstance(example[key], str) and example[key].strip(),
                f"{example['id']} 缺少 {key}",
            )
        require(
            set(example.get("activation_entry_ids", [])) <= entries.keys(),
            f"{example['id']} 触发条目不存在",
        )
    for record in guide["patterns"] + guide["editorial_examples"]:
        if "activation_terms" in record:
            require(
                record["activation_entry_ids"]
                and record["activation_terms"]
                and all(isinstance(t, str) and t.strip() for t in record["activation_terms"]),
                f"{record['id']} 话题触发词为空或不合法",
            )
    return entries, sources


def reference(ref, sources):
    source = sources[ref["source_id"]]
    url = source["url"]
    if ref["source_id"] not in TABLE_FILES:
        start, end = line_span(ref["locator"])
        url += f"#L{start}-L{end}"
    result = f"[{ref['source_id']} · {source['title']}]({url})，定位：`{ref['locator']}`"
    if ref.get("role") == "characterization_reference_only":
        result += "（仅作演绎参考；互动约定来自用户需求）"
    return result


def import_payload(bundle, entries, sources):
    documents = []
    for topic, (_, title) in TOPICS.items():
        chunks = []
        for entry in entries.values():
            if entry["topic"] != topic or entry["kind"] in {
                "interpretation",
                "project_adaptation",
            }:
                continue
            aliases = sorted(
                {
                    alias
                    for name, record in bundle["aliases"].items()
                    if name in entry["entities"] or entry["id"] in record["entry_ids"]
                    for alias in record["aliases"]
                }
            )
            lines = [
                f"# {entry['id']} · 蕾缪安 · {entry['title']}",
                "",
                f"类型：{KINDS[entry['kind']]}。",
                f"人物与检索别名：{'、'.join(aliases or entry['entities'])}。",
                f"时期：{entry['period']}。",
                f"现实层／语境：{entry['world_state']}。",
                f"知情范围：{entry['awareness']}。",
                "",
                entry["summary"],
                "",
            ]
            if entry.get("notes"):
                lines += [f"收录说明：{entry['notes']}", ""]
            lines += ["依据：", ""]
            lines += [f"- {reference(ref, sources)}" for ref in entry["evidence"]]
            chunks.append("\n".join(lines))
        if chunks:
            documents.append(
                {"file_name": f"蕾缪安-{title}.md", "file_type": "md", "chunks": chunks}
            )
    import_payload = {
        "batch_size": 10,
        "tasks_limit": 1,
        "max_retries": 2,
        "documents": documents,
    }
    return import_payload


def read_bundle():
    return {
        name: load(ROOT / f"{name}.json")
        for name in ("entries", "sources", "aliases", "voice-guide")
    }
