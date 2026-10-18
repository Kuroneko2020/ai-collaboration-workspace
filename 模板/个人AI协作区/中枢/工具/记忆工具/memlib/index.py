"""派生索引与记忆.md 生成链接区。

``运行/index.json`` 是派生视图：源清单（相对路径 + SHA-256）、工具/schema 版本、
生成时间与提取索引。查询始终以权威原文为准；索引缺失/损坏/陈旧只影响报告，
不影响事实。``reindex`` 会同步刷新 ``记忆.md`` 的成对标记区域，人工区保持原样。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import INDEX_SCHEMA_VERSION, TOOL_VERSION
from .config import MemoryConfig
from .errors import SchemaError
from .fsutil import atomic_write_bytes
from .jsonutil import loads_strict, read_json_strict
from .schema import ParsedEntry, utc_now, utc_now_str
from .store import (
    BEGIN_MARKER,
    END_MARKER,
    Paths,
    StoreSnapshot,
    is_current,
    review_due,
    status_label,
)

FreshnessStatus = str  # fresh / missing / corrupt / stale


def _entry_extract(entry: ParsedEntry) -> dict[str, Any]:
    metadata = entry.metadata
    return {
        "id": entry.id,
        "title": entry.title,
        "kind": entry.kind,
        "status": entry.status,
        "revision": entry.revision,
        "pinned": metadata["pinned"],
        "scope": dict(metadata["scope"]),
        "tags": list(metadata["tags"]),
        "valid_from": metadata["valid_from"],
        "valid_until": metadata["valid_until"],
        "review_after": metadata["review_after"],
        "sha256": entry.sha256,
        "path": entry.origin,
    }


def build_index_doc(paths: Paths, snapshot: StoreSnapshot, config: MemoryConfig) -> dict[str, Any]:
    entries = snapshot.sorted_entries()
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "workspace_id": config.workspace_id,
        "generated_at": utc_now_str(),
        "entries_dir_exists": snapshot.entries_dir_exists,
        "sources": [
            {
                "path": entry.origin,
                "id": entry.id,
                "sha256": entry.sha256,
                "size": entry.raw_size,
            }
            for entry in entries
        ],
        "entries": [_entry_extract(entry) for entry in entries],
        "store_errors": [
            {"path": error.get("path"), "message": error.get("message")}
            for error in snapshot.errors
        ],
        "counts": {"entries": len(entries), "store_errors": len(snapshot.errors)},
    }


def _load_index(paths: Paths) -> tuple[dict[str, Any] | None, FreshnessStatus, str]:
    index_file = paths.index_file
    if not index_file.is_file():
        return None, "missing", f"派生索引不存在: {paths.rel(index_file)}"
    try:
        doc = read_json_strict(index_file, str(index_file))
    except SchemaError as exc:
        return None, "corrupt", f"派生索引无法解析: {exc}"
    if not isinstance(doc, dict):
        return None, "corrupt", "派生索引必须是 JSON 对象"
    if doc.get("schema_version") != INDEX_SCHEMA_VERSION:
        return None, "corrupt", f"派生索引 schema_version 不受支持: {doc.get('schema_version')!r}"
    for field in ("sources", "entries", "tool_version", "workspace_id", "generated_at"):
        if field not in doc:
            return None, "corrupt", f"派生索引缺少字段: {field}"
    if not isinstance(doc["sources"], list) or not isinstance(doc["entries"], list):
        return None, "corrupt", "派生索引 sources/entries 必须是数组"
    return doc, "ok", ""


def _actual_source_map(snapshot: StoreSnapshot) -> dict[str, str]:
    return {entry.origin: entry.sha256 for entry in snapshot.sorted_entries()}


def _recorded_source_map(doc: dict[str, Any]) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for item in doc.get("sources", []):
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        sha = item.get("sha256")
        if not isinstance(path, str) or not isinstance(sha, str):
            return None
        result[path] = sha
    return result


def index_state(paths: Paths, snapshot: StoreSnapshot, config: MemoryConfig) -> dict[str, Any]:
    doc, status, reason = _load_index(paths)
    if status != "ok":
        return {"status": status, "reason": reason, "generated_at": None}
    assert doc is not None
    if doc.get("tool_version") != TOOL_VERSION:
        return {
            "status": "stale",
            "reason": f"索引由工具版本 {doc.get('tool_version')!r} 生成，当前 {TOOL_VERSION}",
            "generated_at": doc.get("generated_at"),
        }
    if doc.get("workspace_id") != config.workspace_id:
        return {
            "status": "stale",
            "reason": "索引 workspace_id 与配置不一致",
            "generated_at": doc.get("generated_at"),
        }
    recorded = _recorded_source_map(doc)
    if recorded is None:
        return {"status": "corrupt", "reason": "索引 sources 项结构不合法", "generated_at": doc.get("generated_at")}
    actual = _actual_source_map(snapshot)
    if recorded != actual:
        missing = sorted(set(actual) - set(recorded))
        extra = sorted(set(recorded) - set(actual))
        changed = sorted(path for path in set(actual) & set(recorded) if actual[path] != recorded[path])
        return {
            "status": "stale",
            "reason": f"索引源清单与原文不一致（新增 {len(missing)}，删除 {len(extra)}，变更 {len(changed)}）",
            "missing": missing,
            "extra": extra,
            "changed": changed,
            "generated_at": doc.get("generated_at"),
        }
    recorded_errors = [
        {"path": item.get("path"), "message": item.get("message")}
        for item in doc.get("store_errors", [])
        if isinstance(item, dict)
    ]
    current_errors = [
        {"path": error.get("path"), "message": error.get("message")} for error in snapshot.errors
    ]
    if recorded_errors != current_errors:
        return {
            "status": "stale",
            "reason": "索引记录的解析错误状态与当前不同",
            "generated_at": doc.get("generated_at"),
        }
    return {"status": "fresh", "reason": "派生索引与原文一致", "generated_at": doc.get("generated_at")}


def write_index(paths: Paths, doc: dict[str, Any]) -> None:
    atomic_write_bytes(paths.index_file, (__import__("json").dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- 记忆.md 链接区
def _current_nav_entries(snapshot: StoreSnapshot, at: datetime) -> list[ParsedEntry]:
    selected: list[ParsedEntry] = []
    for entry in snapshot.sorted_entries():
        if entry.status != "active":
            continue
        if not is_current(entry, at):
            continue
        selected.append(entry)
    selected.sort(key=lambda entry: (not entry.metadata["pinned"], entry.id))
    return selected


def generate_links_content(paths: Paths, snapshot: StoreSnapshot, at: datetime) -> str:
    lines: list[str] = []
    for entry in _current_nav_entries(snapshot, at):
        scope = entry.scope
        scenes = ",".join(scope["scenes"]) if scope["scenes"] else "通用"
        pinned = "；pinned" if entry.metadata["pinned"] else ""
        lines.append(
            f"- [{entry.title}](条目/{entry.id}.md) — `{entry.id}`；范围：{scope['level']}:{scope['target']}"
            f"；场景：{scenes}{pinned}；权威路径：{entry.origin}"
        )
    if not lines:
        return "（当前没有进入核心导航的当前有效条目）"
    return "\n".join(lines)


def _marker_positions(text: str) -> tuple[int, int] | None:
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin < 0 or end < 0 or end < begin:
        return None
    if text.find(BEGIN_MARKER, begin + 1) >= 0 or text.find(END_MARKER, end + 1) >= 0:
        return None
    return begin, end


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _compose_region(text: str, snapshot: StoreSnapshot, at: datetime, paths: Paths) -> str:
    positions = _marker_positions(text)
    assert positions is not None
    begin, end = positions
    prefix = text[:begin]
    suffix = text[end + len(END_MARKER) :]
    newline = _newline_for(text)
    content = generate_links_content(paths, snapshot, at)
    block = f"{BEGIN_MARKER}{newline}{content}{newline}{END_MARKER}"
    return prefix + block + suffix


def links_state(paths: Paths, snapshot: StoreSnapshot, at: datetime) -> dict[str, Any]:
    memory_md = paths.memory_md
    if not memory_md.is_file():
        return {"status": "memory_md_missing", "reason": f"记忆入口不存在: {paths.rel(memory_md)}"}
    try:
        text = memory_md.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return {"status": "memory_md_unreadable", "reason": f"记忆入口无法读取: {exc}"}
    if _marker_positions(text) is None:
        return {
            "status": "markers_missing",
            "reason": (
                f"记忆入口缺少唯一的成对生成标记（{BEGIN_MARKER} / {END_MARKER}）；"
                "未修改该文件，需要主会话先放置标记后再运行 reindex"
            ),
        }
    desired = _compose_region(text, snapshot, at, paths)
    if desired == text:
        return {"status": "fresh", "reason": "生成链接区与当前有效条目一致"}
    return {"status": "stale", "reason": "生成链接区内容陈旧，需要 reindex 刷新"}


def refresh_memory_links(paths: Paths, snapshot: StoreSnapshot, at: datetime | None = None) -> dict[str, Any]:
    at = at or utc_now()
    memory_md = paths.memory_md
    if not memory_md.is_file():
        return {
            "status": "memory_md_missing",
            "written": False,
            "reason": f"记忆入口不存在，跳过生成链接区: {paths.rel(memory_md)}",
        }
    raw = memory_md.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return {
            "status": "memory_md_unreadable",
            "written": False,
            "reason": f"记忆入口不是合法 UTF-8，未修改: {exc}",
        }
    if _marker_positions(text) is None:
        return {
            "status": "markers_missing",
            "written": False,
            "reason": (
                f"记忆入口缺少唯一的成对生成标记（{BEGIN_MARKER} / {END_MARKER}）；"
                "未修改该文件，需要主会话先放置标记后再运行 reindex"
            ),
        }
    desired = _compose_region(text, snapshot, at, paths)
    if desired == text:
        return {"status": "fresh", "written": False, "reason": "生成链接区已是最新"}
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    data = desired.encode("utf-8")
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    atomic_write_bytes(memory_md, data)
    return {"status": "updated", "written": True, "reason": "已刷新生成链接区"}


# ---------------------------------------------------------------- 派生同步
def sync_derived(
    paths: Paths,
    snapshot: StoreSnapshot,
    config: MemoryConfig,
    *,
    at: datetime | None = None,
    refresh_links: bool = True,
) -> dict[str, Any]:
    """重建派生索引（如陈旧）并刷新链接区；返回状态报告。"""

    at = at or utc_now()
    state = index_state(paths, snapshot, config)
    index_written = False
    if state["status"] != "fresh":
        write_index(paths, build_index_doc(paths, snapshot, config))
        index_written = True
        state = {"status": "fresh", "reason": "已重建派生索引", "generated_at": utc_now_str()}
    link_result: dict[str, Any] = {"status": "skipped", "written": False, "reason": "未请求刷新链接区"}
    if refresh_links:
        link_result = refresh_memory_links(paths, snapshot, at)
    return {
        "index_status": state["status"],
        "index_written": index_written,
        "index_reason": state.get("reason", ""),
        "index_generated_at": state.get("generated_at"),
        "links_status": link_result["status"],
        "links_written": bool(link_result.get("written")),
        "links_reason": link_result.get("reason", ""),
    }


def check_derived(paths: Paths, snapshot: StoreSnapshot, config: MemoryConfig, *, at: datetime | None = None) -> dict[str, Any]:
    at = at or utc_now()
    state = index_state(paths, snapshot, config)
    links = links_state(paths, snapshot, at)
    fresh = state["status"] == "fresh" and links["status"] == "fresh"
    return {"fresh": fresh, "index": state, "links": links}


__all__ = [
    "build_index_doc",
    "index_state",
    "write_index",
    "generate_links_content",
    "links_state",
    "refresh_memory_links",
    "sync_derived",
    "check_derived",
]
