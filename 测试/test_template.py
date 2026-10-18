"""只使用临时目录的模板端到端测试，不初始化模板源码或个人工作区。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "模板/个人AI协作区"
BUILD = PROJECT / "工具/打包.py"
CONFIG = Path("中枢/记忆/配置.json")
STATE = Path("中枢/启用状态.json")
MEMORY = Path("中枢/工具/记忆工具/memory.py")
MANAGER = Path("中枢/工具/工作区管理/工作区管理.py")
TEMPLATE_INFO = Path("中枢/模板/模板信息.json")
FACTORY_MANIFEST = "个人AI协作区/中枢/模板/出厂文件清单.json"


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-template-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "全新 协作区"
        shutil.copytree(SOURCE, self.root)
        self.environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        self.environment.pop("MEMORY_TEST_FAULT", None)

    def invoke(self, script, *arguments, cwd=None, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(script), *map(str, arguments)],
            cwd=cwd or self.root, env=self.environment,
            capture_output=True, text=True, encoding="utf-8", timeout=90,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def initialize(self, *arguments, code=0, root=None):
        target = root or self.root
        return self.invoke(target / MANAGER, *arguments, "--json", cwd=target, code=code)

    def memory(self, command, *arguments, code=0, root=None, cwd=None):
        target = root or self.root
        return self.invoke(target / MEMORY, command, *arguments, "--json", cwd=cwd or target, code=code)

    def request(self, name, *, old=False):
        # 此处偏好仅为隔离测试的虚构数据，不代表任何真实使用者。
        body = "隔离测试偏好为青色。" if old else "隔离测试偏好为琥珀色。"
        data = {
            "metadata": {
                "id": "mem-template-smoke",
                "kind": "preference",
                "subject_key": "test.template.preference",
                "status": "active",
                "scope": {"level": "workspace", "target": ".", "scenes": ["general"], "task_id": None},
                "pinned": False,
                "tags": ["隔离测试"],
                "sources": [{"kind": "user_explicit", "locator": "端到端测试中的虚构用户输入", "source_date": None, "observed_at": None}],
                "valid_from": None, "valid_until": None, "review_after": None,
                "end_condition": None, "superseded_by": None,
            },
            "body": f"# 隔离测试偏好\n\n## 结论\n\n{body}\n\n## 使用边界\n\n仅测试，不是真实用户偏好。\n",
            "reason": "验证干净模板的写入与纠正",
            "actor": "template-e2e",
        }
        path = self.base / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def build(self, *, source=None, code=0, check=False):
        arguments = ["--source", source or self.root, "--output-dir", self.base / "发行", "--json"]
        if check:
            arguments.append("--check")
        return self.invoke(BUILD, *arguments, code=code)

    def test_root_layout_stays_minimal_after_initialization(self):
        expected = {".gitignore", "AGENTS.md", "README.md", "中枢", "待整理"}
        self.assertEqual({path.name for path in self.root.iterdir()}, expected)
        self.assertTrue((self.root / MANAGER).is_file())
        self.assertTrue((self.root / TEMPLATE_INFO).is_file())
        self.initialize("init")
        self.assertEqual({path.name for path in self.root.iterdir()}, expected)

    def test_manager_locates_workspace_from_foreign_current_directory(self):
        first = self.invoke(self.root / MANAGER, "init", "--json", cwd=self.base)
        self.assertEqual(first["memory_mode"], "auto")
        self.assertTrue((self.root / CONFIG).exists())
        self.assertFalse((self.base / CONFIG).exists())
        self.assertEqual(self.invoke(self.root / MANAGER, "status", "--json", cwd=self.base)["workspace_id"], first["workspace_id"])

    def test_manager_rejects_linked_management_directory(self):
        directory = self.root / MANAGER.parent
        external = self.base / "外部管理工具"
        shutil.move(str(directory), external)
        self.make_directory_link(directory, external)
        self.initialize("init", code=2)
        self.assertFalse((self.root / CONFIG).exists())

    def test_status_is_read_only_before_initialization(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.initialize("status")
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(result["status"], "not_initialized")
        self.assertIsNone(result["memory_mode"])
        self.assertEqual(result["default_memory_mode"], "auto")
        self.assertEqual(before, after)
        self.assertFalse((self.root / CONFIG).exists())
        self.assertFalse((self.root / "中枢/记忆/运行").exists())

    def test_initializes_empty_auto_workspace(self):
        result = self.initialize("init")
        config = json.loads((self.root / CONFIG).read_text(encoding="utf-8"))
        state = json.loads((self.root / STATE).read_text(encoding="utf-8"))
        self.assertEqual(result["result"], "initialized")
        self.assertEqual(state["memory_mode"], "auto")
        self.assertIn("模板默认", state["consent_source"])
        self.assertEqual(state["workspace_id"], config["workspace_id"])
        self.assertEqual(len(config), 6)
        self.assertEqual(list((self.root / "中枢/记忆/条目").iterdir()), [])
        context = self.memory("context", "--cwd", self.root, "--scene", "general")
        self.assertEqual(context["items"], [])
        self.assertTrue(context["ok"])
        self.memory("validate")

    def test_repeated_initialization_preserves_identity_and_bytes(self):
        first = self.initialize("init")
        before = [(self.root / p).read_bytes() for p in (CONFIG, STATE)]
        second = self.initialize("init")
        self.assertEqual(first["workspace_id"], second["workspace_id"])
        self.assertEqual(second["result"], "already_initialized")
        self.assertEqual(before, [(self.root / p).read_bytes() for p in (CONFIG, STATE)])

    def test_first_init_can_opt_out_to_manual_without_reinitialization_reset(self):
        first = self.initialize("init", "--memory-mode", "manual")
        self.assertEqual(first["memory_mode"], "manual")
        before = (self.root / STATE).read_bytes()
        again = self.initialize("init")
        self.assertEqual(again["memory_mode"], "manual")
        self.assertEqual(again["workspace_id"], first["workspace_id"])
        self.assertEqual(before, (self.root / STATE).read_bytes())

    def test_declared_template_default_is_used_instead_of_hardcoded_mode(self):
        path = self.root / TEMPLATE_INFO
        info = json.loads(path.read_text(encoding="utf-8"))
        info["default_memory_mode"] = "manual"
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.initialize("status")["default_memory_mode"], "manual")
        self.assertEqual(self.initialize("init")["memory_mode"], "manual")

    def test_invalid_declared_default_blocks_init_without_files(self):
        path = self.root / TEMPLATE_INFO
        info = json.loads(path.read_text(encoding="utf-8"))
        info["default_memory_mode"] = "unknown"
        path.write_text(json.dumps(info), encoding="utf-8")
        self.initialize("init", code=2)
        self.assertFalse((self.root / CONFIG).exists())
        self.assertFalse((self.root / STATE).exists())

    def test_two_copies_get_different_identities(self):
        other = self.base / "第二个独立副本"
        shutil.copytree(SOURCE, other)
        first = self.initialize("init")
        second = self.initialize("init", root=other)
        self.assertNotEqual(first["workspace_id"], second["workspace_id"])

    def test_auto_requires_explicit_source_and_can_be_disabled(self):
        self.initialize("init", "--memory-mode", "manual")
        before = (self.root / STATE).read_bytes()
        result = self.initialize("configure", "--memory-mode", "auto", code=2)
        self.assertFalse(result["ok"])
        self.assertEqual(before, (self.root / STATE).read_bytes())
        result = self.initialize("configure", "--memory-mode", "auto", "--consent-source", "隔离测试明确启用")
        self.assertEqual(result["memory_mode"], "auto")
        self.assertEqual(self.initialize("init")["memory_mode"], "auto")
        result = self.initialize("configure", "--memory-mode", "manual")
        self.assertEqual(result["memory_mode"], "manual")
        self.assertIsNone(json.loads((self.root / STATE).read_text(encoding="utf-8"))["consent_source"])

    def test_configure_before_init_cannot_create_partial_state(self):
        self.initialize("configure", "--memory-mode", "manual", code=2)
        self.assertFalse((self.root / CONFIG).exists())
        self.assertFalse((self.root / STATE).exists())

    def test_partial_state_refuses_reinitialization(self):
        (self.root / CONFIG).write_text("{}", encoding="utf-8")
        original = (self.root / CONFIG).read_bytes()
        result = self.initialize("init", code=2)
        self.assertFalse(result["ok"])
        self.assertEqual(original, (self.root / CONFIG).read_bytes())
        self.assertFalse((self.root / STATE).exists())

    def test_existing_memory_without_identity_is_not_overwritten(self):
        entries = self.root / "中枢/记忆/条目"
        entries.mkdir()
        (entries / "keep.md").write_text("保留原件", encoding="utf-8")
        self.initialize("init", code=2)
        self.assertEqual((entries / "keep.md").read_text(encoding="utf-8"), "保留原件")
        self.assertFalse((self.root / CONFIG).exists())

    def test_identity_mismatch_is_rejected(self):
        self.initialize("init")
        state = json.loads((self.root / STATE).read_text(encoding="utf-8"))
        state["workspace_id"] = "other-workspace"
        (self.root / STATE).write_text(json.dumps(state), encoding="utf-8")
        self.initialize("status", code=2)
        self.initialize("init", code=2)

    def test_malformed_config_is_not_reset(self):
        self.initialize("init")
        (self.root / CONFIG).write_text("{broken", encoding="utf-8")
        before = (self.root / CONFIG).read_bytes()
        self.initialize("init", code=2)
        self.assertEqual(before, (self.root / CONFIG).read_bytes())

    def test_missing_markers_blocks_init_before_state_creation(self):
        path = self.root / "中枢/记忆/记忆.md"
        path.write_text("# 没有标记\n", encoding="utf-8")
        self.initialize("init", code=2)
        self.assertFalse((self.root / CONFIG).exists())

    def test_existing_lock_is_not_removed(self):
        path = self.root / "中枢/.工作区管理.lock"
        path.write_text("unknown-owner", encoding="utf-8")
        self.initialize("init", code=2)
        self.assertEqual(path.read_text(encoding="utf-8"), "unknown-owner")

    def test_no_implicit_tool_initialization(self):
        self.memory("validate", code=2)
        self.assertFalse((self.root / CONFIG).exists())

    def test_add_read_correct_and_reindex_in_new_processes(self):
        self.initialize("init")
        created = self.memory("add", "--input", self.request("add.json", old=True))
        self.assertEqual(created["result"], "created")
        first = self.memory("show", "mem-template-smoke")["entry"]
        context = self.memory("context", "--cwd", self.root, "--scene", "general", "--query", "隔离测试")
        self.assertEqual(len(context["items"]), 1)
        self.assertIn("青色", json.dumps(context, ensure_ascii=False))
        update = self.request("update.json")
        self.memory("update", "mem-template-smoke", "--input", update,
                    "--expected-revision", first["revision"], "--expected-sha256", first["sha256"])
        current = self.memory("context", "--cwd", self.root, "--scene", "general", "--query", "隔离测试")
        current_text = json.dumps(current, ensure_ascii=False)
        self.assertIn("琥珀色", current_text)
        self.assertNotIn("青色", current_text)
        self.memory("reindex")
        self.assertTrue(self.memory("reindex", "--check")["fresh"])
        self.memory("validate")

    def test_moved_workspace_keeps_identity_and_default_root(self):
        identity = self.initialize("init")["workspace_id"]
        self.memory("add", "--input", self.request("move.json"))
        moved = self.base / "搬迁后 新位置"
        shutil.move(str(self.root), moved)
        self.root = moved
        self.assertEqual(self.initialize("status")["workspace_id"], identity)
        context = self.memory("context", "--cwd", moved, "--scene", "general", "--query", "隔离测试", cwd=self.base)
        self.assertEqual(len(context["items"]), 1)
        self.assertEqual(self.memory("show", "mem-template-smoke")["entry"]["id"], "mem-template-smoke")

    def test_init_does_not_follow_linked_memory_directory(self):
        target = self.base / "外部目标"
        shutil.copytree(self.root / "中枢/记忆", target)
        original = self.root / "中枢/记忆"
        shutil.move(str(original), self.base / "原目录保留")
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(original), str(target)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, repr(result.stdout + result.stderr))
        else:
            original.symlink_to(target, target_is_directory=True)
        result = self.initialize("init", code=2)
        self.assertFalse(result["ok"])
        self.assertFalse((target / "配置.json").exists())

    def test_build_verifies_empty_source_and_checksums(self):
        result = self.build()
        archive = Path(result["archive"])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"])
        with zipfile.ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            checksums = json.loads(package.read(FACTORY_MANIFEST))
            self.assertEqual(len(package.namelist()), len(checksums["files"]) + 1)
            for record in checksums["files"]:
                raw = package.read("个人AI协作区/" + record["path"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])
            extraction = self.base / "解压"
            package.extractall(extraction)
        unpacked = extraction / "个人AI协作区"
        self.assertEqual(self.initialize("status", root=unpacked)["status"], "not_initialized")
        self.assertEqual(self.initialize("init", root=unpacked)["memory_mode"], "auto")

    def test_build_rejects_linked_directory(self):
        target = self.base / "外部工具"
        original = self.root / "中枢/工具"
        shutil.move(str(original), target)
        if os.name == "nt":
            linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(original), str(target)], capture_output=True, timeout=10)
            self.assertEqual(linked.returncode, 0, repr(linked.stdout + linked.stderr))
        else:
            original.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.build(code=2)["ok"])
        self.assertFalse((self.base / "发行").exists())

    def test_build_rejects_extra_private_file(self):
        (self.root / "私人资料.txt").write_text("不允许自动纳入", encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])
        self.assertFalse((self.base / "发行").exists())

    def test_build_rejects_initialized_source(self):
        self.initialize("init")
        self.assertFalse(self.build(code=2)["ok"])

    def test_build_rejects_secret_pattern(self):
        path = self.root / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + "ghp_" + "X" * 30, encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])

    def test_build_rejects_external_link(self):
        path = self.root / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n[外部原件](../private.md)\n", encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])

    def make_directory_link(self, link, target):
        if os.name == "nt":
            linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, timeout=10)
            self.assertEqual(linked.returncode, 0, repr(linked.stdout + linked.stderr))
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_init_rejects_linked_workspace_root(self):
        entrance = self.base / "联接入口"
        self.make_directory_link(entrance, self.root)
        self.invoke(entrance / MANAGER, "init", "--json", cwd=self.base, code=2)
        self.assertFalse((self.root / CONFIG).exists())
        self.assertFalse((self.root / STATE).exists())

    def test_build_rejects_linked_source_ancestor(self):
        entrance = self.base / "祖先联接"
        self.make_directory_link(entrance, self.base)
        self.assertFalse(self.build(source=entrance / self.root.name, code=2)["ok"])

    def test_build_rejects_reference_link_escape(self):
        path = self.root / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n[外部][private]\n\n[private]: ../private.md\n", encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])

    def test_build_rejects_local_file_uri(self):
        path = self.root / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n[本地原件](file://server/share/private.md)\n", encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])

    def test_build_rejects_link_to_unarchived_empty_directory(self):
        (self.root / "空目标").mkdir()
        path = self.root / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n[导航](空目标/)\n", encoding="utf-8")
        self.assertFalse(self.build(code=2)["ok"])

    def test_build_payload_is_repeatable(self):
        first = self.build()
        second = self.build()
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertEqual(first["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
