# -*- coding: utf-8 -*-
"""隔离记忆库测试辅助：临时根、独立 --root、真实子进程调用。

所有功能测试都在 ``tempfile.mkdtemp()`` 下自建 ``--root``，绝不接触生产根。
条目、来源、用户确认和偏好都是纯虚构测试夹具，不表示作者或使用者的真实事实。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目/测试/记忆工具/helpers.py
TOOL_DIR = PROJECT_ROOT / "模板" / "个人AI协作区" / "中枢" / "工具" / "记忆工具"
MEMORY_PY = TOOL_DIR / "memory.py"

# 只导入模板源码，不向模板或测试源码目录写 __pycache__；子进程也继承该约束。
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
PYTHON = sys.executable
FAULT_ENV = "MEMORY_TEST_FAULT"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from memlib.schema import WRITABLE_FIELDS  # noqa: E402

BASE_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "workspace_id": "memory-unittest-isolated",
    "lock_timeout_seconds": 5,
    "query_limit": 8,
    "context_max_chars": 6000,
    "candidate_review_days": 30,
}

MEMORY_MD_TEMPLATE = """\
# 记忆（隔离测试）

人工使用说明区，保持原样。

<!-- BEGIN GENERATED MEMORY LINKS -->
<!-- END GENERATED MEMORY LINKS -->

