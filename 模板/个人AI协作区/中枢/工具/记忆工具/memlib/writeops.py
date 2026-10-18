"""写请求解析与事务计划构建（add / update / apply）。

写入请求的 metadata 只接受可写字段；schema_version/revision/created_at/
updated_at/last_verified_at 由工具托管，``verification`` 是修改
last_verified_at 的唯一入口。计划构建阶段完成全部校验、预期版本/SHA 检查
与"完成后的不变量/冲突"检查；真正落盘由 tx.execute_plan 负责。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .errors import ConflictError, IOFailure, SchemaError, UsageError
from .jsonutil import loads_strict, read_text_utf8
from .schema import (
    USER_SOURCE_KINDS,
    WRITABLE_FIELDS,
    ParsedEntry,
    add_days,
    analyze_body,
    build_full_metadata,
    has_user_source,
    parse_entry_bytes,
    serialize_entry,
    utc_now_str,
    validate_read_metadata,
    validate_verification,
    validate_write_metadata,
)
from .store import (
    Paths,
    StoreSnapshot,
    ensure_final_state_valid,
    entry_file_path,
    entry_summary,
)
from .tx import TargetChange, TransactionPlan, TxEnv, sha256_bytes

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_APPLY_OPERATIONS = 200

ADD_REQUEST_KEYS = {"metadata", "body", "reason", "actor", "verification"}
APPLY_REQUEST_KEYS = {"reason", "actor", "operations"}
RECORD_KEYS = {"metadata", "body"}
UPDATE_OP_KEYS = {"op", "id", "expected_revision", "expected_sha256", "record", "verification"}
ADD_OP_KEYS = {"op", "record", "verification"}
DELETE_OP_KEYS = {"op", "id", "expected_revision", "expected_sha256"}


@dataclass
class PlanOutcome:
    plan: TransactionPlan | None
    no_change: bool
    summary: dict[str, Any]


# ---------------------------------------------------------------- 请求文件
def load_request_file(value: str, *, label: str) -> Any:
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except OSError as exc:
        raise UsageError(f"{label}: 无法解析输入路径 {value!r}: {exc}") from exc
    if not path.is_file():
        raise UsageError(f"{label}: 输入文件不存在: {path}")
    text = read_text_utf8(path, label)
    return loads_strict(text, label)


def _require_text(value: Any, field: str, *, max_len: int = 4000) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        raise SchemaError(f"{field} 不能为空")
    if len(text) > max_len:
        raise SchemaError(f"{field} 过长（上限 {max_len} 字符）")
    return text


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{field} 必须是 JSON 对象")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SchemaError(f"{field} 出现未定义字段: {unknown}")


def _parse_common(data: dict[str, Any], allowed: set[str]) -> tuple[str, str]:
    _reject_unknown(data, allowed, "写入请求")
    for key in ("reason", "actor"):
        if key not in data:
            raise SchemaError(f"写入请求缺少字段: {key}")
    actor = _require_text(data["actor"], "actor", max_len=200)
    reason = _require_text(data["reason"], "reason", max_len=2000)
    return actor, reason


def _parse_record(record: Any, *, update: bool, cli_id: str | None) -> dict[str, Any]:
    data = _require_mapping(record, "record")
    _reject_unknown(data, RECORD_KEYS, "record")
    for key in ("metadata", "body"):
        if key not in data:
            raise SchemaError(f"record 缺少字段: {key}")
    if not isinstance(data["body"], str):
        raise SchemaError("record.body 必须是字符串")
    writable = validate_write_metadata(data["metadata"], update=update, cli_id=cli_id)
    return {"writable": writable, "body": data["body"]}


def parse_add_request(input_path: str) -> dict[str, Any]:
    data = _require_mapping(load_request_file(input_path, label="add 请求"), "写入请求")
    actor, reason = _parse_common(data, ADD_REQUEST_KEYS)
    for key in ("metadata", "body"):
        if key not in data:
            raise SchemaError(f"写入请求缺少字段: {key}")
    if not isinstance(data["body"], str):
        raise SchemaError("body 必须是字符串")
    writable = validate_write_metadata(data["metadata"], update=False)
    verification = (
        validate_verification(data["verification"]) if data.get("verification") is not None else None
    )
    return {"writable": writable, "body": data["body"], "actor": actor, "reason": reason, "verification": verification}


def parse_update_request(input_path: str, *, cli_id: str) -> dict[str, Any]:
    data = _require_mapping(load_request_file(input_path, label="update 请求"), "写入请求")
    actor, reason = _parse_common(data, ADD_REQUEST_KEYS)
    for key in ("metadata", "body"):
        if key not in data:
            raise SchemaError(f"写入请求缺少字段: {key}")
    if not isinstance(data["body"], str):
        raise SchemaError("body 必须是字符串")
    writable = validate_write_metadata(data["metadata"], update=True, cli_id=cli_id)
    verification = (
        validate_verification(data["verification"]) if data.get("verification") is not None else None
    )
    return {"writable": writable, "body": data["body"], "actor": actor, "reason": reason, "verification": verification}


def parse_expected_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.lower()):
        raise SchemaError(f"{field} 必须是 64 位十六进制 SHA-256")
    return value.lower()


def parse_expected_revision(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SchemaError(f"{field} 必须是 >=1 的整数")
    return value


def parse_apply_request(input_path: str) -> dict[str, Any]:
    data = _require_mapping(load_request_file(input_path, label="apply 请求"), "写入请求")
    actor, reason = _parse_common(data, APPLY_REQUEST_KEYS)
    if "operations" not in data:
        raise SchemaError("apply 请求缺少 operations")
    operations_raw = data["operations"]
    if not isinstance(operations_raw, list) or not operations_raw:
        raise SchemaError("operations 必须是非空数组")
    if len(operations_raw) > MAX_APPLY_OPERATIONS:
        raise SchemaError(f"operations 最多 {MAX_APPLY_OPERATIONS} 项")
    operations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def register(entry_id: str) -> None:
        if entry_id in seen_ids:
            raise SchemaError(f"同一事务出现重复目标 ID: {entry_id}（拒绝重复目标）")
        seen_ids.add(entry_id)

    for index, raw in enumerate(operations_raw):
        label = f"operations[{index}]"
        op = _require_mapping(raw, label)
        kind = op.get("op")
        if kind == "add":
            _reject_unknown(op, ADD_OP_KEYS, label)
            if "record" not in op:
                raise SchemaError(f"{label} 缺少 record")
            record = _parse_record(op["record"], update=False, cli_id=None)
            entry_id = record["writable"].get("id") or f"mem-{uuid.uuid4().hex}"
            record["writable"]["id"] = entry_id
            register(entry_id)
            verification = (
                validate_verification(op["verification"], f"{label}.verification")
                if op.get("verification") is not None
                else None
            )
            operations.append(
                {"op": "add", "id": entry_id, "record": record, "verification": verification}
            )
        elif kind == "update":
            _reject_unknown(op, UPDATE_OP_KEYS, label)
            for key in ("id", "expected_revision", "expected_sha256", "record"):
                if key not in op:
                    raise SchemaError(f"{label} 缺少字段: {key}")
            entry_id = op["id"]
            if not isinstance(entry_id, str):
                raise SchemaError(f"{label}.id 必须是字符串")
            register(entry_id)
            record = _parse_record(op["record"], update=True, cli_id=entry_id)
            verification = (
                validate_verification(op["verification"], f"{label}.verification")
                if op.get("verification") is not None
                else None
            )
            operations.append(
                {
                    "op": "update",
                    "id": entry_id,
                    "expected_revision": parse_expected_revision(op["expected_revision"], f"{label}.expected_revision"),
                    "expected_sha256": parse_expected_sha(op["expected_sha256"], f"{label}.expected_sha256"),
                    "record": record,
                    "verification": verification,
                }
            )
        elif kind == "delete":
            _reject_unknown(op, DELETE_OP_KEYS, label)
            for key in ("id", "expected_revision", "expected_sha256"):
                if key not in op:
                    raise SchemaError(f"{label} 缺少字段: {key}")
            entry_id = op["id"]
            if not isinstance(entry_id, str):
                raise SchemaError(f"{label}.id 必须是字符串")
            register(entry_id)
            operations.append(
                {
                    "op": "delete",
                    "id": entry_id,
                    "expected_revision": parse_expected_revision(op["expected_revision"], f"{label}.expected_revision"),
                    "expected_sha256": parse_expected_sha(op["expected_sha256"], f"{label}.expected_sha256"),
                }
            )
        else:
            raise SchemaError(f"{label}.op 必须是 add/update/delete，当前 {kind!r}")
    return {"actor": actor, "reason": reason, "operations": operations}


# ---------------------------------------------------------------- 计划构建
def _writable_view(metadata: dict[str, Any]) -> dict[str, Any]:
    return {field: metadata[field] for field in WRITABLE_FIELDS}


def _sources_kinds(metadata: dict[str, Any]) -> list[str]:
    return [str(source.get("kind")) for source in metadata["sources"]]


def _validate_transition(old_metadata: dict[str, Any], writable: dict[str, Any]) -> None:
    old_status = old_metadata["status"]
    new_status = writable["status"]
    if old_status in ("retired", "rejected") and new_status == "active":
        kinds = _sources_kinds({"sources": writable["sources"]})
        if not any(kind in USER_SOURCE_KINDS for kind in kinds):
            raise ConflictError(
                f"重新启用已 {old_status} 的条目需要用户确认类来源"
                "（user_explicit/user_quote_in_task/user_confirmed），并在 reason 中记录理由",
                details={"old_status": old_status, "new_status": new_status},
            )


def _effective_writable(
    old_metadata: dict[str, Any],
    writable: dict[str, Any],
    config: MemoryConfig,
    notes: list[str],
) -> dict[str, Any]:
    effective = {field: writable[field] for field in WRITABLE_FIELDS}
    if (
        old_metadata["status"] == "candidate"
        and effective["status"] == "active"
        and effective["review_after"] is not None
        and effective["review_after"] == old_metadata["review_after"]
    ):
        effective["review_after"] = None
        notes.append("候选转正：已移除仅为候选整理设置的 review_after（事实有效期字段未改动）")
    if effective["status"] == "candidate" and effective["review_after"] is None:
        # update 把条目改成 candidate 与 add 新建 candidate 采用同一复核政策，
        # 默认按原 created_at + candidate_review_days 设置整理提示。
        effective["review_after"] = add_days(old_metadata["created_at"], config.candidate_review_days)
        notes.append("状态改为 candidate：按 created_at + candidate_review_days 设置默认复核提示 review_after")
    return effective


def _make_entry(
    writable: dict[str, Any],
    body: str,
    *,
    entry_id: str,
    revision: int,
    created_at: str,
    updated_at: str,
    last_verified_at: str | None,
    origin: str,
) -> tuple[ParsedEntry, bytes]:
    metadata = build_full_metadata(
        writable,
        entry_id=entry_id,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
        last_verified_at=last_verified_at,
    )
    validate_read_metadata(metadata, origin=origin)
    body_info = analyze_body(body, status=metadata["status"], kind=metadata["kind"])
    data = serialize_entry(metadata, body_info["canonical"])
    entry = parse_entry_bytes(data, origin=origin)
    if entry.id != entry_id:
        raise SchemaError("内部一致性检查失败：序列化后的条目 ID 与预期不一致")
    return entry, data


def _change_from_entry(
    *,
    op: str,
    entry: ParsedEntry,
    path: Path,
    rel_path: str,
    before_sha256: str | None,
    before_revision: int | None,
    before_status: str | None,
    new_bytes: bytes | None,
    verification: dict[str, Any] | None,
) -> TargetChange:
    return TargetChange(
        op=op,
        entry_id=entry.id,
        path=path,
        before_sha256=before_sha256,
        after_sha256=sha256_bytes(new_bytes) if new_bytes is not None else None,
        before_revision=before_revision,
        after_revision=entry.revision,
        before_status=before_status,
        after_status=entry.status,
        new_bytes=new_bytes,
        source_locators=[str(source.get("locator")) for source in entry.metadata["sources"]],
        verification=verification,
        rel_path=rel_path,
    )


def _candidate_default_review_after(
    writable: dict[str, Any], created_at: str, config: MemoryConfig
) -> None:
    if writable["status"] == "candidate" and writable["review_after"] is None:
        writable["review_after"] = add_days(created_at, config.candidate_review_days)


def _add_change(
    env: TxEnv,
    snapshot: StoreSnapshot,
    writable: dict[str, Any],
    body: str,
    verification: dict[str, Any] | None,
    notes: list[str],
) -> tuple[TargetChange, ParsedEntry]:
    entry_id = writable.get("id") or f"mem-{uuid.uuid4().hex}"
    writable["id"] = entry_id
    path = entry_file_path(env.paths, entry_id)
    if path.exists():
        raise ConflictError(
            f"条目 ID 已存在，add 不会静默覆盖: {entry_id}；更新请用 update 并带 expected revision/SHA",
            details={"id": entry_id},
        )
    created_at = utc_now_str()
    _candidate_default_review_after(writable, created_at, env.config)
    origin = env.paths.rel(path)
    last_verified_at = verification["verified_at"] if verification else None
    entry, data = _make_entry(
        writable,
        body,
        entry_id=entry_id,
        revision=1,
        created_at=created_at,
        updated_at=created_at,
        last_verified_at=last_verified_at,
        origin=origin,
    )
    if verification:
        notes.append(
            f"来源核实记录：{verification['source_locator']}（{verification['verified_at']}）"
        )
    change = _change_from_entry(
        op="add",
        entry=entry,
        path=path,
        rel_path=origin,
        before_sha256=None,
        before_revision=None,
        before_status=None,
        new_bytes=data,
        verification=verification,
    )
    return change, entry


def plan_add(env: TxEnv, snapshot: StoreSnapshot, request: dict[str, Any]) -> PlanOutcome:
    snapshot.ensure_complete()
    notes: list[str] = []
    writable = {field: request["writable"][field] for field in WRITABLE_FIELDS}
    change, entry = _add_change(env, snapshot, writable, request["body"], request["verification"], notes)
    after = dict(snapshot.entries)
    after[entry.id] = entry
    ensure_final_state_valid(env.paths, snapshot.entries, after)
    plan = TransactionPlan(
        action="add",
        actor=request["actor"],
        reason=request["reason"],
        changes=[change],
        notes=notes,
    )
    summary = {
        "operations": [
            {
                "op": "add",
                "id": entry.id,
                "revision_after": entry.revision,
                "status_after": entry.status,
                "sha256_after": change.after_sha256,
                "path": change.rel_path,
            }
        ],
        "notes": notes,
    }
    return PlanOutcome(plan=plan, no_change=False, summary=summary)


def _update_change(
    env: TxEnv,
    current: ParsedEntry,
    writable: dict[str, Any],
    body: str,
    verification: dict[str, Any] | None,
    notes: list[str],
    *,
    now: str,
) -> tuple[TargetChange | None, dict[str, Any]]:
    old_metadata = current.metadata
    _validate_transition(old_metadata, writable)
    effective = _effective_writable(old_metadata, writable, env.config, notes)
    body_info = analyze_body(body, status=effective["status"], kind=effective["kind"])
    substantive = (
        effective != _writable_view(old_metadata) or body_info["canonical"] != current.body
    )
    verification_changed = verification is not None
    if not substantive and not verification_changed:
        return None, {"op": "update", "id": current.id, "result": "no_change"}
    if substantive:
        revision = current.revision + 1
    else:
        revision = current.revision
    last_verified_at = (
        verification["verified_at"] if verification_changed else old_metadata["last_verified_at"]
    )
    path = entry_file_path(env.paths, current.id)
    origin = env.paths.rel(path)
    entry, data = _make_entry(
        effective,
        body_info["canonical"],
        entry_id=current.id,
        revision=revision,
        created_at=old_metadata["created_at"],
        updated_at=now,
        last_verified_at=last_verified_at,
        origin=origin,
    )
    if verification:
        notes.append(
            f"来源核实记录：{verification['source_locator']}（{verification['verified_at']}）"
        )
    change = _change_from_entry(
        op="update",
        entry=entry,
        path=path,
        rel_path=origin,
        before_sha256=current.sha256,
        before_revision=current.revision,
        before_status=current.status,
        new_bytes=data,
        verification=verification,
    )
    summary = {
        "op": "update",
        "id": entry.id,
        "result": "updated" if substantive else "verified",
        "revision_before": current.revision,
        "revision_after": entry.revision,
        "sha256_before": current.sha256,
        "sha256_after": change.after_sha256,
        "status_after": entry.status,
        "path": change.rel_path,
    }
    return change, summary


def _check_expected(
    current: ParsedEntry,
    expected_revision: int,
    expected_sha256: str,
    *,
    label: str = "条目",
) -> None:
    problems: list[str] = []
    if current.revision != expected_revision:
        problems.append(f"revision 期望 {expected_revision}，当前 {current.revision}")
    if current.sha256 != expected_sha256:
        problems.append(f"sha256 期望 {expected_sha256}，当前 {current.sha256}")
    if problems:
        raise ConflictError(
            f"{label} {current.id} 与预期版本不一致，未覆盖：" + "；".join(problems)
            + "。请重新 show 读取当前内容后再提交新请求",
            details={
                "id": current.id,
                "expected_revision": expected_revision,
                "current_revision": current.revision,
                "expected_sha256": expected_sha256,
                "current_sha256": current.sha256,
            },
        )


def plan_update(
    env: TxEnv,
    snapshot: StoreSnapshot,
    entry_id: str,
    expected_revision: int,
    expected_sha256: str,
    request: dict[str, Any],
) -> PlanOutcome:
    snapshot.ensure_complete()
    current = snapshot.entries.get(entry_id)
    if current is None:
        raise UsageError(f"条目不存在: {entry_id}")
    _check_expected(current, expected_revision, expected_sha256.lower())
    if request["writable"].get("id") != entry_id:
        raise SchemaError("update 的 metadata.id 必须与命令行 ID 一致")
    notes: list[str] = []
    writable = {field: request["writable"][field] for field in WRITABLE_FIELDS}
    now = utc_now_str()
    change, summary = _update_change(
        env, current, writable, request["body"], request["verification"], notes, now=now
    )
    if change is None:
        return PlanOutcome(plan=None, no_change=True, summary={"operations": [summary], "notes": notes})
    after = dict(snapshot.entries)
    after[entry_id] = parse_entry_bytes(change.new_bytes or b"", origin=change.rel_path)
    ensure_final_state_valid(env.paths, snapshot.entries, after)
    plan = TransactionPlan(
        action="update",
        actor=request["actor"],
        reason=request["reason"],
        changes=[change],
        notes=notes,
    )
    return PlanOutcome(
        plan=plan,
        no_change=False,
        summary={"operations": [summary], "notes": notes},
    )


def plan_apply(env: TxEnv, snapshot: StoreSnapshot, request: dict[str, Any]) -> PlanOutcome:
    snapshot.ensure_complete()
    before = dict(snapshot.entries)
    final: dict[str, ParsedEntry] = dict(snapshot.entries)
    changes: list[TargetChange] = []
    summaries: list[dict[str, Any]] = []
    notes: list[str] = []
    now = utc_now_str()

    for operation in request["operations"]:
        kind = operation["op"]
        entry_id = operation["id"]
        if kind == "add":
            writable = {field: operation["record"]["writable"][field] for field in WRITABLE_FIELDS}
            if entry_id in final:
                raise ConflictError(f"add 目标 ID 已存在: {entry_id}", details={"id": entry_id})
            change, entry = _add_change(
                env, snapshot, writable, operation["record"]["body"], operation.get("verification"), notes
            )
            if change.entry_id != entry_id:
                raise SchemaError("内部一致性检查失败：add 操作的 ID 与计划不一致")
            changes.append(change)
            final[entry_id] = entry
            summaries.append(
                {
                    "op": "add",
                    "id": entry_id,
                    "result": "created",
                    "revision_after": entry.revision,
                    "sha256_after": change.after_sha256,
                    "path": change.rel_path,
                }
            )
            continue
        if entry_id not in final:
            raise UsageError(f"操作目标不存在: {entry_id}（delete/update 都必须基于当前条目）")
        current = final[entry_id]
        _check_expected(current, operation["expected_revision"], operation["expected_sha256"])
        if kind == "delete":
            path = entry_file_path(env.paths, entry_id)
            changes.append(
                TargetChange(
                    op="delete",
                    entry_id=entry_id,
                    path=path,
                    before_sha256=current.sha256,
                    after_sha256=None,
                    before_revision=current.revision,
                    after_revision=None,
                    before_status=current.status,
                    after_status=None,
                    new_bytes=None,
                    source_locators=[],
                    verification=None,
                    rel_path=env.paths.rel(path),
                )
            )
            del final[entry_id]
            summaries.append(
                {
                    "op": "delete",
                    "id": entry_id,
                    "result": "deleted",
                    "revision_before": current.revision,
                    "sha256_before": current.sha256,
                    "path": env.paths.rel(path),
                }
            )
            continue
        # update
        writable = {field: operation["record"]["writable"][field] for field in WRITABLE_FIELDS}
        change, summary = _update_change(
            env, current, writable, operation["record"]["body"], operation.get("verification"), notes, now=now
        )
        if change is None:
            summaries.append({"op": "update", "id": entry_id, "result": "no_change"})
            continue
        changes.append(change)
        final[entry_id] = parse_entry_bytes(change.new_bytes or b"", origin=change.rel_path)
        summaries.append(summary)

    if not changes:
        return PlanOutcome(plan=None, no_change=True, summary={"operations": summaries, "notes": notes})
    ensure_final_state_valid(env.paths, before, final)
    plan = TransactionPlan(
        action="apply",
        actor=request["actor"],
        reason=request["reason"],
        changes=changes,
        notes=notes,
    )
    return PlanOutcome(
        plan=plan,
        no_change=False,
        summary={"operations": summaries, "notes": notes},
    )


__all__ = [
    "PlanOutcome",
    "load_request_file",
    "parse_add_request",
    "parse_update_request",
    "parse_apply_request",
    "parse_expected_sha",
    "parse_expected_revision",
    "plan_add",
    "plan_update",
    "plan_apply",
]
