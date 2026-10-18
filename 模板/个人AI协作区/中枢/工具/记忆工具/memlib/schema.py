"""记忆数据格式：严格解析、验证与规范化序列化。

文件结构为独立的 ``---`` 行包围的**纯 JSON** front matter（不是 YAML），
正文必须有一个 H1 与 ``## 结论`` 段落，candidate 还必须有 ``## 待澄清问题``。
新文件写出 UTF-8 无 BOM、LF；读取兼容 BOM 与 CRLF。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import SCHEMA_VERSION
from .errors import SchemaError
from .jsonutil import loads_strict

# ---------------------------------------------------------------- 常量
ID_RE = re.compile(r"^mem-[a-z0-9]+(?:-[a-z0-9]+)*$")
ID_MAX = 96
SUBJECT_KEY_RE = re.compile(r"^[^\s/\\\x00]{1,200}$")
TASK_ID_RE = re.compile(r"^[^\s/\\\x00]{1,200}$")

KINDS = ("preference", "fact", "episode", "lesson", "pointer")
STATUSES = ("candidate", "active", "superseded", "retired", "rejected")
LEVELS = ("workspace", "topic", "project", "task")
LEVEL_RANK = {"workspace": 0, "topic": 1, "project": 2, "task": 3}

SOURCE_KINDS = (
    "user_explicit",
    "user_quote_in_task",
    "user_confirmed",
    "artifact_verified",
    "agent_observation",
    "agent_inference",
)
USER_SOURCE_KINDS = frozenset(("user_explicit", "user_quote_in_task", "user_confirmed"))

FIELD_ORDER = (
    "schema_version",
    "id",
    "revision",
    "kind",
    "subject_key",
    "status",
    "scope",
    "pinned",
    "tags",
    "sources",
    "created_at",
    "updated_at",
    "last_verified_at",
    "valid_from",
    "valid_until",
    "review_after",
    "end_condition",
    "superseded_by",
)
TOOL_MANAGED_FIELDS = ("schema_version", "revision", "created_at", "updated_at", "last_verified_at")
WRITABLE_FIELDS = tuple(field for field in FIELD_ORDER if field not in TOOL_MANAGED_FIELDS)
READ_REQUIRED_FIELDS = (
    "schema_version",
    "id",
    "revision",
    "kind",
    "subject_key",
    "status",
    "scope",
    "pinned",
    "tags",
    "sources",
    "created_at",
    "updated_at",
)
READ_OPTIONAL_FIELDS = (
    "last_verified_at",
    "valid_from",
    "valid_until",
    "review_after",
    "end_condition",
    "superseded_by",
)
WRITE_REQUIRED_FIELDS = ("kind", "subject_key", "status", "scope", "sources")

MAX_BODY_CHARS = 400_000
MAX_ENTRY_BYTES = 2 * 1024 * 1024
TITLE_MAX = 300


# ---------------------------------------------------------------- 基础类型
def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{field} 必须是 JSON 对象")
    return value


def _require_str(value: Any, field: str, *, min_len: int = 1, max_len: int = 4000) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是字符串")
    if "\x00" in value:
        raise SchemaError(f"{field} 不能包含 NUL 字符")
    text = value.strip()
    if len(text) < min_len:
        raise SchemaError(f"{field} 不能为空")
    if len(text) > max_len:
        raise SchemaError(f"{field} 过长（上限 {max_len} 字符）")
    return text


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{field} 必须是布尔值 true/false")
    return value


def _require_int(value: Any, field: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{field} 必须是整数")
    if not (minimum <= value <= maximum):
        raise SchemaError(f"{field} 超出范围 {minimum}..{maximum}: {value}")
    return value


# ---------------------------------------------------------------- 时间
def _parse_aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是带时区的 ISO 8601 时间字符串或 null")
    text = value.strip()
    if not text:
        raise SchemaError(f"{field} 不能为空字符串；没有值请用 null")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"{field} 不是合法的 ISO 8601 时间: {value!r}（{exc}）") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError(f"{field} 必须带时区偏移或 Z，不能是裸 datetime: {value!r}")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_optional_datetime(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return format_utc(_parse_aware(value, field))


def parse_required_datetime(value: Any, field: str) -> str:
    if value is None:
        raise SchemaError(f"{field} 不能为 null")
    return format_utc(_parse_aware(value, field))


def parse_at_argument(value: Any) -> datetime:
    """``--at`` 时间：必须带时区；缺省由调用方取当前时间。"""

    return _parse_aware(value, "--at")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_str() -> str:
    return format_utc(utc_now())


def add_days(iso_text: str, days: int) -> str:
    parsed = datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return format_utc(parsed + timedelta(days=days))


def parse_source_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是 YYYY-MM-DD 字符串或 null")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise SchemaError(f"{field} 必须是 YYYY-MM-DD 日期或 null: {value!r}") from exc
    return parsed.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- ID 与范围
def validate_id(value: Any, field: str = "id") -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是字符串")
    if len(value) > ID_MAX or not ID_RE.fullmatch(value):
        raise SchemaError(
            f"{field} 不合法: {value!r}。要求小写 ASCII 字母/数字/连字符、以 mem- 开头、长度不超过 {ID_MAX}"
        )
    return value


def validate_subject_key(value: Any, field: str = "subject_key") -> str:
    if not isinstance(value, str) or not SUBJECT_KEY_RE.fullmatch(value):
        raise SchemaError(f"{field} 必须是 1..200 位、不含空白与路径分隔符的稳定标识: {value!r}")
    return value


def normalize_target(value: Any, field: str = "scope.target") -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是工作区相对目录字符串")
    raw = value.strip().replace("\\", "/")
    if "\x00" in raw:
        raise SchemaError(f"{field} 不能包含 NUL 字符")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise SchemaError(f"{field} 必须是工作区相对目录，不能是绝对路径: {value!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SchemaError(f"{field} 不能包含 .. 路径穿越: {value!r}")
    return "/".join(parts) if parts else "."


def validate_scenes(value: Any, field: str = "scope.scenes") -> list[str]:
    if not isinstance(value, list):
        raise SchemaError(f"{field} 必须是字符串数组")
    if len(value) > 32:
        raise SchemaError(f"{field} 最多 32 项")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SchemaError(f"{field}[{index}] 必须是字符串")
        text = item.strip()
        if not text:
            raise SchemaError(f"{field}[{index}] 不能为空")
        if len(text) > 64:
            raise SchemaError(f"{field}[{index}] 过长（上限 64 字符）")
        if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in text):
            raise SchemaError(f"{field}[{index}] 不能包含空白或控制字符: {item!r}")
        if text in result:
            raise SchemaError(f"{field} 有重复场景: {text!r}")
        result.append(text)
    return result


def validate_scope(value: Any, field: str = "scope") -> dict[str, Any]:
    data = _require_mapping(value, field)
    allowed = {"level", "target", "scenes", "task_id"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SchemaError(f"{field} 出现未定义字段: {unknown}")
    for key in ("level", "target", "scenes"):
        if key not in data:
            raise SchemaError(f"{field} 缺少字段: {key}")
    level = data["level"]
    if level not in LEVELS:
        raise SchemaError(f"{field}.level 必须是 {list(LEVELS)} 之一，当前 {level!r}")
    target = normalize_target(data["target"], f"{field}.target")
    scenes = validate_scenes(data["scenes"], f"{field}.scenes")
    task_id = data.get("task_id")
    if task_id is not None:
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise SchemaError(f"{field}.task_id 必须是 1..200 位、不含空白与路径分隔符的字符串或 null")
    if level == "workspace":
        if target != ".":
            raise SchemaError(f"{field}: workspace 级别 target 必须是 '.'，当前 {target!r}")
        if task_id is not None:
            raise SchemaError(f"{field}: workspace 级别 task_id 必须为 null")
    elif level == "task":
        if task_id is None:
            raise SchemaError(f"{field}: task 级别必须提供 task_id")
    else:
        if target == ".":
            raise SchemaError(f"{field}: {level} 级别 target 必须是真实归属目录，不能是 '.'")
        if task_id is not None:
            raise SchemaError(f"{field}: {level} 级别 task_id 必须为 null")
    return {"level": level, "target": target, "scenes": scenes, "task_id": task_id}


def validate_tags(value: Any, field: str = "tags") -> list[str]:
    if not isinstance(value, list):
        raise SchemaError(f"{field} 必须是字符串数组")
    if len(value) > 50:
        raise SchemaError(f"{field} 最多 50 项")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SchemaError(f"{field}[{index}] 必须是字符串")
        text = item.strip()
        if not text:
            raise SchemaError(f"{field}[{index}] 不能为空")
        if len(text) > 64:
            raise SchemaError(f"{field}[{index}] 过长（上限 64 字符）")
        if any(ch.isspace() for ch in text) or "\x00" in text:
            raise SchemaError(f"{field}[{index}] 不能包含空白或 NUL: {item!r}")
        if text in result:
            raise SchemaError(f"{field} 有重复标签: {text!r}")
        result.append(text)
    return result


def validate_sources(value: Any, field: str = "sources") -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SchemaError(f"{field} 必须是非空数组")
    if len(value) > 50:
        raise SchemaError(f"{field} 最多 50 项")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"{field}[{index}]"
        source = _require_mapping(item, label)
        unknown = sorted(set(source) - {"kind", "locator", "source_date", "observed_at"})
        if unknown:
            raise SchemaError(f"{label} 出现未定义字段: {unknown}")
        for key in ("kind", "locator"):
            if key not in source:
                raise SchemaError(f"{label} 缺少字段: {key}")
        kind = source["kind"]
        if kind not in SOURCE_KINDS:
            raise SchemaError(f"{label}.kind 必须是 {list(SOURCE_KINDS)} 之一，当前 {kind!r}")
        locator = _require_str(source["locator"], f"{label}.locator", max_len=2000)
        source_date = parse_source_date(source.get("source_date"), f"{label}.source_date")
        observed_at = parse_optional_datetime(source.get("observed_at"), f"{label}.observed_at")
        result.append(
            {"kind": kind, "locator": locator, "source_date": source_date, "observed_at": observed_at}
        )
    return result


# ---------------------------------------------------------------- 正文
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def normalize_body_text(body: Any) -> str:
    if not isinstance(body, str):
        raise SchemaError("body 必须是字符串")
    text = body
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\n").rstrip("\n")
    if len(text) > MAX_BODY_CHARS:
        raise SchemaError(f"body 过长（上限 {MAX_BODY_CHARS} 字符）")
    return text


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    fence_marker = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_fence:
            if fence_marker and stripped.startswith(fence_marker):
                in_fence = False
            continue
        fence = _FENCE_RE.match(stripped)
        if fence:
            in_fence = True
            fence_marker = fence.group(1)
            continue
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    return headings


def analyze_body(body: Any, *, status: str, kind: str) -> dict[str, Any]:
    canonical = normalize_body_text(body)
    if not canonical.strip():
        raise SchemaError("正文不能为空")
    lines = canonical.split("\n")
    headings = _headings(lines)
    h1s = [heading for heading in headings if heading[1] == 1]
    if len(h1s) != 1:
        raise SchemaError(f"正文必须恰好有一个 H1 标题，当前有 {len(h1s)} 个")
    first_content = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_content is None or h1s[0][0] != first_content:
        raise SchemaError("正文 H1 必须是第一个非空行")
    title = h1s[0][2]
    if not title:
        raise SchemaError("正文 H1 标题不能为空")
    if len(title) > TITLE_MAX:
        raise SchemaError(f"正文 H1 标题过长（上限 {TITLE_MAX} 字符）")
    h2_texts = [heading[2] for heading in headings if heading[1] == 2]
    if "结论" not in h2_texts:
        raise SchemaError("正文缺少“## 结论”段落")
    if not extract_section(canonical, "结论"):
        raise SchemaError("“## 结论”段落不能为空")
    if status == "candidate" and "待澄清问题" not in h2_texts:
        raise SchemaError("candidate 条目的正文必须有“## 待澄清问题”段落")
    return {"canonical": canonical, "title": title, "h2": h2_texts, "kind": kind}


def extract_section(body: str, heading: str) -> str | None:
    """提取某个二级标题的正文（到下一个同级或更高级标题为止）。"""

    lines = body.split("\n")
    headings = _headings(lines)
    for position, (line_index, level, text) in enumerate(headings):
        if level == 2 and text == heading:
            end = len(lines)
            for later_index, later_level, _ in headings[position + 1 :]:
                if later_level <= 2:
                    end = later_index
                    break
            section = "\n".join(lines[line_index + 1 : end]).strip()
            return section or None
    return None


# ---------------------------------------------------------------- metadata
def _validate_semantics(metadata: dict[str, Any]) -> None:
    status = metadata["status"]
    superseded_by = metadata["superseded_by"]
    if status == "superseded":
        if superseded_by is None:
            raise SchemaError("superseded 状态必须填写 superseded_by（指向现存替代条目）")
        validate_id(superseded_by, "superseded_by")
        if superseded_by == metadata["id"]:
            raise SchemaError("superseded_by 不能指向自身")
    elif superseded_by is not None:
        raise SchemaError(f"状态为 {status} 时 superseded_by 必须为 null，不能维护反向关系")
    valid_from = metadata["valid_from"]
    valid_until = metadata["valid_until"]
    if valid_from is not None and valid_until is not None and not (valid_until > valid_from):
        raise SchemaError(f"valid_until 必须晚于 valid_from（半开区间 [valid_from, valid_until)）：{valid_from} .. {valid_until}")
    review_after = metadata["review_after"]
    if review_after is not None and valid_from is not None and review_after < valid_from:
        # 复核提示早于生效起点通常无意义，但不阻止合法数据；这里只做类型与顺序的显式记录
        pass
    if metadata["end_condition"] is not None:
        _require_str(metadata["end_condition"], "end_condition", max_len=2000)


def validate_read_metadata(data: Any, *, origin: str) -> dict[str, Any]:
    """校验并规范化一份权威条目的 metadata（文件读取路径）。"""

    metadata = _require_mapping(data, f"{origin}: metadata")
    unknown = sorted(set(metadata) - set(FIELD_ORDER))
    if unknown:
        raise SchemaError(f"{origin}: 出现 v1 未定义字段: {unknown}（拼写错误不会被静默忽略）")
    missing = [field for field in READ_REQUIRED_FIELDS if field not in metadata]
    if missing:
        raise SchemaError(f"{origin}: 缺少必填字段: {missing}")
    schema_version = metadata["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise SchemaError(f"{origin}: schema_version 必须是整数")
    if schema_version != SCHEMA_VERSION:
        raise SchemaError(f"{origin}: 不支持的 schema_version={schema_version}（本工具支持 {SCHEMA_VERSION}）")
    normalized: dict[str, Any] = {}
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["id"] = validate_id(metadata["id"])
    normalized["revision"] = _require_int(metadata["revision"], "revision", minimum=1)
    kind = metadata["kind"]
    if kind not in KINDS:
        raise SchemaError(f"{origin}: kind 必须是 {list(KINDS)} 之一，当前 {kind!r}")
    normalized["kind"] = kind
    normalized["subject_key"] = validate_subject_key(metadata["subject_key"])
    status = metadata["status"]
    if status not in STATUSES:
        raise SchemaError(f"{origin}: status 必须是 {list(STATUSES)} 之一，当前 {status!r}")
    normalized["status"] = status
    normalized["scope"] = validate_scope(metadata["scope"])
    normalized["pinned"] = _require_bool(metadata["pinned"], "pinned")
    normalized["tags"] = validate_tags(metadata["tags"])
    normalized["sources"] = validate_sources(metadata["sources"])
    normalized["created_at"] = parse_required_datetime(metadata["created_at"], "created_at")
    normalized["updated_at"] = parse_required_datetime(metadata["updated_at"], "updated_at")
    normalized["last_verified_at"] = parse_optional_datetime(metadata.get("last_verified_at"), "last_verified_at")
    normalized["valid_from"] = parse_optional_datetime(metadata.get("valid_from"), "valid_from")
    normalized["valid_until"] = parse_optional_datetime(metadata.get("valid_until"), "valid_until")
    normalized["review_after"] = parse_optional_datetime(metadata.get("review_after"), "review_after")
    end_condition = metadata.get("end_condition")
    normalized["end_condition"] = (
        _require_str(end_condition, "end_condition", max_len=2000) if end_condition is not None else None
    )
    superseded_by = metadata.get("superseded_by")
    normalized["superseded_by"] = validate_id(superseded_by, "superseded_by") if superseded_by is not None else None
    _validate_semantics(normalized)
    return {field: normalized[field] for field in FIELD_ORDER}


def validate_write_metadata(
    data: Any,
    *,
    update: bool,
    cli_id: str | None = None,
) -> dict[str, Any]:
    """校验写入请求中的可写字段，返回规范化结果（含 id 键，可为 None）。

    不接受工具托管字段；缺少必填字段直接拒绝，不把漏填当清空。
    """

    metadata = _require_mapping(data, "metadata")
    tool_fields = sorted(set(metadata) & set(TOOL_MANAGED_FIELDS))
    if tool_fields:
        raise SchemaError(
            f"metadata 不接受工具托管字段: {tool_fields}。"
            " schema_version/revision/created_at/updated_at/last_verified_at 由工具维护；"
            " 修改 last_verified_at 请用顶层 verification 对象。"
        )
    unknown = sorted(set(metadata) - set(WRITABLE_FIELDS))
    if unknown:
        raise SchemaError(f"metadata 出现未定义字段: {unknown}")
    missing = [field for field in WRITE_REQUIRED_FIELDS if field not in metadata]
    if missing:
        raise SchemaError(f"metadata 缺少必填字段: {missing}")
    entry_id = metadata.get("id")
    if update:
        if entry_id is None:
            raise SchemaError("update 的 metadata 必须包含 id，且与命令行 ID 一致")
        entry_id = validate_id(entry_id, "metadata.id")
        if cli_id is not None and entry_id != cli_id:
            raise SchemaError(f"metadata.id 必须与命令行 ID 一致（{entry_id!r} != {cli_id!r}），不支持改名换 ID")
    elif entry_id is not None:
        entry_id = validate_id(entry_id, "metadata.id")

    result: dict[str, Any] = {}
    result["id"] = entry_id
    kind = metadata["kind"]
    if kind not in KINDS:
        raise SchemaError(f"kind 必须是 {list(KINDS)} 之一，当前 {kind!r}")
    result["kind"] = kind
    result["subject_key"] = validate_subject_key(metadata["subject_key"])
    status = metadata["status"]
    if status not in STATUSES:
        raise SchemaError(f"status 必须是 {list(STATUSES)} 之一，当前 {status!r}")
    result["status"] = status
    result["scope"] = validate_scope(metadata["scope"])
    result["pinned"] = _require_bool(metadata.get("pinned", False), "pinned")
    result["tags"] = validate_tags(metadata.get("tags", []))
    result["sources"] = validate_sources(metadata["sources"])
    result["valid_from"] = parse_optional_datetime(metadata.get("valid_from"), "valid_from")
    result["valid_until"] = parse_optional_datetime(metadata.get("valid_until"), "valid_until")
    result["review_after"] = parse_optional_datetime(metadata.get("review_after"), "review_after")
    end_condition = metadata.get("end_condition")
    result["end_condition"] = (
        _require_str(end_condition, "end_condition", max_len=2000) if end_condition is not None else None
    )
    superseded_by = metadata.get("superseded_by")
    result["superseded_by"] = validate_id(superseded_by, "superseded_by") if superseded_by is not None else None
    # 先用占位 id 做语义检查（superseded 自指检查依赖真实 id，稍后由调用方再查）
    probe = dict(result)
    probe["id"] = entry_id or "mem-placeholder"
    _validate_semantics(probe)
    return result


def validate_verification(value: Any, label: str = "verification") -> dict[str, Any]:
    data = _require_mapping(value, label)
    unknown = sorted(set(data) - {"verified_at", "source_locator", "reason"})
    if unknown:
        raise SchemaError(f"{label} 出现未定义字段: {unknown}")
    for key in ("verified_at", "source_locator", "reason"):
        if key not in data:
            raise SchemaError(f"{label} 缺少字段: {key}")
    return {
        "verified_at": parse_required_datetime(data["verified_at"], f"{label}.verified_at"),
        "source_locator": _require_str(data["source_locator"], f"{label}.source_locator", max_len=2000),
        "reason": _require_str(data["reason"], f"{label}.reason", max_len=2000),
    }


def build_full_metadata(
    writable: dict[str, Any],
    *,
    entry_id: str,
    revision: int,
    created_at: str,
    updated_at: str,
    last_verified_at: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": entry_id,
        "revision": revision,
        "kind": writable["kind"],
        "subject_key": writable["subject_key"],
        "status": writable["status"],
        "scope": dict(writable["scope"]),
        "pinned": writable["pinned"],
        "tags": list(writable["tags"]),
        "sources": [dict(source) for source in writable["sources"]],
        "created_at": created_at,
        "updated_at": updated_at,
        "last_verified_at": last_verified_at,
        "valid_from": writable["valid_from"],
        "valid_until": writable["valid_until"],
        "review_after": writable["review_after"],
        "end_condition": writable["end_condition"],
        "superseded_by": writable["superseded_by"],
    }
    _validate_semantics(metadata)
    return {field: metadata[field] for field in FIELD_ORDER}


# ---------------------------------------------------------------- 序列化 / 解析
@dataclass(frozen=True)
class ParsedEntry:
    metadata: dict[str, Any]
    body: str
    title: str
    raw_text: str
    raw_size: int
    sha256: str
    origin: str

    @property
    def id(self) -> str:
        return self.metadata["id"]

    @property
    def revision(self) -> int:
        return self.metadata["revision"]

    @property
    def kind(self) -> str:
        return self.metadata["kind"]

    @property
    def status(self) -> str:
        return self.metadata["status"]

    @property
    def scope(self) -> dict[str, Any]:
        return self.metadata["scope"]

    @property
    def subject_key(self) -> str:
        return self.metadata["subject_key"]

    @property
    def conclusion(self) -> str | None:
        return extract_section(self.body, "结论")


def serialize_entry(metadata: dict[str, Any], body: str) -> bytes:
    """把 canonical metadata 与 canonical body 序列化为规范 UTF-8 字节。"""

    missing = [field for field in FIELD_ORDER if field not in metadata]
    if missing:
        raise SchemaError(f"序列化 metadata 缺少字段: {missing}")
    unknown = sorted(set(metadata) - set(FIELD_ORDER))
    if unknown:
        raise SchemaError(f"序列化 metadata 出现未定义字段: {unknown}")
    canonical = normalize_body_text(body)
    ordered = {field: metadata[field] for field in FIELD_ORDER}
    json_text = __import__("json").dumps(ordered, ensure_ascii=False, indent=2)
    text = "---\n" + json_text + "\n---\n\n" + canonical + "\n"
    return text.encode("utf-8")


def parse_entry_bytes(raw: bytes, *, origin: str) -> ParsedEntry:
    if len(raw) > MAX_ENTRY_BYTES:
        raise SchemaError(f"{origin}: 条目文件过大（上限 {MAX_ENTRY_BYTES} 字节）")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{origin}: 不是合法的 UTF-8 文本（{exc}）") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SchemaError(f"{origin}: 文件必须以独立的 --- 行开始 JSON front matter")
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise SchemaError(f"{origin}: front matter 缺少结束的 --- 行")
    json_text = "\n".join(lines[1:closing])
    metadata_raw = loads_strict(json_text, f"{origin}: front matter")
    metadata = validate_read_metadata(metadata_raw, origin=origin)
    body_text = "\n".join(lines[closing + 1 :])
    body_info = analyze_body(body_text, status=metadata["status"], kind=metadata["kind"])
    return ParsedEntry(
        metadata=metadata,
        body=body_info["canonical"],
        title=body_info["title"],
        raw_text=text,
        raw_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        origin=origin,
    )


def make_parsed_entry(
    metadata: dict[str, Any],
    body: str,
    *,
    origin: str,
    raw: bytes,
) -> ParsedEntry:
    body_info = analyze_body(body, status=metadata["status"], kind=metadata["kind"])
    return ParsedEntry(
        metadata={field: metadata[field] for field in FIELD_ORDER},
        body=body_info["canonical"],
        title=body_info["title"],
        raw_text="",
        raw_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        origin=origin,
    )


def has_user_source(metadata: dict[str, Any]) -> bool:
    return any(source.get("kind") in USER_SOURCE_KINDS for source in metadata["sources"])


def source_locators(metadata: dict[str, Any]) -> list[str]:
    return [str(source.get("locator")) for source in metadata["sources"]]


__all__ = [
    "ID_RE",
    "KINDS",
    "STATUSES",
    "LEVELS",
    "LEVEL_RANK",
    "SOURCE_KINDS",
    "USER_SOURCE_KINDS",
    "FIELD_ORDER",
    "TOOL_MANAGED_FIELDS",
    "WRITABLE_FIELDS",
    "WRITE_REQUIRED_FIELDS",
    "ParsedEntry",
    "validate_id",
    "validate_scope",
    "validate_sources",
    "validate_write_metadata",
    "validate_read_metadata",
    "validate_verification",
    "build_full_metadata",
    "serialize_entry",
    "parse_entry_bytes",
    "make_parsed_entry",
    "analyze_body",
    "normalize_body_text",
    "extract_section",
    "parse_at_argument",
    "parse_optional_datetime",
    "parse_required_datetime",
    "format_utc",
    "utc_now",
    "utc_now_str",
    "add_days",
    "has_user_source",
    "source_locators",
    "MAX_ENTRY_BYTES",
]
