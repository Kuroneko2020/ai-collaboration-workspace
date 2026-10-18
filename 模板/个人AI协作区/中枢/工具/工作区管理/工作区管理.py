#!/usr/bin/env python3
"""管理工作区启用、状态与记忆模式；不安装环境、不读取其他工作区。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.dont_write_bytecode = True
SCRIPT = Path(os.path.abspath(__file__))
ROOT = SCRIPT.parents[3]
TEMPLATE_INFO = Path("中枢/模板/模板信息.json")
MANAGEMENT_COMMAND = "py -3 -X utf8 中枢/工具/工作区管理/工作区管理.py"
CONFIG = Path("中枢/记忆/配置.json")
STATE = Path("中枢/启用状态.json")
LOCK = Path("中枢/.工作区管理.lock")
TOOL_DIR = ROOT / "中枢/工具/记忆工具"
BEGIN = "<!-- BEGIN GENERATED MEMORY LINKS -->"
END = "<!-- END GENERATED MEMORY LINKS -->"


class ManagementError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def plain_path(relative: Path | str) -> Path:
    path = ROOT
    for part in Path(relative).parts:
        if part in ("..", "") or Path(part).is_absolute():
            raise ManagementError("拒绝工作区以外的路径。")
        path = path / part
        if path.is_symlink() or (
            path.exists()
            and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ManagementError(f"管理路径含链接或重解析点：{relative}")
        if path.is_file() and path.stat().st_nlink > 1:
            raise ManagementError(f"管理路径含硬链接：{relative}")
    return path


def read_json(relative: Path | str) -> dict:
    path = plain_path(relative)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ManagementError(f"无法读取合法 JSON：{relative}；{exc}") from exc
    if not isinstance(data, dict):
        raise ManagementError(f"JSON 顶层必须是对象：{relative}")
    return data


def bytes_for(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def preflight() -> dict:
    for path in (*reversed(SCRIPT.parents), SCRIPT):
        if path.is_symlink() or (
            os.path.lexists(path)
            and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ManagementError("工作区根、祖先目录或管理入口含链接或重解析点，拒绝操作。")
    if sys.version_info < (3, 11):
        raise ManagementError("需要 Python 3.11 或更新版本。")
    for relative in (
        SCRIPT.relative_to(ROOT), "AGENTS.md", "README.md", "中枢/记忆/记忆.md",
        "中枢/工具/记忆工具/memory.py",
        "中枢/工具/记忆工具/memlib/config.py",
    ):
        if not plain_path(relative).is_file():
            raise ManagementError(f"模板不完整，缺少文件：{relative}")
    for path in TOOL_DIR.rglob("*.py"):
        plain_path(path.relative_to(ROOT))
    entry = (ROOT / "中枢/记忆/记忆.md").read_text(encoding="utf-8-sig")
    if entry.count(BEGIN) != 1 or entry.count(END) != 1 or entry.index(BEGIN) > entry.index(END):
        raise ManagementError("记忆入口必须含唯一且顺序正确的生成标记。")
    info = read_json(TEMPLATE_INFO)
    if info.get("schema_version") != 1 or not isinstance(info.get("template_version"), str):
        raise ManagementError("模板信息版本不合法。")
    if info.get("default_memory_mode") not in ("manual", "auto"):
        raise ManagementError("模板必须声明合法的默认记忆模式。")
    return info


def validated_config(data: dict):
    if str(TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(TOOL_DIR))
    from memlib.config import validate_config
    from memlib.errors import MemoryError as MemoryToolError
    try:
        return validate_config(data, "工作区初始化配置")
    except MemoryToolError as exc:
        raise ManagementError(str(exc)) from exc


def load_active(info: dict) -> tuple[dict, dict]:
    config = validated_config(read_json(CONFIG)).to_dict()
    state = read_json(STATE)
    required = {"schema_version", "template_version", "workspace_id", "initialized_at", "updated_at", "memory_mode", "consent_source"}
    if set(state) != required or state.get("schema_version") != 1:
        raise ManagementError("启用状态字段或版本不合法，拒绝覆盖。")
    if state["workspace_id"] != config["workspace_id"]:
        raise ManagementError("启用状态与记忆配置的身份不一致，拒绝覆盖。")
    if state["template_version"] != info["template_version"]:
        raise ManagementError("模板版本与启用状态不同；需要显式迁移，不能重新初始化。")
    if state["memory_mode"] not in ("manual", "auto"):
        raise ManagementError("记忆模式不合法。")
    for field in ("initialized_at", "updated_at"):
        try:
            stamp = datetime.fromisoformat(state[field].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("缺少时区")
        except (TypeError, ValueError, AttributeError) as exc:
            raise ManagementError(f"启用状态的 {field} 不合法。") from exc
    source = state["consent_source"]
    if source is not None and (not isinstance(source, str) or not source.strip() or len(source) > 240):
        raise ManagementError("同意来源必须是简短文字或 null。")
    if state["memory_mode"] == "auto" and not source:
        raise ManagementError("自动记忆模式没有明确同意来源。")
    return config, state


@contextmanager
def management_lock():
    lock = plain_path(LOCK)
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ManagementError("已有工作区管理锁；请确认没有其他管理进程。不要自动删除未知锁。") from exc
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink()


def status() -> dict:
    info = preflight()
    config_path = plain_path(CONFIG)
    state_path = plain_path(STATE)
    if not config_path.exists() and not state_path.exists():
        return {"ok": True, "status": "not_initialized", "template_version": info["template_version"], "memory_mode": None, "default_memory_mode": info["default_memory_mode"]}
    if config_path.exists() != state_path.exists():
        raise ManagementError("发现部分初始化状态，拒绝覆盖；请保留现有文件并检查原因。")
    config, state = load_active(info)
    return {"ok": True, "status": "ready", "workspace_id": config["workspace_id"], "template_version": state["template_version"], "memory_mode": state["memory_mode"]}


def init(mode: str | None = None) -> dict:
    info = preflight()
    selected_mode = mode if mode is not None else info["default_memory_mode"]
    if selected_mode not in ("manual", "auto"):
        raise ManagementError("初始化记忆模式不合法。")
    with management_lock():
        current = status()
        if current["status"] == "ready":
            return {**current, "result": "already_initialized"}
        for relative in ("中枢/记忆/条目", "中枢/记忆/运行", "中枢/记录/记忆变更"):
            path = plain_path(relative)
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise ManagementError(f"发现既有记忆或运行数据，不能视为空白工作区：{relative}")
        workspace_id = "workspace-" + uuid.uuid4().hex
        config = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "lock_timeout_seconds": 5,
            "query_limit": 8,
            "context_max_chars": 6000,
            "candidate_review_days": 30,
        }
        validated_config(config)
        stamp = utc_now()
        state = {
            "schema_version": 1,
            "template_version": info["template_version"],
            "workspace_id": workspace_id,
            "initialized_at": stamp,
            "updated_at": stamp,
            "memory_mode": selected_mode,
            "consent_source": (
                "启用工作区时采用已在 README 与 AGENTS 说明的模板默认普通自动记忆策略"
                if mode is None and selected_mode == "auto"
                else "初始化命令显式选择普通自动记忆策略" if selected_mode == "auto" else None
            ),
        }
        for relative in (CONFIG, STATE):
            if plain_path(relative).exists():
                raise ManagementError(f"目标已存在，拒绝覆盖：{relative}")
        for relative, data in ((CONFIG, config), (STATE, state)):
            with plain_path(relative).open("xb") as stream:
                stream.write(bytes_for(data))
                stream.flush()
                os.fsync(stream.fileno())
        for relative in ("中枢/记忆/条目", "中枢/记忆/运行", "中枢/记录/记忆变更"):
            plain_path(relative).mkdir(parents=True, exist_ok=True)
        return {**status(), "result": "initialized"}


def configure(mode: str, source: str | None) -> dict:
    if mode == "auto" and (not source or not source.strip()):
        raise ManagementError("启用自动记忆必须提供用户明确同意的简短来源，不得由程序代替用户同意。")
    if source is not None and (not source.strip() or len(source) > 240):
        raise ManagementError("同意来源应为 1 至 240 字的非敏感简短说明。")
    info = preflight()
    with management_lock():
        _, state = load_active(info)
        path = plain_path(STATE)
        before = path.read_bytes()
        normalized = source.strip() if mode == "auto" and source else None
        if state["memory_mode"] == mode and state["consent_source"] == normalized:
            return {**status(), "result": "no_change"}
        state["memory_mode"] = mode
        state["consent_source"] = normalized
        state["updated_at"] = utc_now()
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(bytes_for(state))
                stream.flush()
                os.fsync(stream.fileno())
            if hashlib.sha256(plain_path(STATE).read_bytes()).digest() != hashlib.sha256(before).digest():
                raise ManagementError("启用状态在操作中已被修改，拒绝覆盖。")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {**status(), "result": "configured"}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "configure"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "init":
            sub.add_argument("--memory-mode", choices=("manual", "auto"))
        if name == "configure":
            sub.add_argument("--memory-mode", required=True, choices=("manual", "auto"))
            sub.add_argument("--consent-source")
    return cli


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        if options.command == "init":
            result = init(options.memory_mode)
        elif options.command == "status":
            result = status()
        else:
            result = configure(options.memory_mode, options.consent_source)
    except (ManagementError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    if getattr(options, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        if result["status"] == "not_initialized":
            print(f"状态：尚未启用；首次启用默认记忆模式：{result['default_memory_mode']}。")
            print(f"请在明确选定的工作区运行：{MANAGEMENT_COMMAND} init")
            print(f"如不采用普通自动记忆：{MANAGEMENT_COMMAND} init --memory-mode manual")
        else:
            print(f"状态：{result['status']}；记忆模式：{result['memory_mode']}。")
            print(f"工作区身份：{result['workspace_id']}")
    else:
        print(f"未执行：{result['error']}", file=sys.stderr)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
