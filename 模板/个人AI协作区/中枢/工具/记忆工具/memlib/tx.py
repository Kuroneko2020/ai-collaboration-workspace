"""可恢复写事务：清单、暂存、凭据、幂等恢复与派生重建。

一次 add/update/apply 都走同一流程：

1. 取锁并检查未完成事务（调用方负责取锁与阻断检查）；
2. 校验预期版本/SHA、目标关系与事务完成后的不变量；
3. 暂存新字节 + 落盘完整清单（先暂存，后清单）；
4. 依次替换/删除目标并核对 SHA；
5. 原子写最小变更凭据（不复制正文）；
6. 标记 committed；
7. 在同一协调锁内调用**不再取锁的内部派生重建函数**；
8. 清理本事务暂存内容，保留清单（供幂等恢复判断）。

任何阶段失败都不回滚已落盘的权威数据；未完成事务由 ``recover`` 幂等补齐。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CREDENTIAL_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION, TOOL_VERSION
from .config import MemoryConfig
from .errors import (
    ConflictError,
    DerivedFailure,
    IOFailure,
    MemoryError,
    NeedsRecoveryError,
    SchemaError,
    StructureConflictError,
    UsageError,
)
from .faults import inject
from .fsutil import atomic_write_bytes, write_staged_file
from .jsonutil import loads_strict, read_text_utf8
from .pathutil import is_within, relative_posix, resolve_path
from .schema import ParsedEntry, utc_now, utc_now_str, validate_id
from .store import (
    MANIFEST_NAME,
    STAGED_DIR_NAME,
    Paths,
    ensure_write_dirs,
    entry_file_path,
    load_snapshot,
)
from . import index as index_module

VALID_PHASES = ("prepared", "applying", "data_committed", "committed")


# ---------------------------------------------------------------- 计划对象
@dataclass
class TargetChange:
    op: str  # add / update / delete
    entry_id: str
    path: Path
    before_sha256: str | None
    after_sha256: str | None
    before_revision: int | None
    after_revision: int | None
    before_status: str | None
    after_status: str | None
    new_bytes: bytes | None = None
    source_locators: list[str] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    staged_rel: str | None = None
    rel_path: str = ""

    def __post_init__(self) -> None:
        if not self.rel_path:
            self.rel_path = self.path.as_posix()


@dataclass
class TransactionPlan:
    action: str
    actor: str
    reason: str
    changes: list[TargetChange]
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"added": 0, "updated": 0, "deleted": 0}
        for change in self.changes:
            if change.op == "add":
                counts["added"] += 1
            elif change.op == "update":
                counts["updated"] += 1
            elif change.op == "delete":
                counts["deleted"] += 1
        return counts


@dataclass(frozen=True)
class TxEnv:
    paths: Paths
    config: MemoryConfig
    root: Path
    default_root: Path


# ---------------------------------------------------------------- 工具函数
def new_transaction_id(now: datetime | None = None) -> str:
    now = now or utc_now()
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"tx-{stamp}-{uuid.uuid4().hex[:12]}"


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IOFailure(f"读取目标文件失败 {path}: {exc}") from exc
    return sha256_bytes(data)


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _write_manifest(tx_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write_bytes(tx_dir / MANIFEST_NAME, _manifest_bytes(manifest))


def _credential_doc(
    env: TxEnv,
    plan: TransactionPlan,
    txid: str,
    now: str,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for change in plan.changes:
        operation: dict[str, Any] = {
            "op": change.op,
            "id": change.entry_id,
            "path": change.rel_path,
            "revision_before": change.before_revision,
            "revision_after": change.after_revision,
            "sha256_before": change.before_sha256,
            "sha256_after": change.after_sha256,
            "status_before": change.before_status,
            "status_after": change.after_status,
            "source_locators": list(change.source_locators),
        }
        if change.verification is not None:
            operation["verification"] = dict(change.verification)
        operations.append(operation)
    return {
        "schema_version": CREDENTIAL_SCHEMA_VERSION,
        "transaction_id": txid,
        "tool_version": TOOL_VERSION,
        "workspace_id": env.config.workspace_id,
        "action": plan.action,
        "actor": plan.actor,
        "reason": plan.reason,
        "timestamp": now,
        "operations": operations,
        "counts": plan.counts(),
        "note": "最小变更凭据，不含记忆正文。",
    }


def _build_manifest(
    env: TxEnv,
    plan: TransactionPlan,
    txid: str,
    now: str,
    credential: dict[str, Any],
    staged_map: dict[int, str],
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for index, change in enumerate(plan.changes):
        targets.append(
            {
                "op": change.op,
                "id": change.entry_id,
                "path": change.rel_path,
                "before_sha256": change.before_sha256,
                "after_sha256": change.after_sha256,
                "before_revision": change.before_revision,
                "after_revision": change.after_revision,
                "before_status": change.before_status,
                "after_status": change.after_status,
                "staged_file": staged_map.get(index),
                "size": len(change.new_bytes) if change.new_bytes is not None else None,
            }
        )
    credential_bytes = _manifest_bytes(credential)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "transaction_id": txid,
        "tool_version": TOOL_VERSION,
        "workspace_id": env.config.workspace_id,
        "action": plan.action,
        "actor": plan.actor,
        "reason": plan.reason,
        "phase": "prepared",
        "created_at": now,
        "targets": targets,
        "operations": [
            {"op": target["op"], "id": target["id"]} for target in targets
        ],
        "credential": {
            "path": relative_posix(env.root, env.paths.credentials_dir / f"{txid}.json"),
            "sha256": sha256_bytes(credential_bytes),
            "content": credential,
        },
        "derived_complete": False,
    }


def _target_state(change: TargetChange, current: str | None) -> str:
    """返回 old / new / third。"""

    if current == change.before_sha256 and (change.before_sha256 != change.after_sha256):
        return "old"
    if current == change.after_sha256:
        return "new"
    return "third"


def _verify_entry_path(env: TxEnv, change: TargetChange) -> Path:
    expected = entry_file_path(env.paths, change.entry_id)
    if resolve_path(expected) != resolve_path(change.path):
        raise IOFailure(f"事务目标路径异常，拒绝写入: {change.path}")
    real_entries = resolve_path(env.paths.entries_dir)
    if not is_within(resolve_path(change.path.parent), real_entries):
        raise IOFailure(f"事务目标越出条目目录，拒绝写入: {change.path}")
    return expected


def _remove_tx_dir(env: TxEnv, tx_dir: Path) -> None:
    if not is_within(tx_dir, env.paths.tx_root):
        raise IOFailure(f"拒绝删除不属于事务目录的路径: {tx_dir}")
    try:
        shutil.rmtree(tx_dir)
    except OSError as exc:
        raise IOFailure(f"清理事务目录失败 {tx_dir}: {exc}") from exc


def _cleanup_staged(env: TxEnv, tx_dir: Path, *, strict: bool = False) -> bool:
    staged = tx_dir / STAGED_DIR_NAME
    if not staged.exists():
        return False
    try:
        shutil.rmtree(staged)
        return True
    except OSError as exc:
        if strict:
            raise IOFailure(f"清理事务暂存目录失败 {staged}: {exc}") from exc
        return False


def _quarantine_orphan(env: TxEnv, record: dict[str, Any]) -> Path:
    """把缺少清单的事务目录移入 运行/orphaned/，保留材料并解除阻断。"""

    tx_dir = record["dir"]
    if not is_within(resolve_path(tx_dir), resolve_path(env.paths.tx_root)):
        raise IOFailure(f"拒绝隔离不属于事务目录的路径: {tx_dir}")
    orphan_root = env.paths.run_dir / "orphaned"
    try:
        orphan_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IOFailure(f"无法创建 orphaned 目录 {orphan_root}: {exc}") from exc
    target = orphan_root / tx_dir.name
    if target.exists():
        target = orphan_root / f"{tx_dir.name}-{utc_now_str().replace(':', '')}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(tx_dir, target)
    except OSError as exc:
        raise IOFailure(f"隔离无清单事务目录失败 {tx_dir}: {exc}") from exc
    return target


# ---------------------------------------------------------------- 事务扫描
def scan_transactions(paths: Paths) -> list[dict[str, Any]]:
    tx_root = paths.tx_root
    if not tx_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        directories = sorted(tx_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise IOFailure(f"无法扫描事务目录 {tx_root}: {exc}") from exc
    for directory in directories:
        if not directory.is_dir():
            continue
        record: dict[str, Any] = {
            "txid": directory.name,
            "dir": directory,
            "manifest": None,
            "error": None,
        }
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file():
            # 清单在一切权威文件变化之前落盘；因此缺少清单的事务目录目录按写入顺序
            # 不可能已经改动权威条目，只会残留暂存字节。标为 orphan，由 recover 隔离
            # 到 运行/orphaned/（保留材料而非删除），避免一次中断永久阻断工具。
            record["orphan"] = True
            record["error"] = "事务目录缺少 manifest.json（清单落盘前中断；未改权威文件，可由 recover 隔离）"
            records.append(record)
            continue
        try:
            raw_text = read_text_utf8(manifest_path, relative_posix(paths.root, manifest_path))
            manifest = loads_strict(raw_text, relative_posix(paths.root, manifest_path))
            if not isinstance(manifest, dict):
                raise SchemaError("事务清单必须是 JSON 对象")
            if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                raise SchemaError(f"事务清单 schema_version 不受支持: {manifest.get('schema_version')!r}")
            if manifest.get("transaction_id") != directory.name:
                raise SchemaError("事务清单 transaction_id 与目录名不一致")
            if manifest.get("phase") not in VALID_PHASES:
                raise SchemaError(f"事务清单阶段不合法: {manifest.get('phase')!r}")
            record["manifest"] = manifest
        except (SchemaError, IOFailure) as exc:
            record["error"] = str(exc)
        records.append(record)
    return records


def _needs_recovery(record: dict[str, Any]) -> bool:
    if record.get("error"):
        return True
    manifest = record.get("manifest") or {}
    if manifest.get("phase") != "committed":
        return True
    if not manifest.get("derived_complete", False):
        return True
    staged = record["dir"] / STAGED_DIR_NAME
    if staged.is_dir() and any(staged.iterdir()):
        return True
    return False


def pending_transactions(paths: Paths) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in scan_transactions(paths):
        if record.get("error"):
            result.append(record)
            continue
        manifest = record.get("manifest") or {}
        if manifest.get("phase") != "committed":
            result.append(record)
    return result


def ensure_no_pending(paths: Paths) -> None:
    pending = pending_transactions(paths)
    if pending:
        raise NeedsRecoveryError(
            [record["txid"] for record in pending],
            message="存在未完成事务，正常命令被阻断；请先执行 recover --dry-run / recover --apply",
            details={
                "transaction_ids": [record["txid"] for record in pending],
                "transaction_errors": [
                    {"txid": record["txid"], "error": record["error"]}
                    for record in pending
                    if record.get("error")
                ],
            },
        )


# ---------------------------------------------------------------- 执行事务
def execute_plan(env: TxEnv, plan: TransactionPlan, *, now: str | None = None) -> dict[str, Any]:
    if not plan.changes:
        raise UsageError("事务没有任何目标变更")
    ensure_write_dirs(env.paths)
    now = now or utc_now_str()
    txid = new_transaction_id()
    tx_dir = env.paths.tx_root / txid
    staged_dir = tx_dir / STAGED_DIR_NAME
    credential = _credential_doc(env, plan, txid, now)
    credential_bytes = _manifest_bytes(credential)
    credential_path = env.paths.credentials_dir / f"{txid}.json"
    manifest: dict[str, Any] | None = None
    applied_count = 0
    stage = "prepared"
    try:
        try:
            tx_dir.mkdir(parents=True, exist_ok=False)
            staged_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IOFailure(f"无法创建事务目录 {tx_dir}: {exc}") from exc
        staged_map: dict[int, str] = {}
        for index, change in enumerate(plan.changes):
            _verify_entry_path(env, change)
            if change.new_bytes is not None:
                staged_file = staged_dir / f"{index:03d}.bin"
                write_staged_file(staged_file, change.new_bytes)
                staged_rel = f"{STAGED_DIR_NAME}/{index:03d}.bin"
                change.staged_rel = staged_rel
                staged_map[index] = staged_rel
        manifest = _build_manifest(env, plan, txid, now, credential, staged_map)
        _write_manifest(tx_dir, manifest)
        inject("prepared", env.root, env.default_root)

        manifest["phase"] = "applying"
        _write_manifest(tx_dir, manifest)
        stage = "applying"
        inject("applying", env.root, env.default_root)

        for index, change in enumerate(plan.changes):
            stage = f"apply:{change.entry_id}"
            current = sha256_file(change.path)
            state = _target_state(change, current)
            if state != "old":
                raise ConflictError(
                    f"事务期间目标 {change.entry_id} 的状态已变化（期望 {change.before_sha256 or '不存在'}，"
                    f"实际 {current or '不存在'}），未覆盖，请重新读取后重试",
                    details={"id": change.entry_id, "expected_sha256": change.before_sha256, "current_sha256": current},
                )
            if change.op == "delete":
                try:
                    change.path.unlink()
                except FileNotFoundError:
                    raise ConflictError(f"删除目标已不存在: {change.entry_id}") from None
                except OSError as exc:
                    raise IOFailure(f"删除条目失败 {change.entry_id}: {exc}") from exc
            else:
                assert change.new_bytes is not None
                atomic_write_bytes(change.path, change.new_bytes)
            applied_count += 1
            after = sha256_file(change.path)
            if after != change.after_sha256:
                raise IOFailure(
                    f"条目 {change.entry_id} 替换后校验失败（期望 {change.after_sha256}，实际 {after}）",
                    details={"id": change.entry_id, "stage": stage},
                )
            if applied_count == 1:
                inject("after_first_replace", env.root, env.default_root)

        manifest["phase"] = "data_committed"
        _write_manifest(tx_dir, manifest)
        stage = "data_committed"
        inject("data_committed", env.root, env.default_root)
        inject("before_credential", env.root, env.default_root)

        stage = "credential"
        inject("credential_write", env.root, env.default_root)
        atomic_write_bytes(credential_path, credential_bytes)
        written_sha = sha256_file(credential_path)
        if written_sha != sha256_bytes(credential_bytes):
            raise IOFailure(f"变更凭据写入后校验失败: {credential_path}")
        inject("after_credential", env.root, env.default_root)

        manifest["phase"] = "committed"
        manifest["derived_complete"] = False
        _write_manifest(tx_dir, manifest)
        stage = "committed"
        inject("committed", env.root, env.default_root)

        stage = "derived"
        inject("derived", env.root, env.default_root, exit_code=7)
        try:
            snapshot = load_snapshot(env.paths)
            derived = index_module.sync_derived(env.paths, snapshot, env.config)
        except MemoryError as exc:
            raise DerivedFailure(
                "权威数据与变更凭据已提交，但派生视图更新失败: " + exc.message,
                details={
                    "transaction_id": txid,
                    "credential": relative_posix(env.root, credential_path),
                    "derived_status": "failed",
                    "next_action": "运行 recover --apply 只补齐派生视图；不要重复新增",
                },
            ) from exc
        except OSError as exc:
            raise DerivedFailure(
                f"权威数据与变更凭据已提交，但派生视图更新失败: {exc}",
                details={"transaction_id": txid, "credential": relative_posix(env.root, credential_path)},
            ) from exc
        manifest["derived_complete"] = True
        _write_manifest(tx_dir, manifest)
        stage = "cleanup"
        inject("cleanup", env.root, env.default_root)
        _cleanup_staged(env, tx_dir)
        return {
            "transaction_id": txid,
            "credential": relative_posix(env.root, credential_path),
            "derived": derived,
            "phase": "committed",
        }
    except MemoryError as exc:
        exc.details.setdefault("transaction_id", txid)
        exc.details.setdefault("stage", stage)
        injected = bool(exc.details.get("injected"))
        safe_to_abort = applied_count == 0 and not injected and not isinstance(exc, (DerivedFailure, NeedsRecoveryError))
        if safe_to_abort:
            try:
                _cleanup_staged(env, tx_dir)
                _remove_tx_dir(env, tx_dir)
            except MemoryError:
                pass
        elif manifest is not None and not isinstance(exc, DerivedFailure):
            try:
                manifest["last_error"] = {
                    "stage": stage,
                    "message": exc.message,
                    "timestamp": utc_now_str(),
                }
                _write_manifest(tx_dir, manifest)
            except MemoryError:
                pass
            exc.details.setdefault("next_action", "运行 recover --dry-run / recover --apply 补齐未完成事务")
        raise
    except OSError as exc:
        if applied_count == 0:
            try:
                _cleanup_staged(env, tx_dir)
                _remove_tx_dir(env, tx_dir)
            except MemoryError:
                pass
        raise IOFailure(
            f"事务 {txid} 在阶段 {stage} 发生 I/O 错误: {exc}",
            details={"transaction_id": txid, "stage": stage, "next_action": "运行 recover --dry-run 查看状态"},
        ) from exc


# ---------------------------------------------------------------- 恢复
def _plan_recovery(env: TxEnv, record: dict[str, Any]) -> dict[str, Any]:
    """只读检查一个事务的目标与凭据，返回计划；不写任何文件。"""

    manifest = record["manifest"]
    assert manifest is not None
    plan: dict[str, Any] = {
        "txid": record["txid"],
        "phase": manifest.get("phase"),
        "derived_complete": bool(manifest.get("derived_complete")),
        "actions": [],
        "blocked": False,
        "reason": None,
        "manifest": manifest,
        "dir": record["dir"],
    }
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        plan.update(blocked=True, reason="事务清单 targets 必须是非空数组，拒绝恢复")
        return plan
    for target in targets:
        if not isinstance(target, dict):
            plan.update(blocked=True, reason=f"事务清单目标必须是对象: {target!r}")
            return plan
        entry_id = target.get("id")
        rel = target.get("path")
        before = target.get("before_sha256")
        after = target.get("after_sha256")
        if not isinstance(entry_id, str) or not isinstance(rel, str):
            plan.update(blocked=True, reason=f"清单目标字段不合法: {target!r}")
            return plan
        try:
            entry_id = validate_id(entry_id, "事务清单目标 id")
        except SchemaError as exc:
            plan.update(blocked=True, reason=f"清单目标 id 非法，拒绝恢复: {exc}")
            return plan
        expected_rel = f"中枢/记忆/条目/{entry_id}.md"
        if rel != expected_rel:
            plan.update(blocked=True, reason=f"清单目标路径异常，拒绝恢复: {rel!r}")
            return plan
        path = entry_file_path(env.paths, entry_id)
        real_entries = resolve_path(env.paths.entries_dir)
        if not is_within(resolve_path(path.parent), real_entries):
            plan.update(blocked=True, reason=f"清单目标解析后越出条目目录，拒绝恢复: {entry_id}")
            return plan
        current = sha256_file(path)
        if current == before and before != after:
            action = "delete" if target.get("op") == "delete" else "write"
            if action == "write":
                staged_value = target.get("staged_file")
                if not isinstance(staged_value, str) or not staged_value.startswith(f"{STAGED_DIR_NAME}/"):
                    plan.update(blocked=True, reason=f"缺失可恢复的暂存新字节: {entry_id}")
                    return plan
                staged_path = record["dir"] / staged_value
                if not is_within(staged_path, record["dir"]) or not staged_path.is_file():
                    plan.update(blocked=True, reason=f"暂存文件不存在或越界: {entry_id} -> {staged_value}")
                    return plan
                data = staged_path.read_bytes()
                if after is not None and sha256_bytes(data) != after:
                    plan.update(blocked=True, reason=f"暂存文件内容 SHA 与清单不符: {entry_id}")
                    return plan
            plan["actions"].append({"id": entry_id, "op": target.get("op"), "action": action})
        elif current == after:
            plan["actions"].append({"id": entry_id, "op": target.get("op"), "action": "skip"})
        else:
            plan.update(
                blocked=True,
                reason=(
                    f"目标 {entry_id} 出现第三种 SHA（期望旧值 {before or '不存在'} 或新值 {after or '不存在'}，"
                    f"实际 {current or '不存在'}），停止覆盖并保留双方材料"
                ),
            )
            return plan

    credential = manifest.get("credential") or {}
    cred_rel = credential.get("path")
    cred_sha = credential.get("sha256")
    if not isinstance(cred_rel, str) or not isinstance(cred_sha, str):
        plan.update(blocked=True, reason="清单缺少凭据路径或校验值")
        return plan
    cred_path = env.root.joinpath(*cred_rel.split("/"))
    if not is_within(cred_path, env.paths.credentials_dir):
        plan.update(blocked=True, reason=f"凭据路径越界，拒绝恢复: {cred_rel!r}")
        return plan
    current_cred_sha = sha256_file(cred_path)
    if current_cred_sha is None:
        plan["actions"].append({"id": "credential", "op": "credential", "action": "write_credential"})
    elif current_cred_sha == cred_sha:
        plan["actions"].append({"id": "credential", "op": "credential", "action": "skip"})
    else:
        plan.update(
            blocked=True,
            reason=f"已存在的变更凭据 SHA 与清单不符: {cred_rel}（实际 {current_cred_sha}，清单 {cred_sha}）",
        )
        return plan
    return plan


def _apply_recovery(env: TxEnv, plan: dict[str, Any]) -> dict[str, Any]:
    manifest = plan["manifest"]
    record_dir = plan["dir"]
    changed_data = False
    for target in manifest.get("targets", []):
        entry_id = target.get("id")
        try:
            entry_id = validate_id(entry_id if isinstance(entry_id, str) else "", "恢复目标 id")
        except SchemaError as exc:
            raise IOFailure(f"恢复清单目标 id 非法，停止覆盖: {exc}") from exc
        expected_rel = f"中枢/记忆/条目/{entry_id}.md"
        if target.get("path") != expected_rel:
            raise IOFailure(f"恢复清单目标路径异常，停止覆盖: {target.get('path')!r}")
        before = target.get("before_sha256")
        after = target.get("after_sha256")
        path = entry_file_path(env.paths, entry_id)
        if not is_within(resolve_path(path.parent), resolve_path(env.paths.entries_dir)):
            raise IOFailure(f"恢复目标解析后越出条目目录，停止覆盖: {entry_id}")
        current = sha256_file(path)
        if current == after:
            continue
        if current != before:
            raise ConflictError(
                f"恢复期间目标 {entry_id} 再次变化，停止覆盖: 期望 {before or '不存在'}，实际 {current or '不存在'}",
                details={"id": entry_id, "phase": manifest.get("phase")},
            )
        if target.get("op") == "delete":
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise IOFailure(f"恢复删除失败 {entry_id}: {exc}") from exc
        else:
            staged_value = target.get("staged_file")
            staged_path = record_dir / str(staged_value)
            if not is_within(staged_path, record_dir) or not staged_path.is_file():
                raise IOFailure(f"恢复缺少暂存新字节: {entry_id}", details={"transaction_id": plan["txid"]})
            data = staged_path.read_bytes()
            if after is not None and sha256_bytes(data) != after:
                raise IOFailure(f"暂存文件 SHA 校验失败: {entry_id}", details={"transaction_id": plan["txid"]})
            atomic_write_bytes(path, data)
        verify = sha256_file(path)
        if verify != after:
            raise IOFailure(
                f"恢复后校验失败: {entry_id}（期望 {after}，实际 {verify}）",
                details={"transaction_id": plan["txid"]},
            )
        changed_data = True

    credential = manifest.get("credential") or {}
    cred_path = env.root.joinpath(*str(credential["path"]).split("/"))
    current_cred_sha = sha256_file(cred_path)
    expected_cred_sha = credential["sha256"]
    if current_cred_sha is None:
        content = credential.get("content")
        if not isinstance(content, dict):
            raise SchemaError("事务清单缺少可写入的凭据内容")
        credential_bytes = _manifest_bytes(content)
        if sha256_bytes(credential_bytes) != expected_cred_sha:
            raise SchemaError("事务清单中的凭据内容与记录的 SHA 不一致")
        atomic_write_bytes(cred_path, credential_bytes)
        if sha256_file(cred_path) != expected_cred_sha:
            raise IOFailure(f"恢复写入凭据后校验失败: {cred_path}")
    elif current_cred_sha != expected_cred_sha:
        raise ConflictError(
            f"恢复发现凭据 SHA 冲突，停止覆盖: {credential['path']}",
            details={"expected": expected_cred_sha, "actual": current_cred_sha},
        )

    if manifest.get("phase") != "committed":
        manifest["phase"] = "committed"
        manifest["derived_complete"] = False
        _write_manifest(record_dir, manifest)
    return {"changed_data": changed_data}


def recover(env: TxEnv, *, dry_run: bool) -> dict[str, Any]:
    """查看或幂等补齐未完成事务。调用方必须已经持有协调锁。"""

    records = scan_transactions(env.paths)
    work_records = [record for record in records if _needs_recovery(record)]
    report: dict[str, Any] = {
        "mode": "dry-run" if dry_run else "apply",
        "transactions": [],
        "blocked": False,
        "changed": False,
        "derived": None,
    }
    if not work_records:
        report["result"] = "no_work"
        return report
    plans: list[dict[str, Any]] = []
    for record in work_records:
        if record.get("orphan"):
            plan = {
                "txid": record["txid"],
                "phase": None,
                "derived_complete": False,
                "blocked": False,
                "reason": record.get("error"),
                "manifest": None,
                "dir": record["dir"],
                "orphan": True,
                "actions": [{"id": record["txid"], "op": "orphan", "action": "quarantine_orphan"}],
            }
            plans.append(plan)
            report["transactions"].append(
                {
                    "txid": plan["txid"],
                    "phase": None,
                    "derived_complete": False,
                    "blocked": False,
                    "reason": plan["reason"],
                    "actions": plan["actions"],
                }
            )
            continue
        if record.get("error"):
            report["transactions"].append(
                {"txid": record["txid"], "error": record["error"], "actions": []}
            )
            report["blocked"] = True
            continue
        plan = _plan_recovery(env, record)
        plans.append(plan)
        report["transactions"].append(
            {
                "txid": plan["txid"],
                "phase": plan["phase"],
                "derived_complete": plan["derived_complete"],
                "blocked": plan["blocked"],
                "reason": plan["reason"],
                "actions": plan["actions"],
            }
        )
        if plan["blocked"]:
            report["blocked"] = True
    if report["blocked"]:
        report["result"] = "blocked"
        return report
    if dry_run:
        report["result"] = "recoverable"
        return report

    changed_any = False
    quarantined: list[dict[str, str]] = []
    data_plans: list[dict[str, Any]] = []
    for plan in plans:
        if plan.get("orphan"):
            moved_to = _quarantine_orphan(env, plan)
            quarantined.append({"txid": plan["txid"], "moved_to": relative_posix(env.root, moved_to)})
            changed_any = True
            continue
        applied = _apply_recovery(env, plan)
        changed_any = changed_any or applied["changed_data"]
        plan["derived_complete"] = False
        data_plans.append(plan)
    # 同一锁内重建派生视图（不重新取锁）
    snapshot = load_snapshot(env.paths)
    try:
        derived = index_module.sync_derived(env.paths, snapshot, env.config)
    except MemoryError as exc:
        raise DerivedFailure(
            "事务数据已恢复/提交，但派生视图重建失败: " + exc.message,
            details={"derived_status": "failed", "next_action": "再次运行 recover --apply 只重建派生视图"},
        ) from exc
    except OSError as exc:
        raise DerivedFailure(
            f"事务数据已恢复/提交，但派生视图重建失败: {exc}",
            details={"derived_status": "failed"},
        ) from exc
    for plan in data_plans:
        manifest = plan["manifest"]
        manifest["derived_complete"] = True
        _write_manifest(plan["dir"], manifest)
        _cleanup_staged(env, plan["dir"])
    report["changed"] = True
    report["derived"] = derived
    report["quarantined"] = quarantined
    report["result"] = "recovered"
    return report


def describe_transaction_dir(env: TxEnv, txid: str) -> dict[str, Any] | None:
    for record in scan_transactions(env.paths):
        if record["txid"] == txid:
            return {
                "txid": txid,
                "phase": (record.get("manifest") or {}).get("phase"),
                "error": record.get("error"),
                "dir": relative_posix(env.root, record["dir"]),
            }
    return None


__all__ = [
    "TargetChange",
    "TransactionPlan",
    "TxEnv",
    "new_transaction_id",
    "sha256_bytes",
    "sha256_file",
    "scan_transactions",
    "pending_transactions",
    "ensure_no_pending",
    "execute_plan",
    "recover",
    "describe_transaction_dir",
]
