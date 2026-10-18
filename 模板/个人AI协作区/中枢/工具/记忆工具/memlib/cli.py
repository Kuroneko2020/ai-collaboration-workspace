"""命令行入口：参数解析、命令实现、输出与退出码。

JSON 模式：数据写 stdout（合法 JSON，前后无日志）；诊断/警告写 stderr。
机器输出统一区分 data_status / audit_status / index_status / git_status。
CLI 不执行 Git；git_status 恒为 not_attempted，提交由主会话按规则处理。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import TOOL_VERSION
from .config import MemoryConfig, load_config, resolve_root
from .errors import (
    EXIT_CONFLICT,
    EXIT_DERIVED,
    EXIT_IO,
    EXIT_LOCK_TIMEOUT,
    EXIT_NEEDS_RECOVERY,
    EXIT_OK,
    EXIT_USAGE,
    ConflictError,
    DerivedFailure,
    IOFailure,
    MemoryError,
    NeedsRecoveryError,
    SchemaError,
    UsageError,
)
from .lock import MemoryLock
from .pathutil import is_within, relative_posix, resolve_path
from .schema import (
    ID_MAX,
    ID_RE,
    parse_at_argument,
    utc_now,
    utc_now_str,
)
from .search import (
    QuerySpec,
    build_context,
    normalize_query,
    result_payload,
    search,
)
from .store import (
    BEGIN_MARKER,
    END_MARKER,
    Paths,
    StoreSnapshot,
    check_invariants,
    check_scope_targets_exist,
    compute_conflicts,
    entry_summary,
    is_current,
    load_entry,
    load_snapshot,
    review_due,
    scope_specificity,
    sorted_entries,
    status_label,
    validity_state,
)
from . import tx as tx_module
from .tx import TxEnv, ensure_no_pending, execute_plan, recover, scan_transactions
from . import writeops


# ================================================================ 运行时上下文
@dataclass
class AppContext:
    root: Path
    paths: Paths
    config: MemoryConfig
    default_root: Path

    def lock(self) -> MemoryLock:
        return MemoryLock(self.paths.lock_file, self.config.lock_timeout_seconds)

    def tx_env(self) -> TxEnv:
        return TxEnv(paths=self.paths, config=self.config, root=self.root, default_root=self.default_root)


# ================================================================ 输出
def _base_result(
    command: str,
    *,
    data_status: str = "ok",
    index_status: str = "unknown",
    audit_status: str = "not_attempted",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool_version": TOOL_VERSION,
        "command": command,
        "ok": True,
        "data_status": data_status,
        "audit_status": audit_status,
        "index_status": index_status,
        "git_status": "not_attempted",
        "audit": {
            "status": "not_attempted",
            "note": "CLI 不执行 Git；是否提交由主会话按记忆管理规则处理。",
        },
        "git": {"status": "not_attempted", "note": "CLI 不运行任何 git 命令。"},
    }


def _emit(result: dict[str, Any], json_mode: bool) -> int:
    exit_code = int(result.pop("_exit", EXIT_OK))
    stderr_lines = result.pop("_stderr", [])
    for line in stderr_lines:
        print(line, file=sys.stderr)
    if json_mode:
        payload = {key: value for key, value in result.items() if not key.startswith("_")}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        human = result.get("_human")
        if human:
            sys.stdout.write(str(human).rstrip("\n") + "\n")
        else:
            payload = {key: value for key, value in result.items() if not key.startswith("_")}
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return exit_code


def _emit_error(exc: MemoryError, json_mode: bool, command: str | None) -> int:
    print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
    for key, value in exc.details.items():
        if key in ("findings", "store_errors", "conflicts"):
            print(f"  {key}: {json.dumps(value, ensure_ascii=False)}", file=sys.stderr)
        elif key in ("next_action",):
            print(f"  下一步: {value}", file=sys.stderr)
    if json_mode:
        payload = {
            "schema_version": 1,
            "tool_version": TOOL_VERSION,
            "command": command,
            "ok": False,
            "data_status": exc.data_status,
            "audit_status": "not_attempted",
            "index_status": "unknown",
            "git_status": "not_attempted",
            "error": exc.to_dict(),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return exc.code


# ================================================================ 参数解析
class _ArgumentError(Exception):
    """argparse 参数错误：不直接退出，便于 --json 模式输出合法错误对象。"""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgumentError(message)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=argparse.SUPPRESS, help="工作区根（默认按 memory.py 位置推算）")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出合法 JSON 到 stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="memory.py",
        description="个人 AI 协作中心记忆工具：校验、检索、并发写入、事务恢复与派生索引。",
        epilog="退出码：0 成功；2 参数/schema；3 版本或结构冲突；4 锁超时；5 需恢复；6 I/O；7 派生失败。",
    )
    parser.add_argument("--root", default=None, help="工作区根（默认按工具自身位置推算）")
    parser.add_argument("--json", action="store_true", default=False, help="输出合法 JSON 到 stdout")
    subparsers = parser.add_subparsers(dest="command", metavar="命令")
    subparsers.required = True

    doctor = subparsers.add_parser("doctor", help="检查布局、环境、标记区、来源断链、待恢复事务与派生状态（默认不修复）")
    _add_common_flags(doctor)

    validate = subparsers.add_parser("validate", help="校验所有条目、schema、冲突、替代关系与路径")
    _add_common_flags(validate)

    show = subparsers.add_parser("show", help="完整显示一条记忆（含原文、metadata、revision、SHA-256）")
    show.add_argument("entry_id", metavar="ID")
    _add_common_flags(show)

    search_parser = subparsers.add_parser("search", help="按第 9 节词法检索适用记忆")
    search_parser.add_argument("query", metavar="QUERY")
    search_parser.add_argument("--cwd", default=None, help="查询上下文目录（默认当前进程目录，必须在工作区内）")
    search_parser.add_argument("--scene", action="append", default=[], help="请求场景，可重复")
    search_parser.add_argument("--task-id", default=None, help="task 范围匹配用的任务 ID")
    search_parser.add_argument("--history", action="store_true", help="按状态显示候选/停用/替代/否决及过期条目")
    search_parser.add_argument("--at", default=None, help="检查哪个有效时点（必须带时区，如 2026-10-17T18:00:00Z）")
    search_parser.add_argument("--limit", type=int, default=None, help="返回条数（默认取配置 query_limit）")
    _add_common_flags(search_parser)

    context_parser = subparsers.add_parser("context", help="输出本次应读取的适用记忆包（pinned + 相关命中）")
    context_parser.add_argument("--cwd", default=None)
    context_parser.add_argument("--scene", action="append", default=[])
    context_parser.add_argument("--task-id", default=None)
    context_parser.add_argument("--query", default=None)
    context_parser.add_argument("--max-chars", type=int, default=None, help="上下文预算（Unicode 字符，默认取配置）")
    context_parser.add_argument("--history", action="store_true", help=argparse.SUPPRESS)
    _add_common_flags(context_parser)

    add_parser = subparsers.add_parser("add", help="创建一条记忆（工具填充时间、revision、缺省 ID）")
    add_parser.add_argument("--input", required=True, help="UTF-8 JSON 请求文件")
    add_parser.add_argument("--dry-run", action="store_true", help="完整校验与冲突检查，但不写任何文件")
    _add_common_flags(add_parser)

    update_parser = subparsers.add_parser("update", help="局部替换当前条目（必须带预期 revision 与 SHA-256）")
    update_parser.add_argument("entry_id", metavar="ID")
    update_parser.add_argument("--input", required=True)
    update_parser.add_argument("--expected-revision", type=int, required=True)
    update_parser.add_argument("--expected-sha256", required=True)
    update_parser.add_argument("--dry-run", action="store_true")
    _add_common_flags(update_parser)

    apply_parser = subparsers.add_parser("apply", help="把明确列出的 add/update/delete 组成一项事务（合并、替代、批量修正）")
    apply_parser.add_argument("--patch-file", required=True)
    apply_parser.add_argument("--dry-run", action="store_true")
    _add_common_flags(apply_parser)

    review_parser = subparsers.add_parser("review", help="只读列出候选、复核到期、有效期到期、冲突与断链")
    _add_common_flags(review_parser)

    reindex_parser = subparsers.add_parser("reindex", help="重建派生索引与核心链接；--check 只检查新鲜度")
    reindex_parser.add_argument("--check", action="store_true", help="只检查派生视图是否新鲜，不写文件")
    _add_common_flags(reindex_parser)

    recover_parser = subparsers.add_parser("recover", help="查看或幂等补齐未完成事务（--dry-run 与 --apply 二选一）")
    recover_group = recover_parser.add_mutually_exclusive_group(required=True)
    recover_group.add_argument("--dry-run", action="store_true", help="只检查，不写文件")
    recover_group.add_argument("--apply", action="store_true", help="幂等补齐未完成事务")
    _add_common_flags(recover_parser)

    return parser


# ================================================================ 查询上下文
def _parse_at(value: str | None) -> datetime:
    if value is None:
        return utc_now()
    return parse_at_argument(value)


def _parse_cwd(value: str | None, root: Path) -> Path:
    if value:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        cwd = resolve_path(candidate)
    else:
        cwd = resolve_path(Path.cwd())
    if not is_within(cwd, root):
        raise UsageError(
            f"查询目录不在核实后的工作区内: {cwd}（工作区根 {root}）。"
            " 请用 --cwd 显式传入位于工作区内的目录；--root 是隔离/迁移根时尤其需要。"
        )
    return cwd


def _parse_scenes(values: list[str]) -> tuple[str, ...]:
    from .schema import validate_scenes

    return tuple(validate_scenes(list(values or []), "scene"))


def _parse_task_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise SchemaError("--task-id 不能为空")
    if len(text) > 200 or any(ch.isspace() or ch in "/\\\x00" for ch in text):
        raise SchemaError("--task-id 必须是 1..200 位、不含空白与路径分隔符的字符串")
    return text


def _query_spec(ctx: AppContext, args: argparse.Namespace) -> QuerySpec:
    at = _parse_at(getattr(args, "at", None))
    cwd = _parse_cwd(getattr(args, "cwd", None), ctx.root)
    scenes = _parse_scenes(getattr(args, "scene", []) or [])
    task_id = _parse_task_id(getattr(args, "task_id", None))
    return QuerySpec(
        at=at,
        cwd=cwd,
        scenes=scenes,
        task_id=task_id,
        history=bool(getattr(args, "history", False)),
    )


# ================================================================ 通用格式化
def _format_findings(findings: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        severity = finding.get("severity", "info").upper()
        ids = finding.get("ids") or []
        suffix = f" [{', '.join(ids)}]" if ids else ""
        lines.append(f"- {severity} {finding.get('code')}: {finding.get('message')}{suffix}")
    return lines


def _format_entry(payload: dict[str, Any], *, index: int | None = None) -> str:
    scope = payload.get("scope", {})
    scenes = ",".join(scope.get("scenes", []) or []) or "通用"
    prefix = f"{index}. " if index is not None else ""
    lines = [
        f"{prefix}{payload.get('title')}  [{payload.get('id')}]",
        f"   kind={payload.get('kind')} status={payload.get('status')} rev={payload.get('revision')}"
        f" 范围={scope.get('level')}:{scope.get('target')} 场景={scenes}",
        f"   状态: {payload.get('status_label')}"
        + ("  needs_review=是" if payload.get("needs_review") else "")
        + ("  历史经历" if payload.get("is_historical") else ""),
    ]
    if payload.get("score") is not None:
        lines.append(f"   相关度: {payload.get('score')}")
    lines.append(
        "   有效区间: ["
        + (payload.get("valid_from") or "未设起点")
        + ", "
        + (payload.get("valid_until") or "未设终点")
        + ")"
    )
    sources = "；".join(
        f"{source.get('kind')} @ {source.get('locator')}" for source in payload.get("sources", [])
    )
    lines.append(f"   来源: {sources}")
    lines.append(f"   权威路径: {payload.get('path')}  sha256={payload.get('sha256')}")
    conclusion = payload.get("conclusion")
    if conclusion:
        lines.append("   结论: " + conclusion.replace("\n", " "))
    return "\n".join(lines)


def _index_status(ctx: AppContext, snapshot: StoreSnapshot) -> dict[str, Any]:
    from .index import index_state

    return index_state(ctx.paths, snapshot, ctx.config)


def _store_errors(snapshot: StoreSnapshot) -> list[dict[str, Any]]:
    return list(snapshot.errors)


# ================================================================ 来源线索检查
def _check_source_locators(ctx: AppContext, snapshot: StoreSnapshot) -> list[dict[str, Any]]:
    import re

    findings: list[dict[str, Any]] = []
    uri_re = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
    missing: dict[str, list[str]] = {}
    unverified: dict[str, list[str]] = {}
    for entry in snapshot.sorted_entries():
        for source in entry.metadata["sources"]:
            locator = str(source.get("locator", ""))
            if uri_re.match(locator):
                unverified.setdefault(locator, []).append(entry.id)
                continue
            path_part = locator.split("#", 1)[0].strip()
            if not path_part or not any(sep in path_part for sep in ("/", "\\")):
                unverified.setdefault(locator, []).append(entry.id)
                continue
            candidate = Path(path_part)
            if not candidate.is_absolute():
                candidate = ctx.root / path_part
            if not is_within(candidate, ctx.root):
                unverified.setdefault(locator, []).append(entry.id)
                continue
            if not candidate.exists():
                missing.setdefault(locator, []).append(entry.id)
    for locator, ids in sorted(missing.items()):
        findings.append(
            {
                "code": "source_locator_missing",
                "severity": "warning",
                "message": f"来源本地路径不存在（线索失效不自动否定原事实）: {locator}",
                "ids": sorted(set(ids)),
            }
        )
    for locator, ids in sorted(unverified.items()):
        findings.append(
            {
                "code": "source_locator_unverified",
                "severity": "info",
                "message": f"来源线索无法机械验证（外部 URI/文本标识，不自动网络抓取）: {locator}",
                "ids": sorted(set(ids)),
            }
        )
    return findings


# ================================================================ doctor
def cmd_doctor(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    from .index import links_state

    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str, ids: list[str] | None = None, path: str | None = None) -> None:
        finding: dict[str, Any] = {"code": code, "severity": severity, "message": message, "ids": ids or []}
        if path:
            finding["path"] = path
        findings.append(finding)

    with ctx.lock():
        snapshot = load_snapshot(ctx.paths)
        index_info = _index_status(ctx, snapshot)
        at = utc_now()
        links = links_state(ctx.paths, snapshot, at)

        # 工具环境
        version = sys.version_info
        add(
            "python_version",
            "info" if version >= (3, 11) else "error",
            f"Python {version.major}.{version.minor}.{version.micro}（要求 >=3.11）",
        )
        add("platform", "info", f"平台 {os.name} / {sys.platform}；锁实现 {'msvcrt' if os.name == 'nt' else 'fcntl'}")
        add("tool_version", "info", f"记忆工具版本 {TOOL_VERSION}")
        add("workspace", "info", f"工作区根 {ctx.root}；workspace_id={ctx.config.workspace_id}")

        # 布局
        if not snapshot.entries_dir_exists:
            add("entries_dir_missing", "warning", f"条目目录尚未创建: {ctx.paths.rel(ctx.paths.entries_dir)}")
        if not ctx.paths.memory_md.is_file():
            add("memory_md_missing", "warning", f"记忆入口不存在: {ctx.paths.rel(ctx.paths.memory_md)}")
        if not ctx.paths.credentials_dir.is_dir():
            add(
                "credentials_dir_missing",
                "info",
                f"变更凭据目录尚未创建（首次写入时创建）: {ctx.paths.rel(ctx.paths.credentials_dir)}",
            )
        if not ctx.paths.run_dir.is_dir():
            add("run_dir_missing", "warning", "运行目录缺失（取锁时会创建）")

        # 生成链接区
        if links["status"] == "markers_missing":
            add(
                "generated_links_markers_missing",
                "warning",
                f"记忆入口缺少唯一的成对生成标记（{BEGIN_MARKER} / {END_MARKER}）；"
                "工具未修改该文件，需要主会话先放置标记后再 reindex",
                path=ctx.paths.rel(ctx.paths.memory_md),
            )
        elif links["status"] in ("stale", "memory_md_unreadable", "memory_md_missing"):
            add("generated_links_" + links["status"], "warning", links.get("reason", "生成链接区未就绪"))
        else:
            add("generated_links_fresh", "info", "生成链接区与当前有效条目一致")

        # 条目解析与不变量
        for error in snapshot.errors:
            add("entry_parse_error", "error", error["message"], path=error.get("path"))
        if snapshot.entries:
            findings.extend(check_invariants(snapshot.entries, ctx.paths))
            findings.extend(check_scope_targets_exist(snapshot.entries, ctx.paths))
        findings.extend(_check_source_locators(ctx, snapshot))

        # 派生状态
        if index_info["status"] != "fresh":
            add("index_" + index_info["status"], "warning", index_info["reason"], path=ctx.paths.rel(ctx.paths.index_file))
        else:
            add("index_fresh", "info", index_info["reason"])

        # 待恢复事务
        pending = tx_module.pending_transactions(ctx.paths)
        for record in pending:
            add(
                "pending_transaction",
                "error",
                f"存在未完成事务: {record['txid']}" + (f"（{record['error']}）" if record.get("error") else ""),
            )
        if not pending:
            add("no_pending_transactions", "info", "无未完成事务")

        data_status = "ok"
        if snapshot.errors:
            data_status = "degraded"
        if any(item["severity"] == "error" for item in findings):
            data_status = "degraded"
        index_status = index_info["status"]
        result = _base_result("doctor", data_status=data_status, index_status=index_status)
        result.update(
            {
                "findings": findings,
                "counts": {
                    "entries": len(snapshot.entries),
                    "parse_errors": len(snapshot.errors),
                    "pending_transactions": len(pending),
                    "errors": sum(1 for item in findings if item["severity"] == "error"),
                    "warnings": sum(1 for item in findings if item["severity"] == "warning"),
                },
                "paths": {
                    "root": str(ctx.root),
                    "entries_dir": ctx.paths.rel(ctx.paths.entries_dir),
                    "index_file": ctx.paths.rel(ctx.paths.index_file),
                    "lock_file": ctx.paths.rel(ctx.paths.lock_file),
                    "memory_md": ctx.paths.rel(ctx.paths.memory_md),
                    "credentials_dir": ctx.paths.rel(ctx.paths.credentials_dir),
                },
                "generated_links": {"markers": [BEGIN_MARKER, END_MARKER], **links},
                "derived": index_info,
            }
        )
        lines = [f"记忆工具 doctor — {ctx.root}", ""]
        lines.extend(_format_findings(findings))
        lines.append("")
        lines.append(
            f"条目 {len(snapshot.entries)}，解析错误 {len(snapshot.errors)}，"
            f"未完成事务 {len(pending)}，索引 {index_status}"
        )
        result["_human"] = "\n".join(lines)
        if pending:
            # doctor 不只做阻断：仍返回完整 findings，同时用退出码 5 表示需要 recover。
            result["data_status"] = "needs_recovery"
            result["_exit"] = EXIT_NEEDS_RECOVERY
        return result


# ================================================================ validate
def cmd_validate(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        invariants = check_invariants(snapshot.entries, ctx.paths) if snapshot.entries else []
        conflicts = compute_conflicts(ctx.paths, snapshot.entries.values())
        index_info = _index_status(ctx, snapshot)

        errors = list(snapshot.errors)
        exit_code = EXIT_OK
        if errors:
            exit_code = EXIT_USAGE
        elif invariants or conflicts:
            exit_code = EXIT_CONFLICT
        data_status = "ok" if exit_code == EXIT_OK else "invalid"
        result = _base_result("validate", data_status=data_status, index_status=index_info["status"])
        result.update(
            {
                "counts": {
                    "entries": len(snapshot.entries),
                    "parse_errors": len(errors),
                    "invariant_errors": len(invariants),
                    "conflict_groups": len(conflicts),
                },
                "errors": errors,
                "findings": invariants,
                "conflicts": conflicts,
                "entries": [
                    {
                        "id": entry.id,
                        "revision": entry.revision,
                        "status": entry.status,
                        "sha256": entry.sha256,
                        "path": entry.origin,
                        "valid_until": entry.metadata["valid_until"],
                    }
                    for entry in snapshot.sorted_entries()
                ],
                "derived": index_info,
            }
        )
        if exit_code == EXIT_OK:
            summary = "validate 通过：schema、不变量、替代关系与冲突组检查均无错误"
        elif errors:
            summary = f"validate 发现 {len(errors)} 个 schema/解析错误（退出码 2）"
        else:
            summary = (
                f"validate 发现 {len(invariants)} 个结构问题、{len(conflicts)} 个冲突组（退出码 3）"
            )
        lines = [summary, ""]
        for error in errors:
            lines.append(f"- ERROR {error.get('path')}: {error.get('message')}")
        lines.extend(_format_findings(invariants))
        for group in conflicts:
            lines.append(
                f"- CONFLICT subject_key={group['subject_key']} ids={','.join(group['ids'])} kinds={group['kinds']}"
            )
        lines.append(f"派生索引: {index_info['status']}（{index_info['reason']}）")
        result["_human"] = "\n".join(lines)
        result["_exit"] = exit_code
        result["summary"] = summary
        return result


# ================================================================ show
def cmd_show(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        entry = load_entry(ctx.paths, args.entry_id)
        snapshot = load_snapshot(ctx.paths)
        index_info = _index_status(ctx, snapshot)
        at = utc_now()
        payload = entry_summary(ctx.paths, entry, at)
        payload.update(
            {
                "metadata": entry.metadata,
                "body": entry.body,
                "raw": entry.raw_text,
                "raw_size": entry.raw_size,
            }
        )
        result = _base_result("show", index_status=index_info["status"])
        result.update(
            {
                "entry": payload,
                "needs_recovery": False,
                "derived": index_info,
                "store_errors": _store_errors(snapshot),
            }
        )
        lines = [
            f"{entry.title}  [{entry.id}] · revision {entry.revision} · sha256 {entry.sha256}",
            f"状态: {status_label(entry, at)}；kind={entry.kind}；范围={entry.scope['level']}:{entry.scope['target']}；"
            f"场景={','.join(entry.scope['scenes']) or '通用'}",
            f"有效区间: [{entry.metadata['valid_from'] or '未设起点'}, {entry.metadata['valid_until'] or '未设终点'})"
            f"；review_after={entry.metadata['review_after']}",
            f"权威路径: {ctx.paths.rel(ctx.paths.entry_path(entry.id))}",
            "-" * 60,
            entry.raw_text.rstrip("\n"),
        ]
        result["_human"] = "\n".join(lines)
        return result


# ================================================================ search
def cmd_search(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    limit = args.limit if args.limit is not None else ctx.config.query_limit
    if limit < 1 or limit > 1000:
        raise UsageError("--limit 必须在 1..1000 之间")
    query = normalize_query(args.query or "")
    if not query:
        raise UsageError("search 的 QUERY 不能为空；需要核心记忆包请用 context 子命令")
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        spec = _query_spec(ctx, args)
        index_info = _index_status(ctx, snapshot)
        found = search(ctx.paths, snapshot, spec, normalized_query=query, limit=limit)
        result = _base_result(
            "search",
            data_status="degraded" if snapshot.errors else "ok",
            index_status=index_info["status"],
        )
        result.update(found)
        result.update(
            {
                "cwd": str(spec.cwd),
                "scenes": list(spec.scenes),
                "task_id": spec.task_id,
                "at": spec.at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "derived": index_info,
                "store_errors": _store_errors(snapshot),
            }
        )
        lines = [
            f"search: {args.query!r}  tokens={found['tokens']}  matched={found['total_matched']} "
            f"returned={found['returned']}  scope={spec.cwd} scenes={list(spec.scenes) or '无'} "
            f"at={result['at']}",
        ]
        for index_number, item in enumerate(found["items"], start=1):
            lines.append("")
            lines.append(_format_entry(item, index=index_number))
        if not found["items"]:
            lines.append("")
            lines.append("（无命中；空结果是合法结果。可换同义关键词或查主题路由。）")
        if found["conflicts"]:
            lines.append("")
            lines.append("冲突提示（不能被 top-k 隐藏）：")
            for group in found["conflicts"]:
                lines.append(
                    f"- subject_key={group['subject_key']} ids={','.join(group['ids'])}："
                    + "；".join(f"{pair['a']} ↔ {pair['b']}（{pair['kind']}）" for pair in group["pairs"])
                )
        result["_human"] = "\n".join(lines)
        if snapshot.errors:
            result.setdefault("_stderr", []).append(
                f"警告: 记忆库有 {len(snapshot.errors)} 个无法解析的文件，结果不宣称完整健康；请运行 validate。"
            )
        return result


# ================================================================ context
def cmd_context(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    max_chars = args.max_chars if args.max_chars is not None else ctx.config.context_max_chars
    if max_chars < 100 or max_chars > 1_000_000:
        raise UsageError("--max-chars 必须在 100..1000000 之间")
    normalized = normalize_query(args.query or "")
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        spec = _query_spec(ctx, args)
        index_info = _index_status(ctx, snapshot)
        payload = build_context(
            ctx.paths,
            snapshot,
            spec,
            normalized_query=normalized,
            max_chars=max_chars,
            limit=ctx.config.query_limit,
        )
        result = _base_result(
            "context",
            data_status="degraded" if snapshot.errors else "ok",
            index_status=index_info["status"],
        )
        result.update(payload)
        result["derived"] = index_info
        result["store_errors"] = _store_errors(snapshot)
        lines = [payload["markdown"].rstrip("\n")]
        if payload["omitted_count"]:
            lines.append("")
            lines.append(
                f"（另有 {payload['omitted_count']} 项因预算/条数上限未展开；"
                "可用 show <id> 或提高 --max-chars 查看。）"
            )
        if snapshot.errors:
            result.setdefault("_stderr", []).append(
                f"警告: 记忆库有 {len(snapshot.errors)} 个无法解析的文件；context 结果不宣称完整健康。"
            )
        result["_human"] = "\n".join(lines)
        return result


# ================================================================ review
def cmd_review(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        index_info = _index_status(ctx, snapshot)
        at = utc_now()
        entries = snapshot.sorted_entries()
        candidates = [
            {
                "id": entry.id,
                "title": entry.title,
                "review_after": entry.metadata["review_after"],
                "review_due": review_due(entry, at),
                "scope": entry.scope,
                "path": entry.origin,
            }
            for entry in entries
            if entry.status == "candidate"
        ]
        due = [
            {
                "id": entry.id,
                "title": entry.title,
                "status": entry.status,
                "review_after": entry.metadata["review_after"],
                "needs_review": True,
            }
            for entry in entries
            if review_due(entry, at)
        ]
        expired = [
            {
                "id": entry.id,
                "title": entry.title,
                "status": entry.status,
                "valid_until": entry.metadata["valid_until"],
                "note": "已到 valid_until，普通当前查询已排除；不推断事情已完成",
            }
            for entry in entries
            if entry.status == "active"
            and entry.metadata["valid_until"] is not None
            and validity_state(entry, at)["expired"]
        ]
        conflicts = compute_conflicts(ctx.paths, entries)
        invariants = check_invariants(snapshot.entries, ctx.paths) if entries else []
        locator_findings = _check_source_locators(ctx, snapshot)
        findings: list[dict[str, Any]] = []
        for candidate in candidates:
            findings.append(
                {
                    "code": "candidate",
                    "severity": "info",
                    "message": f"候选（不是程序错误）: {candidate['id']}"
                    + ("，已到 review_after" if candidate["review_due"] else ""),
                    "ids": [candidate["id"]],
                }
            )
        for item in due:
            findings.append(
                {
                    "code": "review_due",
                    "severity": "warning",
                    "message": f"复核提示到期（不导致失效）: {item['id']}（{item['status']}）",
                    "ids": [item["id"]],
                }
            )
        for item in expired:
            findings.append(
                {
                    "code": "validity_expired",
                    "severity": "warning",
                    "message": f"已过 valid_until，退出当前上下文: {item['id']}",
                    "ids": [item["id"]],
                }
            )
        for group in conflicts:
            findings.append(
                {
                    "code": "conflict_group",
                    "severity": "error",
                    "message": f"同 subject_key 的 active 冲突组: {group['subject_key']}",
                    "ids": group["ids"],
                }
            )
        findings.extend(invariants)
        findings.extend(locator_findings)
        if index_info["status"] != "fresh":
            findings.append(
                {
                    "code": "derived_" + index_info["status"],
                    "severity": "warning",
                    "message": index_info["reason"],
                    "ids": [],
                }
            )
        result = _base_result(
            "review",
            data_status="degraded" if snapshot.errors else "ok",
            index_status=index_info["status"],
        )
        result.update(
            {
                "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "candidates": candidates,
                "review_due": due,
                "validity_expired": expired,
                "conflicts": conflicts,
                "invariants": invariants,
                "findings": findings,
                "store_errors": _store_errors(snapshot),
                "counts": {
                    "candidates": len(candidates),
                    "review_due": len(due),
                    "validity_expired": len(expired),
                    "conflict_groups": len(conflicts),
                },
                "derived": index_info,
            }
        )
        lines = [
            f"记忆审查 review — {at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"候选 {len(candidates)}；复核到期 {len(due)}；有效期到期 {len(expired)}；"
            f"冲突组 {len(conflicts)}；解析错误 {len(snapshot.errors)}",
            "",
        ]
        lines.extend(_format_findings(findings))
        if not findings:
            lines.append("（没有需要提出的复核项。）")
        result["_human"] = "\n".join(lines)
        return result


# ================================================================ add / update / apply
def _write_result(
    ctx: AppContext,
    command: str,
    outcome: writeops.PlanOutcome,
    tx_result: dict[str, Any] | None,
    *,
    dry_run: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = dict(outcome.summary)
    result_action = "applied"
    if outcome.no_change:
        result_action = "no_change"
    elif outcome.plan is not None and outcome.plan.action in ("add", "update"):
        single = summary.get("operations", [{}])[0]
        result_action = {
            "add": "created",
            "update": single.get("result", "updated"),
        }.get(outcome.plan.action, "applied")
    if dry_run:
        result_action = "dry_run"
    index_status = "fresh"
    derived: dict[str, Any] = {}
    if tx_result is not None:
        derived = tx_result.get("derived", {})
        index_status = derived.get("index_status", "fresh")
    else:
        snapshot = load_snapshot(ctx.paths)
        index_status = _index_status(ctx, snapshot)["status"]
    result = _base_result(command, data_status="ok", index_status=index_status)
    result.update(
        {
            "result": result_action,
            "dry_run": dry_run,
            "no_change": outcome.no_change,
            "would_change": (not outcome.no_change) and dry_run,
            "operations": summary.get("operations", []),
            "notes": summary.get("notes", []),
            "transaction_id": None if tx_result is None else tx_result.get("transaction_id"),
            "credential": None if tx_result is None else tx_result.get("credential"),
            "derived": derived,
            "findings": [],
        }
    )
    if derived and derived.get("links_status") not in (None, "updated", "fresh", "skipped"):
        result["findings"].append(
            {
                "code": "generated_links_" + str(derived.get("links_status")),
                "severity": "warning",
                "message": derived.get("links_reason", "生成链接区未刷新"),
                "ids": [],
            }
        )
    if extra:
        result.update(extra)
    lines = [f"{command}: {result_action}" + ("（dry-run，未写任何文件）" if dry_run else "")]
    for operation in result["operations"]:
        lines.append(
            f"- {operation.get('op')} {operation.get('id')}"
            + (
                f" revision {operation.get('revision_before')} -> {operation.get('revision_after')}"
                if operation.get("revision_after") is not None
                else ""
            )
            + (f" result={operation.get('result')}" if operation.get("result") else "")
        )
    for note in result["notes"]:
        lines.append(f"- 说明: {note}")
    if tx_result is not None:
        lines.append(f"- transaction: {tx_result['transaction_id']}")
        lines.append(f"- 变更凭据: {tx_result['credential']}（不含正文）")
        if derived:
            lines.append(
                f"- 派生: index={derived.get('index_status')} links={derived.get('links_status')}"
            )
        lines.append("- Git: not_attempted（由主会话按规则集中提交）")
    if dry_run:
        lines.append("- dry-run 只做完整校验与冲突检查，没有写条目、事务清单或凭据。")
    for finding in result["findings"]:
        lines.append(f"- WARNING {finding['message']}")
    result["_human"] = "\n".join(lines)
    return result


def cmd_add(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    request = writeops.parse_add_request(args.input)
    env = ctx.tx_env()
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        outcome = writeops.plan_add(env, snapshot, request)
        if args.dry_run:
            return _write_result(ctx, "add", outcome, None, dry_run=True)
        assert outcome.plan is not None
        tx_result = execute_plan(env, outcome.plan)
        return _write_result(ctx, "add", outcome, tx_result, dry_run=False)


def cmd_update(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    entry_id = args.entry_id
    if not isinstance(entry_id, str) or len(entry_id) > ID_MAX or not ID_RE.fullmatch(entry_id):
        raise SchemaError(f"非法条目 ID: {entry_id!r}")
    expected_sha = writeops.parse_expected_sha(args.expected_sha256, "--expected-sha256")
    expected_revision = writeops.parse_expected_revision(args.expected_revision, "--expected-revision")
    request = writeops.parse_update_request(args.input, cli_id=entry_id)
    env = ctx.tx_env()
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        if snapshot.errors:
            snapshot.ensure_complete()
        outcome = writeops.plan_update(env, snapshot, entry_id, expected_revision, expected_sha, request)
        if args.dry_run or outcome.no_change:
            return _write_result(ctx, "update", outcome, None, dry_run=bool(args.dry_run))
        assert outcome.plan is not None
        tx_result = execute_plan(env, outcome.plan)
        return _write_result(ctx, "update", outcome, tx_result, dry_run=False)


def cmd_apply(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    request = writeops.parse_apply_request(args.patch_file)
    env = ctx.tx_env()
    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        if snapshot.errors:
            snapshot.ensure_complete()
        outcome = writeops.plan_apply(env, snapshot, request)
        if args.dry_run or outcome.no_change:
            return _write_result(ctx, "apply", outcome, None, dry_run=bool(args.dry_run))
        assert outcome.plan is not None
        tx_result = execute_plan(env, outcome.plan)
        return _write_result(ctx, "apply", outcome, tx_result, dry_run=False)


# ================================================================ reindex
def cmd_reindex(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    from .index import check_derived, sync_derived

    with ctx.lock():
        ensure_no_pending(ctx.paths)
        snapshot = load_snapshot(ctx.paths)
        at = utc_now()
        if args.check:
            report = check_derived(ctx.paths, snapshot, ctx.config, at=at)
            index_status = report["index"]["status"]
            data_status = "degraded" if snapshot.errors else "ok"
            result = _base_result("reindex", data_status=data_status, index_status=index_status)
            result.update({"check": True, "fresh": report["fresh"], "derived": report})
            # --check 是新鲜度检查，不是写事务失败：陈旧用 fresh=false 表达并返回 0；
            # 退出码 7 留给 reindex 实际无法完成派生刷新（缺标记/入口不可读等）。
            result["_exit"] = EXIT_OK
            lines = [
                f"reindex --check: {'fresh（无需重建）' if report['fresh'] else '需要重建（fresh=false，运行 reindex 修复）'}",
                f"- 索引: {report['index']['status']} — {report['index']['reason']}",
                f"- 生成链接区: {report['links']['status']} — {report['links'].get('reason', '')}",
            ]
            result["_human"] = "\n".join(lines)
            return result
        derived = sync_derived(ctx.paths, snapshot, ctx.config, at=at)
        index_status = derived["index_status"]
        findings: list[dict[str, Any]] = []
        if derived["links_status"] == "markers_missing":
            findings.append(
                {
                    "code": "generated_links_markers_missing",
                    "severity": "warning",
                    "message": derived["links_reason"],
                    "ids": [],
                }
            )
        elif derived["links_status"] in ("stale", "memory_md_unreadable", "memory_md_missing"):
            findings.append(
                {
                    "code": "generated_links_" + derived["links_status"],
                    "severity": "warning",
                    "message": derived["links_reason"],
                    "ids": [],
                }
            )
        data_status = "degraded" if snapshot.errors else "ok"
        links_complete = derived["links_status"] in ("fresh", "updated", "skipped")
        fresh = index_status == "fresh" and links_complete
        result = _base_result("reindex", data_status=data_status, index_status=index_status)
        result.update({"check": False, "fresh": fresh, "derived": derived, "findings": findings})
        if not fresh:
            result["_exit"] = EXIT_DERIVED
        lines = [
            f"reindex {'完成' if fresh else '未完成派生刷新（退出码 7）'}",
            f"- 派生索引: {derived['index_status']}（{'已重建' if derived['index_written'] else '已是最新，未重写'}）",
            f"- 生成链接区: {derived['links_status']}（{'已刷新' if derived['links_written'] else '无需/未修改'}）",
        ]
        if derived["links_status"] == "markers_missing":
            lines.append(f"- 需要主会话处理: {derived['links_reason']}")
        for finding in findings:
            lines.append(f"- WARNING {finding['message']}")
        result["_human"] = "\n".join(lines)
        return result


# ================================================================ recover
def cmd_recover(ctx: AppContext, args: argparse.Namespace) -> dict[str, Any]:
    env = ctx.tx_env()
    dry_run = bool(args.dry_run)
    with ctx.lock():
        report = recover(env, dry_run=dry_run)
        if report["blocked"]:
            result = _base_result(
                "recover",
                data_status="conflict",
                index_status="unknown",
            )
            result.update({"result": "blocked", "mode": report["mode"], **_recovery_report(report)})
            result["_exit"] = EXIT_CONFLICT
            lines = ["recover: 发现无法安全恢复的冲突，已停止覆盖并保留双方材料（退出码 3）"]
            for transaction in report["transactions"]:
                lines.append(
                    f"- {transaction['txid']}: phase={transaction.get('phase')} "
                    f"blocked={transaction.get('blocked')} reason={transaction.get('reason')}"
                )
            result["_human"] = "\n".join(lines)
            return result
        if dry_run:
            result = _base_result("recover", data_status="needs_recovery", index_status="unknown")
            result.update({"result": report["result"], "mode": "dry-run", **_recovery_report(report)})
            result["_exit"] = EXIT_OK
            lines = ["recover --dry-run：只检查，不写任何文件"]
            for transaction in report["transactions"]:
                lines.append(
                    f"- {transaction['txid']} phase={transaction.get('phase')} "
                    f"actions={transaction.get('actions')}"
                )
            if report["result"] == "no_work":
                lines.append("- 没有未完成事务。")
            result["_human"] = "\n".join(lines)
            return result
        index_status = "fresh" if report.get("derived") else "unknown"
        result = _base_result("recover", data_status="ok", index_status=index_status)
        result.update({"result": report["result"], "mode": "apply", **_recovery_report(report)})
        lines = ["recover --apply 完成"]
        for transaction in report["transactions"]:
            lines.append(
                f"- {transaction['txid']} phase={transaction.get('phase')} actions={transaction.get('actions')}"
            )
        if report.get("derived"):
            lines.append(
                f"- 派生重建: index={report['derived'].get('index_status')} "
                f"links={report['derived'].get('links_status')}"
            )
        if report["result"] == "no_work":
            lines.append("- 没有未完成事务；未产生新的凭据或版本。")
        result["_human"] = "\n".join(lines)
        return result


def _recovery_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "transactions": report["transactions"],
        "blocked": bool(report.get("blocked")),
        "changed": report["changed"],
        "derived": report.get("derived"),
        "quarantined": report.get("quarantined", []),
    }


# ================================================================ 主入口
HANDLERS: dict[str, Callable[[AppContext, argparse.Namespace], dict[str, Any]]] = {
    "doctor": cmd_doctor,
    "validate": cmd_validate,
    "show": cmd_show,
    "search": cmd_search,
    "context": cmd_context,
    "add": cmd_add,
    "update": cmd_update,
    "apply": cmd_apply,
    "review": cmd_review,
    "reindex": cmd_reindex,
    "recover": cmd_recover,
}


def main(argv: list[str] | None = None, *, default_root: Path | None = None) -> int:
    parser = build_parser()
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    json_hint = "--json" in argv_list
    try:
        args = parser.parse_args(argv_list)
    except _ArgumentError as exc:
        message = str(exc)
        parser.print_usage(sys.stderr)
        print(f"错误[{EXIT_USAGE}]: 参数错误: {message}", file=sys.stderr)
        if json_hint:
            command = next((item for item in argv_list if item in HANDLERS), None)
            payload = {
                "schema_version": 1,
                "tool_version": TOOL_VERSION,
                "command": command,
                "ok": False,
                "data_status": "invalid",
                "audit_status": "not_attempted",
                "index_status": "unknown",
                "git_status": "not_attempted",
                "error": {
                    "code": EXIT_USAGE,
                    "kind": "ArgumentError",
                    "message": message,
                    "details": {},
                },
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return EXIT_USAGE
    except SystemExit as exc:  # --help / --version 等正常退出
        return int(exc.code or EXIT_OK)
    command = getattr(args, "command", None)
    json_mode = bool(getattr(args, "json", False))
    try:
        if default_root is None:
            # 本模块位于 <根>/中枢/工具/记忆工具/memlib/cli.py；正常入口 memory.py
            # 会显式传入 parents[3]，这里作为兜底同样求到工作区根。
            default_root = Path(__file__).resolve().parents[4]
        root = resolve_root(getattr(args, "root", None), default_root)
        config = load_config(root)
        paths = Paths.build(root)
        ctx = AppContext(root=root, paths=paths, config=config, default_root=resolve_path(default_root))
        handler = HANDLERS[command]
        result = handler(ctx, args)
        return _emit(result, json_mode)
    except NeedsRecoveryError as exc:
        return _emit_error(exc, json_mode, command)
    except MemoryError as exc:
        return _emit_error(exc, json_mode, command)
    except KeyboardInterrupt:
        print("已中断；已落盘的权威数据不回滚，可运行 recover --dry-run 检查。", file=sys.stderr)
        return EXIT_IO
    except OSError as exc:
        return _emit_error(IOFailure(f"I/O 错误: {exc}"), json_mode, command)
    except Exception as exc:  # 未预期错误：保守返回 I/O 错误码并给出类型
        print(f"内部错误[{type(exc).__name__}]: {exc}", file=sys.stderr)
        return EXIT_IO


__all__ = ["main", "build_parser", "AppContext"]
