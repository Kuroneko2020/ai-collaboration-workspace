#!/usr/bin/env python3
"""把已初始化的工作区升级到新发行包版本。

仅处理新旧出厂清单覆盖的模板文件；记忆条目、运行目录、记录目录、待整理目录，
以及启用状态与记忆配置的其余字段一律不动。替换与删除前完整备份到
`升级备份/<时间戳>/` 并写报告；提交前用新包记忆工具试载当前配置，不兼容即中止；
提交后复核失败自动回滚。版本推进由本脚本完成，不重新初始化、不手改运行文件。

典型用法（在能读写文件与执行命令的 Agent 会话中，按根 AGENTS 的升级规程执行）：

    py -3 -X utf8 <新包解压目录>/个人AI协作区/中枢/工具/工作区管理/升级.py --package <ZIP路径> --workspace <工作区根> --dry-run
    py -3 -X utf8 <新包解压目录>/个人AI协作区/中枢/工具/工作区管理/升级.py --package <ZIP路径> --workspace <工作区根>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPT = Path(os.path.abspath(__file__))
DEFAULT_WORKSPACE = SCRIPT.parents[3]
ARCHIVE_ROOT = "个人AI协作区"
FACTORY_REL = "中枢/模板/出厂文件清单.json"
INFO_REL = "中枢/模板/模板信息.json"
STATE_REL = "中枢/启用状态.json"
CONFIG_REL = "中枢/记忆/配置.json"
MEMORY_ENTRY_REL = "中枢/记忆/记忆.md"
TOOL_PREFIX = "中枢/工具/记忆工具/"
REQUIRED_FILES = (
    "AGENTS.md",
    "README.md",
    MEMORY_ENTRY_REL,
    "中枢/工具/记忆工具/memory.py",
    "中枢/工具/记忆工具/memlib/config.py",
)
BACKUP_NAME = "升级备份"
REPORT_NAME = "报告.md"
GENERATION_BEGIN = "<!-- BEGIN GENERATED MEMORY LINKS -->"
GENERATION_END = "<!-- END GENERATED MEMORY LINKS -->"
STATE_FIELDS = {
    "schema_version",
    "template_version",
    "workspace_id",
    "initialized_at",
    "updated_at",
    "memory_mode",
    "consent_source",
}
FLAT_BACKUP = {
    "模板信息.json": INFO_REL,
    "启用状态.json": STATE_REL,
    "出厂文件清单.json": FACTORY_REL,
}


class UpgradeError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json_bytes(raw: bytes, label: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpgradeError(f"{label} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise UpgradeError(f"{label} 顶层必须是对象。")
    return data


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def looks_like_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def plain_path(root: Path, relative: str) -> Path:
    path = root
    for part in Path(relative).parts:
        if part in ("..", "") or Path(part).is_absolute():
            raise UpgradeError(f"拒绝清单以外的相对路径：{relative}")
        path = path / part
        if looks_like_reparse(path):
            raise UpgradeError(f"路径含符号链接或重解析点：{relative}")
        if path.is_file() and path.stat().st_nlink > 1:
            raise UpgradeError(f"路径含硬链接：{relative}")
    return path


def load_factory(data: dict, label: str) -> dict:
    if data.get("schema_version") != 1 or not isinstance(data.get("files"), list):
        raise UpgradeError(f"{label} 格式不合法。")
    result: dict[str, str] = {}
    for record in data["files"]:
        if not isinstance(record, dict):
            raise UpgradeError(f"{label} 含非法记录。")
        path = record.get("path")
        digest = record.get("sha256")
        parts = Path(path or "").parts
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or path.startswith("/")
            or ".." in parts
            or Path(path).is_absolute()
        ):
            raise UpgradeError(f"{label} 含非法路径：{path!r}")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise UpgradeError(f"{label} 含非法校验值：{path}")
        if path in result:
            raise UpgradeError(f"{label} 路径重复：{path}")
        result[path] = digest.lower()
    if not result:
        raise UpgradeError(f"{label} 没有文件记录。")
    return result


def validate_workspace(root: Path) -> tuple[dict, dict]:
    if not root.is_dir():
        raise UpgradeError(f"工作区目录不存在：{root}")
    if looks_like_reparse(root):
        raise UpgradeError("工作区根不能是符号链接或重解析点。")
    for relative in (*REQUIRED_FILES, FACTORY_REL, STATE_REL, CONFIG_REL):
        if not plain_path(root, relative).is_file():
            raise UpgradeError(f"工作区缺少必需文件：{relative}")
    state = read_json_bytes(plain_path(root, STATE_REL).read_bytes(), "启用状态")
    if set(state) != STATE_FIELDS or state.get("schema_version") != 1:
        raise UpgradeError("启用状态字段或版本不合法；已初始化状态异常时先人工核对，不做升级。")
    config = read_json_bytes(plain_path(root, CONFIG_REL).read_bytes(), "记忆配置")
    if not isinstance(config.get("workspace_id"), str) or config.get("workspace_id") != state.get("workspace_id"):
        raise UpgradeError("启用状态与记忆配置的工作区身份不一致，拒绝升级。")
    factory = load_factory(read_json_bytes(plain_path(root, FACTORY_REL).read_bytes(), "出厂文件清单"), "出厂文件清单")
    return state, factory


def load_package(package: Path) -> tuple[str, dict, dict[str, bytes], bytes]:
    if not package.is_file():
        raise UpgradeError(f"发行包不存在：{package}")
    try:
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            info_name = f"{ARCHIVE_ROOT}/{INFO_REL}"
            factory_name = f"{ARCHIVE_ROOT}/{FACTORY_REL}"
            if info_name not in names or factory_name not in names:
                raise UpgradeError("发行包缺少模板信息或出厂文件清单，不是合法的新版包。")
            info = read_json_bytes(archive.read(info_name), "发行包模板信息")
            version = info.get("template_version")
            if not isinstance(version, str) or not version:
                raise UpgradeError("发行包模板信息缺少合法版本。")
            factory_raw = archive.read(factory_name)
            factory = load_factory(read_json_bytes(factory_raw, "发行包出厂清单"), "发行包出厂清单")
            payload: dict[str, bytes] = {}
            for path, digest in factory.items():
                entry = f"{ARCHIVE_ROOT}/{path}"
                if entry not in names:
                    raise UpgradeError(f"发行包缺少清单文件：{path}")
                raw = archive.read(entry)
                if sha256_hex(raw) != digest:
                    raise UpgradeError(f"发行包文件校验不一致，包可能损坏：{path}")
                payload[path] = raw
    except zipfile.BadZipFile as exc:
        raise UpgradeError(f"发行包不是合法 ZIP：{exc}") from exc
    return version, factory, payload, factory_raw


def classify(root: Path, old_factory: dict, new_factory: dict, payload: dict[str, bytes]) -> dict:
    plans: dict[str, list[str]] = {
        "update": [],
        "add": [],
        "delete": [],
        "conflict": [],
        "restore": [],
        "keep_local": [],
    }
    for path in sorted(new_factory):
        old_digest = old_factory.get(path)
        new_digest = new_factory[path]
        target = plain_path(root, path)
        local_bytes = target.read_bytes() if target.is_file() else None
        local_digest = sha256_hex(local_bytes) if local_bytes is not None else None
        if local_bytes is None:
            if old_digest is None:
                plans["add"].append(path)
            else:
                plans["restore"].append(path)
            continue
        if local_digest == new_digest:
            continue
        if old_digest is None:
            plans["conflict"].append(path)
        elif local_digest == old_digest:
            plans["update"].append(path)
        elif new_digest == old_digest:
            plans["keep_local"].append(path)
        else:
            plans["conflict"].append(path)
    for path in sorted(set(old_factory) - set(new_factory)):
        target = plain_path(root, path)
        if not target.is_file():
            continue
        local_digest = sha256_hex(target.read_bytes())
        if local_digest == old_factory[path]:
            plans["delete"].append(path)
        else:
            plans["keep_local"].append(f"{path}（上游已移除，本地有修改，保留）")
    return plans


def check_memory_compat(root: Path, payload: dict[str, bytes], staging: Path) -> None:
    tool_root = staging / "记忆工具"
    for path, raw in payload.items():
        if path.startswith(TOOL_PREFIX):
            target = tool_root / Path(path[len(TOOL_PREFIX):])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    if not (tool_root / "memlib" / "config.py").is_file():
        raise UpgradeError("新包缺少记忆工具 memlib/config.py，无法校验配置兼容性。")
    config_data = read_json_bytes(plain_path(root, CONFIG_REL).read_bytes(), "记忆配置")
    sys.path.insert(0, str(tool_root))
    try:
        try:
            from memlib.config import validate_config
        except (ImportError, OSError) as exc:
            raise UpgradeError(f"无法加载新包记忆工具：{exc}") from exc
        try:
            validate_config(config_data, "升级前配置兼容性校验")
        except Exception as exc:  # memlib 的配置错误统一转述
            raise UpgradeError(f"当前记忆配置与新包工具不兼容，中止升级：{exc}") from exc
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_backup(root: Path, backup_dir: Path, plans: dict, state: dict) -> None:
    backup_dir.mkdir(parents=True)
    for relative in plans["update"] + plans["conflict"] + plans["delete"]:
        target = plain_path(root, relative)
        if target.is_file():
            destination = backup_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(target.read_bytes())
    for name, relative in FLAT_BACKUP.items():
        shutil.copy2(plain_path(root, relative), backup_dir / name)
    (backup_dir / "启用状态-升级前副本.json").write_bytes(json_bytes(state))


def apply_plans(
    root: Path,
    payload: dict[str, bytes],
    plans: dict,
    backup_dir: Path,
    factory_raw: bytes,
    state: dict,
    new_version: str,
) -> None:
    for relative in plans["update"] + plans["conflict"] + plans["restore"] + plans["add"]:
        atomic_write(plain_path(root, relative), payload[relative])
    for relative in plans["delete"]:
        plain_path(root, relative).unlink()
    atomic_write(plain_path(root, FACTORY_REL), factory_raw)
    updated = dict(state)
    updated["template_version"] = new_version
    updated["updated_at"] = utc_now()
    atomic_write(plain_path(root, STATE_REL), json_bytes(updated))
    prune_empty_dirs(root, plans["delete"])


def prune_empty_dirs(root: Path, relatives: list[str]) -> None:
    for relative in relatives:
        current = plain_path(root, relative).parent
        while current != root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def post_verify(root: Path, new_version: str) -> None:
    state = read_json_bytes(plain_path(root, STATE_REL).read_bytes(), "启用状态")
    if set(state) != STATE_FIELDS or state.get("template_version") != new_version:
        raise UpgradeError("升级后启用状态校验失败。")
    info = read_json_bytes(plain_path(root, INFO_REL).read_bytes(), "模板信息")
    if info.get("template_version") != new_version:
        raise UpgradeError("升级后模板信息版本校验失败。")
    for relative in REQUIRED_FILES:
        if not plain_path(root, relative).is_file():
            raise UpgradeError(f"升级后缺少必需文件：{relative}")
    entry = plain_path(root, MEMORY_ENTRY_REL).read_bytes().decode("utf-8-sig")
    if entry.count(GENERATION_BEGIN) != 1 or entry.count(GENERATION_END) != 1 or entry.index(GENERATION_BEGIN) > entry.index(GENERATION_END):
        raise UpgradeError("升级后记忆入口生成标记不合法。")


def rollback(root: Path, backup_dir: Path, plans: dict) -> None:
    for name, relative in FLAT_BACKUP.items():
        source = backup_dir / name
        if source.is_file():
            atomic_write(plain_path(root, relative), source.read_bytes())
    for item in sorted(backup_dir.rglob("*")):
        if not item.is_file() or item.name in FLAT_BACKUP or item.name in (REPORT_NAME, "启用状态-升级前副本.json"):
            continue
        relative = item.relative_to(backup_dir).as_posix()
        atomic_write(plain_path(root, relative), item.read_bytes())
    for relative in plans["add"]:
        target = plain_path(root, relative)
        if target.is_file():
            target.unlink()


def refresh_memory_index(root: Path) -> str:
    command = [
        sys.executable, "-X", "utf8",
        str(plain_path(root, "中枢/工具/记忆工具/memory.py")),
        "reindex",
    ]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    try:
        completed = subprocess.run(command, cwd=str(root), env=environment, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "ok" if completed.returncode == 0 else "failed"


def render_report(result: dict) -> str:
    lines = [
        "# 升级报告",
        "",
        f"- 结果：{result['result']}",
        f"- 版本：{result['from_version']} → {result['to_version']}",
        f"- 时间：{utc_now()}",
        "",
        "| 类别 | 数量 | 路径 |",
        "| --- | --- | --- |",
    ]
    labels = {
        "update": "更新（本地未改，采用新版）",
        "add": "新增",
        "delete": "删除（上游移除且本地未改）",
        "conflict": "冲突（备份本地后采用新版）",
        "restore": "恢复缺失文件",
        "keep_local": "保留本地修改",
    }
    for key, label in labels.items():
        items = result["plans"][key]
        lines.append(f"| {label} | {len(items)} | {'、'.join(items) if items else '—'} |")
    lines += [
        "",
        f"- 备份目录：{result.get('backup_dir') or '（预演未写入）'}",
        f"- 记忆索引重建：{result.get('reindex', 'not_attempted')}",
        "",
        "记忆条目、运行文件、记录目录、待整理目录与清单外内容未参与升级；确认升级无误前请保留本备份目录。",
        "",
    ]
    return "\n".join(lines)


def run_upgrade(package: Path, workspace: Path, dry_run: bool) -> dict:
    state, old_factory = validate_workspace(workspace)
    new_version, new_factory, payload, factory_raw = load_package(package)
    from_version = state["template_version"]
    if from_version == new_version:
        return {
            "ok": True,
            "result": "no_op",
            "from_version": from_version,
            "to_version": new_version,
            "plans": {key: [] for key in ("update", "add", "delete", "conflict", "restore", "keep_local")},
            "backup_dir": None,
            "reindex": "not_attempted",
            "warnings": ["工作区已处于该版本，无需升级；如怀疑文件被改动，请对照出厂文件清单核查。"],
        }
    plans = classify(workspace, old_factory, new_factory, payload)
    staging = Path(tempfile.mkdtemp(prefix="workspace-upgrade-"))
    try:
        check_memory_compat(workspace, payload, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = {
        "ok": True,
        "result": "dry_run" if dry_run else "upgraded",
        "from_version": from_version,
        "to_version": new_version,
        "plans": plans,
        "backup_dir": None,
        "reindex": "not_attempted",
        "warnings": [],
    }
    if dry_run:
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup_dir = plain_path(workspace, BACKUP_NAME) / stamp
    write_backup(workspace, backup_dir, plans, state)
    result["backup_dir"] = str(backup_dir)
    try:
        apply_plans(workspace, payload, plans, backup_dir, factory_raw, state, new_version)
        post_verify(workspace, new_version)
    except Exception as exc:
        rollback(workspace, backup_dir, plans)
        raise UpgradeError(f"升级失败，已从备份回滚到原状态；备份目录：{backup_dir}；原因：{exc}") from exc
    result["reindex"] = refresh_memory_index(workspace)
    if result["reindex"] == "failed":
        result["warnings"].append("记忆派生索引重建未成功；可在工作区根运行 memory.py reindex 补齐，不影响已保存的记忆。")
    (backup_dir / REPORT_NAME).write_text(render_report(result), encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--package", required=True, type=Path, help="新版发行包 ZIP 路径")
    cli.add_argument("--workspace", type=Path, default=None, help="目标工作区根目录；默认按脚本自身位置定位")
    cli.add_argument("--dry-run", action="store_true", help="只生成升级计划报告，不写入任何文件")
    cli.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return cli


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    workspace = Path(os.path.abspath(options.workspace)) if options.workspace else DEFAULT_WORKSPACE
    try:
        result = run_upgrade(Path(os.path.abspath(options.package)), workspace, options.dry_run)
    except (UpgradeError, OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    if getattr(options, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print(f"未执行：{result.get('error')}", file=sys.stderr)
    else:
        counts = ", ".join(f"{key} {len(items)}" for key, items in result["plans"].items())
        if result["result"] == "no_op":
            print(f"工作区已是版本 {result['to_version']}，无需升级。")
        elif result["result"] == "dry_run":
            print(f"预演完成，未写入任何文件：{result['from_version']} → {result['to_version']}")
            print(f"计划：{counts}")
            print("确认后去掉 --dry-run 重新执行即可实际升级。")
        else:
            print(f"升级完成：{result['from_version']} → {result['to_version']}")
            print(f"明细：{counts}")
            print(f"备份目录：{result['backup_dir']}")
            print(f"记忆索引重建：{result['reindex']}")
            for warning in result["warnings"]:
                print(f"注意：{warning}", file=sys.stderr)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
