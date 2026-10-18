"""记忆库定位、快照读取、范围/时间语义、不变量与冲突组。

查询默认直接扫描权威原文；索引只是派生视图。本模块不取锁、不写权威条目。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import ConflictError, IOFailure, SchemaError, StructureConflictError, UsageError
from .pathutil import (
    is_within,
    parts_within,
    path_parts,
    resolve_path,
    relative_posix,
    safe_read_bytes,
)
from .schema import (
    ID_MAX,
    ID_RE,
    LEVEL_RANK,
    ParsedEntry,
    has_user_source,
    parse_entry_bytes,
)

# ---------------------------------------------------------------- 相对路径约定
MEMORY_DIR_PARTS = ("中枢", "记忆")
ENTRIES_DIR_PARTS = ("中枢", "记忆", "条目")
RUN_DIR_PARTS = ("中枢", "记忆", "运行")
MEMORY_MD_PARTS = ("中枢", "记忆", "记忆.md")
CONFIG_PARTS = ("中枢", "记忆", "配置.json")
CREDENTIAL_DIR_PARTS = ("中枢", "记录", "记忆变更")
INDEX_NAME = "index.json"
LOCK_NAME = "memory.lock"
TRANSACTIONS_DIR_NAME = "transactions"
MANIFEST_NAME = "manifest.json"
STAGED_DIR_NAME = "staged"

BEGIN_MARKER = "<!-- BEGIN GENERATED MEMORY LINKS -->"
END_MARKER = "<!-- END GENERATED MEMORY LINKS -->"


@dataclass(frozen=True)
class Paths:
    """工作区内的固定记忆路径（全部由已核实的根推算）。"""

    root: Path

    @classmethod
    def build(cls, root: Path | str) -> "Paths":
        return cls(root=resolve_path(root))

    @property
    def memory_dir(self) -> Path:
        return self.root.joinpath(*MEMORY_DIR_PARTS)

    @property
    def entries_dir(self) -> Path:
        return self.root.joinpath(*ENTRIES_DIR_PARTS)

    @property
    def run_dir(self) -> Path:
        return self.root.joinpath(*RUN_DIR_PARTS)

    @property
    def tx_root(self) -> Path:
        return self.run_dir / TRANSACTIONS_DIR_NAME

    @property
    def index_file(self) -> Path:
        return self.run_dir / INDEX_NAME

    @property
    def lock_file(self) -> Path:
        return self.run_dir / LOCK_NAME

    @property
    def memory_md(self) -> Path:
        return self.root.joinpath(*MEMORY_MD_PARTS)

    @property
    def config_file(self) -> Path:
        return self.root.joinpath(*CONFIG_PARTS)

    @property
    def credentials_dir(self) -> Path:
        return self.root.joinpath(*CREDENTIAL_DIR_PARTS)

    def entry_path(self, entry_id: str) -> Path:
        return self.entries_dir / f"{entry_id}.md"

    def rel(self, path: Path) -> str:
        return relative_posix(self.root, path)


# ---------------------------------------------------------------- 路径安全
def ensure_entries_dir_safe(paths: Paths) -> bool:
    """检查条目目录的物理位置没有越出中枢/记忆；返回目录是否存在。"""

    entries = paths.entries_dir
    if not entries.exists():
        return False
    if not entries.is_dir():
        raise UsageError(f"条目路径不是目录: {entries}")
    real_entries = resolve_path(entries)
    real_memory = resolve_path(paths.memory_dir)
    if not is_within(real_entries, real_memory):
        raise UsageError(
            f"条目目录解析后越出中枢/记忆（可能是指向外部的符号链接/junction），拒绝访问: {real_entries}"
        )
    return True


def entry_file_path(paths: Paths, entry_id: str) -> Path:
    """由 ID 决定条目文件路径，并做组件级边界检查。"""

    if not isinstance(entry_id, str) or len(entry_id) > ID_MAX or not ID_RE.fullmatch(entry_id):
        raise UsageError(f"非法条目 ID: {entry_id!r}（不能作为路径入口）")
    entries = paths.entries_dir
    file_path = entries / f"{entry_id}.md"
    if entries.exists():
        real_entries = resolve_path(entries)
        real_memory = resolve_path(paths.memory_dir)
        if not is_within(real_entries, real_memory):
            raise UsageError(f"条目目录越出中枢/记忆，拒绝访问: {real_entries}")
        if file_path.exists() or file_path.is_symlink():
            real_file = resolve_path(file_path)
            if not is_within(real_file, real_entries):
                raise UsageError(f"条目文件解析后越出条目目录（链接逃逸），拒绝访问: {entry_id}")
    return file_path


# ---------------------------------------------------------------- 快照
@dataclass
class StoreSnapshot:
    entries: dict[str, ParsedEntry] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    entries_dir_exists: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def sorted_entries(self) -> list[ParsedEntry]:
        return [self.entries[key] for key in sorted(self.entries)]

    def ensure_complete(self) -> None:
        if self.errors:
            raise UsageError(
                "记忆库存在无法解析的条目，写入前必须先修复（用 validate 查看具体文件）",
                details={"store_errors": self.errors},
            )


def load_snapshot(paths: Paths) -> StoreSnapshot:
    exists = ensure_entries_dir_safe(paths)
    snapshot = StoreSnapshot(entries_dir_exists=exists)
    if not exists:
        return snapshot
    try:
        candidates = sorted(paths.entries_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise IOFailure(f"无法列出条目目录 {paths.entries_dir}: {exc}") from exc
    for file_path in candidates:
        if not file_path.name.endswith(".md"):
            continue
        entry_id = file_path.name[: -len(".md")]
        try:
            safe_path = entry_file_path(paths, entry_id)
            raw = safe_read_bytes(safe_path, paths.rel(safe_path))
            entry = parse_entry_bytes(raw, origin=paths.rel(safe_path))
            if entry.id != entry_id:
                raise SchemaError(
                    f"{paths.rel(safe_path)}: 文件名 {entry_id}.md 与 metadata.id {entry.id} 不一致"
                )
            snapshot.entries[entry.id] = entry
        except (SchemaError, UsageError, IOFailure) as exc:
            snapshot.errors.append(
                {
                    "path": paths.rel(file_path) if is_within(file_path, paths.root) else str(file_path),
                    "id_hint": entry_id,
                    "kind": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return snapshot


def load_entry(paths: Paths, entry_id: str) -> ParsedEntry:
    ensure_entries_dir_safe(paths)
    path = entry_file_path(paths, entry_id)
    if not path.is_file():
        raise UsageError(f"条目不存在: {entry_id}")
    raw = safe_read_bytes(path, paths.rel(path))
    entry = parse_entry_bytes(raw, origin=paths.rel(path))
    if entry.id != entry_id:
        raise SchemaError(f"{paths.rel(path)}: 文件名与 metadata.id 不一致（{entry.id}）")
    return entry


def ensure_write_dirs(paths: Paths) -> None:
    """为写入准备机械目录；不创建任何权威条目内容。"""

    try:
        paths.entries_dir.mkdir(parents=True, exist_ok=True)
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.tx_root.mkdir(parents=True, exist_ok=True)
        paths.credentials_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IOFailure(f"无法创建记忆运行/条目目录: {exc}") from exc
    ensure_entries_dir_safe(paths)


# ---------------------------------------------------------------- 时间与范围
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_stored_time(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def target_path(paths: Paths, scope: dict[str, Any]) -> Path:
    target = scope["target"]
    if target == ".":
        return paths.root
    return paths.root.joinpath(*target.split("/"))


def scope_specificity(entry: ParsedEntry) -> tuple[int, int]:
    scope = entry.scope
    depth = 0 if scope["target"] == "." else len(scope["target"].split("/"))
    return (LEVEL_RANK[scope["level"]], depth)


def scope_applies(
    paths: Paths,
    entry: ParsedEntry,
    *,
    cwd: Path,
    scenes: Sequence[str],
    task_id: str | None,
) -> bool:
    scope = entry.scope
    if scope["level"] != "workspace":
        if not is_within(cwd, target_path(paths, scope)):
            return False
    if scope["level"] == "task":
        if task_id is None or scope["task_id"] != task_id:
            return False
    entry_scenes = scope["scenes"]
    if entry_scenes:
        requested = set(scenes)
        if not requested:
            return False
        if not (set(entry_scenes) & requested):
            return False
    return True


def validity_state(entry: ParsedEntry, at: datetime) -> dict[str, Any]:
    valid_from = entry.metadata["valid_from"]
    valid_until = entry.metadata["valid_until"]
    not_yet = valid_from is not None and at < parse_stored_time(valid_from)
    expired = valid_until is not None and at >= parse_stored_time(valid_until)
    return {
        "valid_from": valid_from,
        "valid_until": valid_until,
        "not_yet_valid": bool(not_yet),
        "expired": bool(expired),
        "valid_now": bool(not not_yet and not expired),
    }


def is_current(entry: ParsedEntry, at: datetime) -> bool:
    return entry.status == "active" and validity_state(entry, at)["valid_now"]


def review_due(entry: ParsedEntry, at: datetime) -> bool:
    review_after = entry.metadata["review_after"]
    return review_after is not None and at >= parse_stored_time(review_after)


def status_label(entry: ParsedEntry, at: datetime) -> str:
    status = entry.status
    state = validity_state(entry, at)
    label = {
        "candidate": "候选（待澄清，不得作为当前事实）",
        "superseded": "已被替代（不作为当前依据）",
        "retired": "已停用（不进入当前上下文）",
        "rejected": "已否决（不得再次使用）",
        "active": "当前有效" if state["valid_now"] else ("尚未生效" if state["not_yet_valid"] else "已过有效期"),
    }[status]
    if entry.kind == "episode":
        label += " · 历史经历"
    return label


def entry_summary(paths: Paths, entry: ParsedEntry, at: datetime) -> dict[str, Any]:
    metadata = entry.metadata
    state = validity_state(entry, at)
    summary: dict[str, Any] = {
        "id": entry.id,
        "title": entry.title,
        "kind": entry.kind,
        "status": entry.status,
        "status_label": status_label(entry, at),
        "revision": entry.revision,
        "sha256": entry.sha256,
        "path": entry.origin,
        "authoritative_path": entry.origin,
        "scope": dict(metadata["scope"]),
        "pinned": metadata["pinned"],
        "tags": list(metadata["tags"]),
        "sources": [dict(source) for source in metadata["sources"]],
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
        "last_verified_at": metadata["last_verified_at"],
        "valid_from": metadata["valid_from"],
        "valid_until": metadata["valid_until"],
        "review_after": metadata["review_after"],
        "end_condition": metadata["end_condition"],
        "superseded_by": metadata["superseded_by"],
        "needs_review": review_due(entry, at),
        "is_current": is_current(entry, at),
        "valid_now": state["valid_now"],
        "not_yet_valid": state["not_yet_valid"],
        "expired": state["expired"],
        "is_historical": entry.kind == "episode",
        "conclusion": entry.conclusion,
    }
    return summary


# ---------------------------------------------------------------- 不变量
def _finding(code: str, message: str, *, ids: Iterable[str] = (), severity: str = "error", path: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {"code": code, "severity": severity, "message": message, "ids": sorted(set(ids))}
    if path:
        finding["path"] = path
    return finding


def check_invariants(entries: dict[str, ParsedEntry], paths: Paths) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    # 范围目标不得越出工作区（含 junction 逃逸）
    for entry in entries.values():
        if entry.scope["level"] == "workspace":
            continue
        resolved = resolve_path(target_path(paths, entry.scope))
        if not is_within(resolved, paths.root):
            findings.append(
                _finding(
                    "scope_escape",
                    f"scope.target 解析后越出工作区: {entry.id} target={entry.scope['target']!r} -> {resolved}",
                    ids=[entry.id],
                )
            )
    # 替代链：有效、无自指（解析层已查）、无环
    for entry in entries.values():
        if entry.status != "superseded":
            continue
        target = entries.get(entry.metadata["superseded_by"] or "")
        if target is None:
            findings.append(
                _finding(
                    "superseded_target_missing",
                    f"superseded 条目 {entry.id} 指向不存在的替代条目 {entry.metadata['superseded_by']!r}",
                    ids=[entry.id],
                )
            )
            continue
        ordered = [entry.id]
        current = target
        seen = {entry.id}
        while current is not None and current.status == "superseded":
            if current.id in seen:
                findings.append(
                    _finding(
                        "superseded_cycle",
                        "替代链存在环: " + " -> ".join(ordered + [current.id]),
                        ids=seen,
                    )
                )
                break
            seen.add(current.id)
            ordered.append(current.id)
            current = entries.get(current.metadata["superseded_by"] or "")
            if current is None:
                findings.append(
                    _finding(
                        "superseded_target_missing",
                        "替代链中断，找不到目标: " + " -> ".join(ordered),
                        ids=seen,
                    )
                )
                break
    # active 的 preference 必须有用户确认类来源
    for entry in entries.values():
        if entry.status == "active" and entry.kind == "preference" and not has_user_source(entry.metadata):
            findings.append(
                _finding(
                    "active_preference_source",
                    f"active 的 preference 缺少 user_explicit/user_quote_in_task/user_confirmed 来源: {entry.id}",
                    ids=[entry.id],
                )
            )
    return findings


def check_scope_targets_exist(entries: dict[str, ParsedEntry], paths: Paths) -> list[dict[str, Any]]:
    """doctor 用：scope.target 目录不存在时给 warning（不阻止合法先写后建）。"""

    findings: list[dict[str, Any]] = []
    for entry in entries.values():
        if entry.scope["level"] == "workspace":
            continue
        path = target_path(paths, entry.scope)
        if not path.exists():
            findings.append(
                _finding(
                    "scope_target_missing",
                    f"{entry.id} 的 scope.target 目录当前不存在: {entry.scope['target']}（若目录尚未创建可忽略）",
                    ids=[entry.id],
                    severity="warning",
                )
            )
    return findings


# ---------------------------------------------------------------- 冲突组
def _intervals_overlap(a: ParsedEntry, b: ParsedEntry) -> bool:
    a_from = parse_stored_time(a.metadata["valid_from"]) if a.metadata["valid_from"] else None
    a_until = parse_stored_time(a.metadata["valid_until"]) if a.metadata["valid_until"] else None
    b_from = parse_stored_time(b.metadata["valid_from"]) if b.metadata["valid_from"] else None
    b_until = parse_stored_time(b.metadata["valid_until"]) if b.metadata["valid_until"] else None
    starts = [value for value in (a_from, b_from) if value is not None]
    ends = [value for value in (a_until, b_until) if value is not None]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if start is not None and end is not None:
        return start < end  # 半开区间
    return True


def _scenes_overlap(a: ParsedEntry, b: ParsedEntry) -> bool:
    a_scenes = set(a.scope["scenes"])
    b_scenes = set(b.scope["scenes"])
    if not a_scenes or not b_scenes:
        return True
    return bool(a_scenes & b_scenes)


def _target_relation(paths: Paths, a: ParsedEntry, b: ParsedEntry) -> str:
    a_parts = path_parts(target_path(paths, a.scope))
    b_parts = path_parts(target_path(paths, b.scope))
    if a_parts == b_parts:
        return "equal"
    if parts_within(a_parts, b_parts):
        return "a_in_b"
    if parts_within(b_parts, a_parts):
        return "b_in_a"
    return "disjoint"


def _pair_conflict(paths: Paths, a: ParsedEntry, b: ParsedEntry) -> dict[str, Any] | None:
    if a.status != "active" or b.status != "active":
        return None
    if a.subject_key != b.subject_key:
        return None
    if a.scope["level"] == "task" and b.scope["level"] == "task":
        if a.scope["task_id"] != b.scope["task_id"]:
            return None
    relation = _target_relation(paths, a, b)
    if relation == "disjoint":
        return None
    if not _scenes_overlap(a, b):
        return None
    if not _intervals_overlap(a, b):
        return None
    if relation == "equal":
        return {
            "a": a,
            "b": b,
            "relation": relation,
            "kind": "same_level_overlap" if a.scope["level"] == b.scope["level"] else "equal_target_overlap",
        }
    specific = a if relation == "a_in_b" else b
    # 更具体的项目/任务例外允许共存（严格包含，且更具体方为 project/task）
    if specific.scope["level"] in ("project", "task"):
        return None
    return {
        "a": a,
        "b": b,
        "relation": relation,
        "kind": "same_level_overlap" if a.scope["level"] == b.scope["level"] else "nested_overlap",
    }


def conflict_pairs(paths: Paths, entries: Iterable[ParsedEntry]) -> list[dict[str, Any]]:
    items = sorted((entry for entry in entries if entry.status == "active"), key=lambda item: item.id)
    pairs: list[dict[str, Any]] = []
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            pair = _pair_conflict(paths, first, second)
            if pair is not None:
                pairs.append(pair)
    return pairs


def compute_conflicts(paths: Paths, entries: Iterable[ParsedEntry]) -> list[dict[str, Any]]:
    pairs = conflict_pairs(paths, entries)
    if not pairs:
        return []
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    involved: dict[str, ParsedEntry] = {}
    for pair in pairs:
        union(pair["a"].id, pair["b"].id)
        involved[pair["a"].id] = pair["a"]
        involved[pair["b"].id] = pair["b"]
    groups: dict[str, list[str]] = {}
    for entry_id in involved:
        groups.setdefault(find(entry_id), []).append(entry_id)
    result: list[dict[str, Any]] = []
    for members in groups.values():
        member_ids = sorted(members)
        member_entries = [involved[entry_id] for entry_id in member_ids]
        group_pairs = [pair for pair in pairs if pair["a"].id in member_ids and pair["b"].id in member_ids]
        kinds = sorted({pair["kind"] for pair in group_pairs})
        result.append(
            {
                "subject_key": member_entries[0].subject_key,
                "ids": member_ids,
                "kinds": kinds,
                "entries": [
                    {
                        "id": entry.id,
                        "title": entry.title,
                        "level": entry.scope["level"],
                        "target": entry.scope["target"],
                        "scenes": list(entry.scope["scenes"]),
                        "task_id": entry.scope["task_id"],
                        "valid_from": entry.metadata["valid_from"],
                        "valid_until": entry.metadata["valid_until"],
                    }
                    for entry in member_entries
                ],
                "pairs": [
                    {"a": pair["a"].id, "b": pair["b"].id, "relation": pair["relation"], "kind": pair["kind"]}
                    for pair in group_pairs
                ],
            }
        )
    result.sort(key=lambda group: group["ids"])
    return result


def conflict_pair_keys(paths: Paths, entries: Iterable[ParsedEntry]) -> set[frozenset[str]]:
    return {frozenset((pair["a"].id, pair["b"].id)) for pair in conflict_pairs(paths, entries)}


def ensure_final_state_valid(paths: Paths, before: dict[str, ParsedEntry], after: dict[str, ParsedEntry]) -> None:
    """事务提交前检查：最终状态不变量合法，且没有引入新的非法结构冲突。"""

    findings = check_invariants(after, paths)
    if findings:
        raise StructureConflictError(
            "事务完成后的记忆库仍有结构错误，拒绝提交: " + "；".join(item["message"] for item in findings[:5]),
            details={"findings": findings},
        )
    new_pairs = conflict_pair_keys(paths, after.values()) - conflict_pair_keys(paths, before.values())
    if new_pairs:
        groups = compute_conflicts(paths, after.values())
        relevant = [group for group in groups if any(frozenset((member, other)) in new_pairs for member in group["ids"] for other in group["ids"])]
        raise ConflictError(
            "写事务会引入新的结构冲突，已在提交前拒绝: "
            + "；".join(
                "subject_key=" + group["subject_key"] + " ids=" + ",".join(group["ids"]) for group in relevant
            ),
            details={"conflicts": relevant, "new_pairs": sorted(",".join(sorted(pair)) for pair in new_pairs)},
        )


def sorted_entries(entries: dict[str, ParsedEntry]) -> list[ParsedEntry]:
    return [entries[key] for key in sorted(entries)]


__all__ = [
    "MEMORY_DIR_PARTS",
    "ENTRIES_DIR_PARTS",
    "RUN_DIR_PARTS",
    "MEMORY_MD_PARTS",
    "CONFIG_PARTS",
    "CREDENTIAL_DIR_PARTS",
    "INDEX_NAME",
    "LOCK_NAME",
    "TRANSACTIONS_DIR_NAME",
    "MANIFEST_NAME",
    "STAGED_DIR_NAME",
    "BEGIN_MARKER",
    "END_MARKER",
    "Paths",
    "StoreSnapshot",
    "ensure_entries_dir_safe",
    "entry_file_path",
    "load_snapshot",
    "load_entry",
    "ensure_write_dirs",
    "parse_stored_time",
    "target_path",
    "scope_specificity",
    "scope_applies",
    "validity_state",
    "is_current",
    "review_due",
    "status_label",
    "entry_summary",
    "check_invariants",
    "check_scope_targets_exist",
    "conflict_pairs",
    "compute_conflicts",
    "conflict_pair_keys",
    "ensure_final_state_valid",
    "sorted_entries",
]