人工尾部区域。
"""


# ---------------------------------------------------------------- 纯工具函数
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_offset(days: int = 0, seconds: int = 0) -> str:
    return utc_iso(utc_now() + timedelta(days=days, seconds=seconds))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def make_metadata(
    entry_id: str | None = None,
    *,
    kind: str = "preference",
    subject_key: str = "test.subject",
    status: str = "active",
    level: str = "workspace",
    target: str = ".",
    scenes: list[str] | None = None,
    task_id: str | None = None,
    pinned: bool = False,
    tags: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    review_after: str | None = None,
    end_condition: str | None = None,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "kind": kind,
        "subject_key": subject_key,
        "status": status,
        "scope": {
            "level": level,
            "target": target,
            "scenes": list(scenes or []),
            "task_id": task_id,
        },
        "pinned": pinned,
        "tags": list(tags or []),
        "sources": sources
        if sources is not None
        else [
            {
                "kind": "user_explicit",
                "locator": "隔离测试输入#用户来源",
                "source_date": None,
                "observed_at": None,
            }
        ],
        "valid_from": valid_from,
        "valid_until": valid_until,
        "review_after": review_after,
        "end_condition": end_condition,
        "superseded_by": superseded_by,
    }
    if entry_id is not None:
        metadata["id"] = entry_id
    return metadata


def make_body(
    title: str = "隔离测试标题",
    conclusion: str = "隔离测试结论：这是纯虚构的中文测试数据。",
    *,
    sections: list[tuple[str, str]] | None = None,
    candidate_question: str | None = None,
    extra: str | None = None,
) -> str:
    lines = [f"# {title}", "", "## 结论", "", conclusion, ""]
    if sections is None:
        sections = [("使用边界", "仅用于隔离测试，不写入用户真实记忆。")]
    for heading, text in sections:
        lines.extend([f"## {heading}", "", text, ""])
    if candidate_question is not None:
        lines.extend(["## 待澄清问题", "", candidate_question, ""])
    if extra is not None:
        lines.extend([extra, ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def writable_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """把 show 返回的完整 metadata 转成写请求允许的可写字段。"""

    return {field: metadata[field] for field in WRITABLE_FIELDS if field in metadata}


# ---------------------------------------------------------------- 隔离根
def create_root(
    parent: Path,
    name: str = "工作 区-中文",
    *,
    config: dict[str, Any] | None = None,
    memory_md: str | None = MEMORY_MD_TEMPLATE,
) -> Path:
    root = parent / name
    (root / "中枢" / "记忆" / "条目").mkdir(parents=True)
    (root / "中枢" / "记录" / "记忆变更").mkdir(parents=True)
    merged = dict(BASE_CONFIG)
    if config:
        merged.update(config)
    (root / "中枢" / "记忆" / "配置.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if memory_md is not None:
        (root / "中枢" / "记忆" / "记忆.md").write_text(
            memory_md, encoding="utf-8", newline="\n"
        )
    return root


def run_cli(
    root: Path,
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    command = [PYTHON, "-X", "utf8", str(MEMORY_PY), "--root", str(root)]
    command.extend(str(arg) for arg in args)
    env = os.environ.copy()
    env.pop(FAULT_ENV, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or root),
        env=env,
        timeout=timeout,
    )


def parse_json_stdout(proc: subprocess.CompletedProcess[str], context: str = "") -> Any:
    text = (proc.stdout or "").strip()
    if not text:
        raise AssertionError(f"{context}: stdout 为空（期望 JSON）。stderr={proc.stderr!r}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{context}: stdout 不是合法 JSON: {exc}\nstdout={proc.stdout[:2000]!r}\nstderr={proc.stderr[:2000]!r}"
        ) from exc


# ---------------------------------------------------------------- 测试基类
class IsolatedMemoryTestCase(unittest.TestCase):
    """每个测试一个临时根；支持再建额外隔离根（如 -old 兄弟目录、junction 目标）。"""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="memory-test-"))
        self._junction_cleanups: list[Path] = []
        self._root_names = 0
        self.addCleanup(self._cleanup_base)
        self.root = self.new_root()
        self.config = dict(BASE_CONFIG)

    # -- 生命周期 ------------------------------------------------------
    def _cleanup_base(self) -> None:
        for junction in self._junction_cleanups:
            try:
                if junction.exists() or junction.is_symlink():
                    os.rmdir(junction)
            except OSError:
                pass
        shutil.rmtree(self.base, ignore_errors=True)

    def register_junction_cleanup(self, path: Path) -> None:
        self._junction_cleanups.append(path)

    def new_root(
        self,
        *,
        config: dict[str, Any] | None = None,
        memory_md: str | None = MEMORY_MD_TEMPLATE,
        name: str | None = None,
    ) -> Path:
        self._root_names += 1
        folder = name or f"工作 区-{self._root_names}"
        return create_root(self.base, folder, config=config, memory_md=memory_md)

    # -- CLI ----------------------------------------------------------
    def cli(
        self,
        root: Path,
        *args: str,
        expect: int | None = 0,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 180,
    ) -> subprocess.CompletedProcess[str]:
        proc = run_cli(root, *args, cwd=cwd, env_extra=env_extra, timeout=timeout)
        if expect is not None and proc.returncode != expect:
            raise AssertionError(
                f"命令 {' '.join(str(arg) for arg in args)} 期望退出码 {expect}，"
                f"实际 {proc.returncode}\nstdout={proc.stdout[:3000]}\nstderr={proc.stderr[:3000]}"
            )
        return proc

    def cli_json(
        self,
        root: Path,
        *args: str,
        expect: int | None = 0,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 180,
    ) -> dict[str, Any]:
        proc = self.cli(root, *args, expect=expect, cwd=cwd, env_extra=env_extra, timeout=timeout)
        data = parse_json_stdout(proc, context=" ".join(str(arg) for arg in args))
        if not isinstance(data, dict):
            raise AssertionError(f"JSON 输出必须是对象，实际 {type(data)}")
        return data

    # -- 请求文件 -----------------------------------------------------
    def write_json(self, name: str, payload: Any, *, root: Path | None = None) -> Path:
        path = (root or self.root) / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def write_raw(self, name: str, text: str, *, root: Path | None = None, bom: bool = False) -> Path:
        path = (root or self.root) / name
        data = text.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        path.write_bytes(data)
        return path

    # -- 条目操作 -----------------------------------------------------
    def show_entry(self, entry_id: str, *, root: Path | None = None) -> dict[str, Any]:
        data = self.cli_json(root or self.root, "show", entry_id, "--json")
        entry = data["entry"]
        assert entry["id"] == entry_id
        return entry

    def add(
        self,
        metadata: dict[str, Any],
        body: str,
        *,
        root: Path | None = None,
        dry_run: bool = False,
        verification: dict[str, Any] | None = None,
        expect: int | None = 0,
        request_name: str = "add-request.json",
    ) -> dict[str, Any]:
        root = root or self.root
        request: dict[str, Any] = {
            "metadata": metadata,
            "body": body,
            "reason": "隔离测试：新增条目",
            "actor": "unittest/memory-builder",
        }
        if verification is not None:
            request["verification"] = verification
        path = self.write_json(request_name, request, root=root)
        args = ["add", "--input", str(path), "--json"]
        if dry_run:
            args.append("--dry-run")
        return self.cli_json(root, *args, expect=expect)

    def update(
        self,
        entry_id: str,
        metadata: dict[str, Any],
        body: str,
        *,
        expected_revision: int | None = None,
        expected_sha256: str | None = None,
        root: Path | None = None,
        dry_run: bool = False,
        verification: dict[str, Any] | None = None,
        expect: int | None = 0,
        request_name: str = "update-request.json",
    ) -> dict[str, Any]:
        root = root or self.root
        if expected_revision is None or expected_sha256 is None:
            current = self.show_entry(entry_id, root=root)
            expected_revision = expected_revision or current["revision"]
            expected_sha256 = expected_sha256 or current["sha256"]
        request: dict[str, Any] = {
            "metadata": metadata,
            "body": body,
            "reason": "隔离测试：修正条目",
            "actor": "unittest/memory-builder",
        }
        if verification is not None:
            request["verification"] = verification
        path = self.write_json(request_name, request, root=root)
        args = [
            "update",
            entry_id,
            "--input",
            str(path),
            "--expected-revision",
            str(expected_revision),
            "--expected-sha256",
            str(expected_sha256),
            "--json",
        ]
        if dry_run:
            args.append("--dry-run")
        return self.cli_json(root, *args, expect=expect)

    def apply(
        self,
        operations: list[dict[str, Any]],
        *,
        root: Path | None = None,
        dry_run: bool = False,
        expect: int | None = 0,
        patch_name: str = "apply-patch.json",
    ) -> dict[str, Any]:
        root = root or self.root
        patch = {"reason": "隔离测试：事务操作", "actor": "unittest/memory-builder", "operations": operations}
        path = self.write_json(patch_name, patch, root=root)
        args = ["apply", "--patch-file", str(path), "--json"]
        if dry_run:
            args.append("--dry-run")
        return self.cli_json(root, *args, expect=expect)

    def search(
        self,
        query: str,
        *,
        root: Path | None = None,
        cwd: Path | None = None,
        scenes: list[str] | None = None,
        task_id: str | None = None,
        history: bool = False,
        at: str | None = None,
        limit: int | None = None,
        expect: int | None = 0,
    ) -> dict[str, Any]:
        root = root or self.root
        args = ["search", query, "--json"]
        args.extend(["--cwd", str(cwd or root)])
        for scene in scenes or []:
            args.extend(["--scene", scene])
        if task_id is not None:
            args.extend(["--task-id", task_id])
        if history:
            args.append("--history")
        if at is not None:
            args.extend(["--at", at])
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return self.cli_json(root, *args, expect=expect)

    def context(
        self,
        *,
        root: Path | None = None,
        cwd: Path | None = None,
        query: str | None = None,
        scenes: list[str] | None = None,
        task_id: str | None = None,
        max_chars: int | None = None,
        expect: int | None = 0,
    ) -> dict[str, Any]:
        root = root or self.root
        args = ["context", "--json", "--cwd", str(cwd or root)]
        if query is not None:
            args.extend(["--query", query])
        for scene in scenes or []:
            args.extend(["--scene", scene])
        if task_id is not None:
            args.extend(["--task-id", task_id])
        if max_chars is not None:
            args.extend(["--max-chars", str(max_chars)])
        return self.cli_json(root, *args, expect=expect)

    # -- 文件检查 -----------------------------------------------------
    def entry_path(self, entry_id: str, *, root: Path | None = None) -> Path:
        return (root or self.root) / "中枢" / "记忆" / "条目" / f"{entry_id}.md"

    def credential_dir(self, *, root: Path | None = None) -> Path:
        return (root or self.root) / "中枢" / "记录" / "记忆变更"

    def credentials(self, *, root: Path | None = None) -> list[Path]:
        folder = self.credential_dir(root=root)
        if not folder.is_dir():
            return []
        return sorted(path for path in folder.iterdir() if path.is_file())

    def entry_files(self, *, root: Path | None = None) -> list[Path]:
        folder = (root or self.root) / "中枢" / "记忆" / "条目"
        return sorted(path for path in folder.glob("*.md")) if folder.is_dir() else []

    def transaction_dirs(self, *, root: Path | None = None) -> list[Path]:
        folder = (root or self.root) / "中枢" / "记忆" / "运行" / "transactions"
        return sorted(path for path in folder.iterdir() if path.is_dir()) if folder.is_dir() else []

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def write_state_snapshot(self, *, root: Path | None = None) -> dict[str, str]:
        """权威数据 + 凭据的内容哈希快照（不含运行目录）。"""

        root = root or self.root
        state: dict[str, str] = {}
        for folder in (
            root / "中枢" / "记忆" / "条目",
            root / "中枢" / "记录" / "记忆变更",
        ):
            if not folder.is_dir():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    state[path.relative_to(root).as_posix()] = sha256_file(path) or ""
        return state

    def path_exists(self, relative: str, *, root: Path | None = None) -> bool:
        return ((root or self.root) / relative).exists()


__all__ = [
    "TOOL_DIR",
    "MEMORY_PY",
    "PROJECT_ROOT",
    "PYTHON",
    "FAULT_ENV",
    "BASE_CONFIG",
    "MEMORY_MD_TEMPLATE",
    "utc_now",
    "utc_iso",
    "utc_offset",
    "sha256_bytes",
    "sha256_file",
    "make_metadata",
    "make_body",
    "writable_from_metadata",
    "create_root",
    "run_cli",
    "parse_json_stdout",
    "IsolatedMemoryTestCase",
]
