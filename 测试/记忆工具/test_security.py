# -*- coding: utf-8 -*-
"""T18 与入口边界：路径穿越、链接/junction 外逃、错误根、cwd 越界、错误合同。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    FAULT_ENV,
    MEMORY_PY,
    PROJECT_ROOT,
    PYTHON,
    TOOL_DIR,
    IsolatedMemoryTestCase,
    make_body,
    make_metadata,
    parse_json_stdout,
    run_cli,
)


class PathSecurityTests(IsolatedMemoryTestCase):
    def test_t18_root_without_config_is_rejected_and_not_initialised(self) -> None:
        wrong_root = self.base / "错误根"
        wrong_root.mkdir()
        spec = [PYTHON, "-X", "utf8", str(MEMORY_PY), "--root", str(wrong_root), "doctor", "--json"]
        proc = subprocess.run(
            spec, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], 2)
        self.assertIn("配置.json", payload["error"]["message"])
        self.assertFalse((wrong_root / "中枢").exists(), "错误根不得被自动初始化")
        self.assertEqual(list(wrong_root.iterdir()), [])

    def test_invalid_config_schema_version_rejected(self) -> None:
        bad = self.new_root(config={"schema_version": 2})
        proc = run_cli(bad, "doctor", "--json")
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertIn("schema_version", payload["error"]["message"])

    def test_relative_root_and_arbitrary_cwd(self) -> None:
        proc = subprocess.run(
            [
                PYTHON,
                "-X",
                "utf8",
                str(MEMORY_PY),
                "--root",
                self.root.name,
                "validate",
                "--json",
            ],
            cwd=str(self.base),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_t18_id_path_traversal_rejected(self) -> None:
        for bad_id in ("../evil", "mem-../../evil", "mem/evil", "mem-evil.md"):
            data = self.add(
                make_metadata(bad_id, subject_key=f"trav.{abs(hash(bad_id))}"),
                make_body(),
                expect=2,
            )
            self.assertEqual(data["error"]["code"], 2)
        update_proc = run_cli(
            self.root,
            "update",
            "../outside",
            "--input",
            str(self.root / "no-such.json"),
            "--expected-revision",
            "1",
            "--expected-sha256",
            "0" * 64,
            "--json",
        )
        self.assertEqual(update_proc.returncode, 2)
        self.assertFalse((self.base / "evil.md").exists())
        self.assertFalse((self.base / "outside.md").exists())

    def test_t18_scope_target_traversal_rejected(self) -> None:
        cases = ["../outside", "C:/tmp", "/abs", "proj-a/../../x", "//server/share"]
        for index, target in enumerate(cases):
            metadata = make_metadata(
                f"mem-trav-{index}", subject_key=f"trav.scope.{index}", level="project", target=target
            )
            data = self.add(metadata, make_body(), expect=2)
            self.assertEqual(data["error"]["code"], 2)
            self.assertIn("target", data["error"]["message"])
        self.assertEqual(self.entry_files(), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction 专用夹具；非 Windows 平台不调用 cmd/mklink")
    def test_t18_entries_directory_junction_escape_rejected(self) -> None:
        root = self.new_root(name="工作 区-junction")
        entries = root / "中枢" / "记忆" / "条目"
        outside = self.base / "外逃条目目录"
        outside.mkdir()
        os.rmdir(entries)
        try:
            junction_created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(entries), str(outside)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self.skipTest(f"当前环境无法运行 cmd/mklink，junction 外逃拒绝未测: {exc}")
        if junction_created.returncode != 0:
            self.skipTest(
                "当前平台/权限无法创建 junction 夹具；条目目录外逃读/写拒绝未测。"
                f"mklink 输出: {junction_created.stdout} {junction_created.stderr}"
            )
        self.register_junction_cleanup(entries)
        try:
            for args, expect in (
                (("validate", "--json"), 2),
                (("search", "任意", "--cwd", str(root), "--json"), 2),
            ):
                proc = run_cli(root, *args)
                self.assertEqual(proc.returncode, expect, msg=proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertTrue(
                    "越界" in json.dumps(payload, ensure_ascii=False)
                    or "越出" in json.dumps(payload, ensure_ascii=False),
                    msg=json.dumps(payload, ensure_ascii=False),
                )
            request = self.write_json(
                "junction-add.json",
                {
                    "metadata": make_metadata("mem-junction", subject_key="junction.key"),
                    "body": make_body(),
                    "reason": "junction 逃逸测试",
                    "actor": "unittest",
                },
                root=root,
            )
            add_proc = run_cli(root, "add", "--input", str(request), "--json")
            self.assertEqual(add_proc.returncode, 2)
            self.assertEqual(list(outside.iterdir()), [], "不得通过 junction 在外部创建条目")
        finally:
            os.rmdir(entries)

    def test_t18_entry_symlink_escape_rejected(self) -> None:
        root = self.new_root(name="工作 区-symlink")
        outside_file = self.base / "外部条目.md"
        outside_file.write_text("外部内容", encoding="utf-8")
        link = root / "中枢" / "记忆" / "条目" / "mem-symlink.md"
        try:
            os.symlink(outside_file, link)
        except OSError as exc:
            self.skipTest(f"当前环境无法创建文件符号链接（developer mode/权限限制）: {exc}")
        try:
            validate = run_cli(root, "validate", "--json")
            self.assertEqual(validate.returncode, 2, msg=validate.stdout + validate.stderr)
            payload = json.loads(validate.stdout)
            self.assertEqual(payload["counts"]["parse_errors"], 1)
            self.assertIn("越界", json.dumps(payload, ensure_ascii=False))
            show = run_cli(root, "show", "mem-symlink", "--json")
            self.assertEqual(show.returncode, 2)
            self.assertNotIn("外部内容", show.stdout)
        finally:
            link.unlink(missing_ok=True)

    def test_cwd_outside_workspace_rejected(self) -> None:
        self.add(make_metadata("mem-cwd", subject_key="cwd.key"), make_body())
        for cwd in (self.base, self.root / "..", self.root / ".." / ".."):
            proc = run_cli(self.root, "search", "标题", "--cwd", str(cwd), "--json")
            self.assertEqual(proc.returncode, 2, msg=f"cwd={cwd} stdout={proc.stdout}")
            payload = json.loads(proc.stdout)
            self.assertIn("工作区", payload["error"]["message"])

    # ---------------------------------------------------------- 故障注入安全边界
    def test_fault_injection_is_ignored_on_production_root(self) -> None:
        from memlib import faults

        production_root = self.new_root(name="默认根-故障注入测试")
        with mock.patch.dict(os.environ, {FAULT_ENV: "prepared"}), mock.patch(
            "sys.stderr"
        ) as fake_stderr:
            isolated = faults.active_stages(self.root, production_root)
            self.assertEqual(isolated, {"prepared"})
            ignored = faults.active_stages(production_root, production_root)
            self.assertEqual(ignored, set(), "生产根必须忽略故障注入环境变量")
            faults.inject("prepared", production_root, production_root)  # 不应抛错
            faults.inject("derived", production_root, production_root)  # 不应抛错
            self.assertTrue(fake_stderr.write.called or fake_stderr.flush.called)

    # ---------------------------------------------------------- 退出码/输出合同
    def test_cli_help_and_json_error_contract(self) -> None:
        help_proc = run_cli(self.root, "--help")
        self.assertEqual(help_proc.returncode, 0)
        self.assertIn("memory.py", help_proc.stdout)

        no_command = subprocess.run(
            [PYTHON, "-X", "utf8", str(MEMORY_PY), "--root", str(self.root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(no_command.returncode, 2)

        # 解析错误的条目：validate --json 退出 2，stdout 是合法 JSON，结构错误在数据里
        bad_path = self.root / "中枢" / "记忆" / "条目" / "mem-broken.md"
        bad_path.write_text("---\n{not json}\n---\n\n# 坏\n\n## 结论\n\nx\n", encoding="utf-8")
        proc = run_cli(self.root, "validate", "--json")
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data_status"], "invalid")
        self.assertEqual(payload["counts"]["parse_errors"], 1)
        for field in ("data_status", "audit_status", "index_status", "git_status"):
            self.assertIn(field, payload)

        # 请求级错误：错误 JSON 输出到 stdout，诊断在 stderr
        missing = run_cli(self.root, "show", "mem-does-not-exist", "--json")
        self.assertEqual(missing.returncode, 2)
        error_payload = json.loads(missing.stdout)
        self.assertFalse(error_payload["ok"])
        self.assertEqual(error_payload["error"]["code"], 2)
        self.assertIn("错误", missing.stderr)

    def test_json_mode_argparse_error_emits_json_stdout(self) -> None:
        proc = run_cli(self.root, "search", "--json")
        self.assertEqual(proc.returncode, 2)
        payload = parse_json_stdout(proc, context="argparse error with --json")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data_status"], "invalid")
        self.assertEqual(payload["error"]["kind"], "ArgumentError")
        self.assertEqual(payload["error"]["code"], 2)
        self.assertIn("错误", proc.stderr)

    def test_command_runs_from_foreign_cwd_with_absolute_root(self) -> None:
        self.add(make_metadata("mem-cwd2", subject_key="cwd2.key"), make_body())
        proc = subprocess.run(
            [PYTHON, "-X", "utf8", str(MEMORY_PY), "--root", str(self.root), "show", "mem-cwd2", "--json"],
            cwd=str(self.base),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["entry"]["id"], "mem-cwd2")


if __name__ == "__main__":
    unittest.main()
