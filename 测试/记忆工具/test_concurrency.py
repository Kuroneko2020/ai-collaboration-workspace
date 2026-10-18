# -*- coding: utf-8 -*-
"""T04/T05/T06/T07/T08 + 退出码 4：版本/SHA 冲突、真实多进程并发、内核锁释放。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from helpers import (
    FAULT_ENV,
    MEMORY_PY,
    PYTHON,
    TOOL_DIR,
    IsolatedMemoryTestCase,
    make_body,
    make_metadata,
    sha256_file,
    writable_from_metadata,
)

HOLD_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hold_lock.py")


class ConcurrencyTests(IsolatedMemoryTestCase):
    # ---------------------------------------------------------- 辅助
    def _popen(self, *args: str, root=None) -> subprocess.Popen[str]:
        root = root or self.root
        command = [PYTHON, "-X", "utf8", str(MEMORY_PY), "--root", str(root), *args]
        env = os.environ.copy()
        env.pop(FAULT_ENV, None)
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            env=env,
        )

    def _start_lock_holder(self, *, root=None) -> subprocess.Popen[str]:
        root = root or self.root
        lock_path = root / "中枢" / "记忆" / "运行" / "memory.lock"
        command = [PYTHON, "-X", "utf8", HOLD_LOCK, str(TOOL_DIR), str(lock_path), "30"]
        env = os.environ.copy()
        env.pop(FAULT_ENV, None)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            env=env,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if "LOCKED" not in line:
            proc.kill()
            stderr = proc.communicate(timeout=10)[1]
            self.fail(f"持锁子进程未就绪: stdout={line!r} stderr={stderr!r}")
        return proc

    # ---------------------------------------------------------- T04
    def test_t04_stale_revision_update_is_rejected(self) -> None:
        self.add(make_metadata("mem-t04", subject_key="t04.key"), make_body("T04 标题", "原始结论。"))
        original = self.show_entry("mem-t04")
        first = writable_from_metadata(original["metadata"])
        first_target = self.update(
            "mem-t04",
            first,
            make_body("T04 标题", "第一次修正。"),
            expected_revision=original["revision"],
            expected_sha256=original["sha256"],
        )
        self.assertEqual(first_target["result"], "updated")
        after_first = self.show_entry("mem-t04")
        self.assertEqual(after_first["revision"], 2)

        stale = self.update(
            "mem-t04",
            first,
            make_body("T04 标题", "用旧版本的第二次修正。"),
            expected_revision=original["revision"],
            expected_sha256=original["sha256"],
            expect=3,
        )
        self.assertEqual(stale["error"]["code"], 3)
        self.assertIn("revision", stale["error"]["message"])
        current = self.show_entry("mem-t04")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["conclusion"], "第一次修正。")

    # ---------------------------------------------------------- T05
    def test_t05_manual_body_edit_without_revision_bumps_triggers_sha_conflict(self) -> None:
        self.add(make_metadata("mem-t05", subject_key="t05.key"), make_body("T05 标题", "原始结论。"))
        original = self.show_entry("mem-t05")
        entry_file = self.entry_path("mem-t05")
        # 人工编辑正文但没有改 revision
        data = entry_file.read_bytes()
        entry_file.write_bytes(data + "\n人工追加的一行（未改 revision）。\n".encode("utf-8"))
        self.assertNotEqual(sha256_file(entry_file), original["sha256"])

        target = writable_from_metadata(original["metadata"])
        conflict = self.update(
            "mem-t05",
            target,
            make_body("T05 标题", "基于旧 SHA 的更新。"),
            expected_revision=original["revision"],
            expected_sha256=original["sha256"],
            expect=3,
        )
        self.assertEqual(conflict["error"]["code"], 3)
        self.assertIn("sha256", conflict["error"]["message"].lower())
        # 人工修改未被覆盖，revision 未变
        self.assertIn("人工追加的一行", entry_file.read_text(encoding="utf-8"))
        self.assertEqual(self.show_entry("mem-t05")["revision"], 1)

    # ---------------------------------------------------------- T06
    def test_t06_two_processes_add_different_ids_both_kept(self) -> None:
        request_a = self.write_json(
            "add-a.json",
            {
                "metadata": make_metadata("mem-t06-a", subject_key="t06.a"),
                "body": make_body("并发 A", "并发新增 A。"),
                "reason": "T06 并发新增 A",
                "actor": "unittest",
            },
        )
        request_b = self.write_json(
            "add-b.json",
            {
                "metadata": make_metadata("mem-t06-b", subject_key="t06.b"),
                "body": make_body("并发 B", "并发新增 B。"),
                "reason": "T06 并发新增 B",
                "actor": "unittest",
            },
        )
        proc_a = self._popen("add", "--input", str(request_a), "--json")
        proc_b = self._popen("add", "--input", str(request_b), "--json")
        out_a, err_a = proc_a.communicate(timeout=120)
        out_b, err_b = proc_b.communicate(timeout=120)
        self.assertEqual(proc_a.returncode, 0, msg=f"a stdout={out_a} stderr={err_a}")
        self.assertEqual(proc_b.returncode, 0, msg=f"b stdout={out_b} stderr={err_b}")
        self.assertTrue(self.entry_path("mem-t06-a").is_file())
        self.assertTrue(self.entry_path("mem-t06-b").is_file())
        self.assertEqual(len(self.entry_files()), 2)
        self.assertEqual(len(self.credentials()), 2)
        validate = self.cli_json(self.root, "validate", "--json")
        self.assertEqual(validate["counts"]["entries"], 2)
        self.assertEqual(validate["counts"]["invariant_errors"], 0)

    # ---------------------------------------------------------- T07
    def test_t07_two_processes_update_same_old_version_one_wins(self) -> None:
        self.add(make_metadata("mem-t07", subject_key="t07.key"), make_body("T07 标题", "旧结论。"))
        original = self.show_entry("mem-t07")
        target = writable_from_metadata(original["metadata"])
        request_a = self.write_json(
            "update-a.json",
            {
                "metadata": target,
                "body": make_body("T07 标题", "并发更新 A。"),
                "reason": "T07 并发更新 A",
                "actor": "unittest",
            },
        )
        request_b = self.write_json(
            "update-b.json",
            {
                "metadata": target,
                "body": make_body("T07 标题", "并发更新 B。"),
                "reason": "T07 并发更新 B",
                "actor": "unittest",
            },
        )
        base = [
            "update",
            "mem-t07",
            "--expected-revision",
            str(original["revision"]),
            "--expected-sha256",
            original["sha256"],
            "--json",
        ]
        proc_a = self._popen(*base, "--input", str(request_a))
        proc_b = self._popen(*base, "--input", str(request_b))
        out_a, err_a = proc_a.communicate(timeout=120)
        out_b, err_b = proc_b.communicate(timeout=120)
        codes = sorted([proc_a.returncode, proc_b.returncode])
        self.assertEqual(codes, [0, 3], msg=f"a={out_a}{err_a}\nb={out_b}{err_b}")
        winner = "并发更新 A" if proc_a.returncode == 0 else "并发更新 B"
        final = self.show_entry("mem-t07")
        self.assertEqual(final["revision"], 2)
        self.assertEqual(final["conclusion"], winner + "。")
        self.assertEqual(len(self.credentials()), 2)  # add + 唯一成功的 update
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["invariant_errors"], 0)

    # ---------------------------------------------------------- T08
    def test_t08_killed_lock_holder_releases_kernel_lock(self) -> None:
        proc = self._start_lock_holder()
        proc.kill()
        proc.communicate(timeout=20)
        lock_file = self.root / "中枢" / "记忆" / "运行" / "memory.lock"
        self.assertTrue(lock_file.is_file(), "不得删除锁文件")
        start = time.monotonic()
        result = self.cli_json(self.root, "validate", "--json", timeout=60)
        elapsed = time.monotonic() - start
        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 5.0, f"进程被强杀后仍无法及时取锁: {elapsed:.2f}s")
        self.assertTrue(lock_file.is_file())

    # ---------------------------------------------------------- 退出码 4
    def test_lock_timeout_returns_exit_code_4(self) -> None:
        root = self.new_root(config={"lock_timeout_seconds": 0.5})
        proc = self._start_lock_holder(root=root)
        try:
            start = time.monotonic()
            data = self.cli_json(root, "validate", "--json", expect=4, timeout=60)
            elapsed = time.monotonic() - start
            self.assertEqual(data["error"]["code"], 4)
            self.assertIn("锁", data["error"]["message"])
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 10.0)
        finally:
            proc.kill()
            proc.communicate(timeout=20)
        self.cli_json(root, "validate", "--json", timeout=60)


if __name__ == "__main__":
    unittest.main()
