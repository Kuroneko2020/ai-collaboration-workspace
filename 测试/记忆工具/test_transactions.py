# -*- coding: utf-8 -*-
"""T09/T10/T19：故障注入、幂等恢复、第三种 SHA、恢复重跑收敛。

故障注入只通过测试专用环境变量 + 隔离 --root 触发；生产根上环境变量会被忽略。
"""

from __future__ import annotations

import json
import unittest

from helpers import (
    FAULT_ENV,
    IsolatedMemoryTestCase,
    make_body,
    make_metadata,
    sha256_file,
)


class FaultInjectionTests(IsolatedMemoryTestCase):
    # ---------------------------------------------------------- 辅助
    def fault_cli(
        self,
        stage: str,
        *args: str,
        expect: int,
        root=None,
    ):
        return self.cli(
            root or self.root,
            *args,
            expect=expect,
            env_extra={FAULT_ENV: stage},
        )

    def recover(self, *, apply: bool, expect: int = 0, root=None) -> dict:
        root = root or self.root
        flag = "--apply" if apply else "--dry-run"
        return self.cli_json(root, "recover", flag, "--json", expect=expect)

    def tx_manifests(self, *, root=None) -> list[dict]:
        manifests: list[dict] = []
        for directory in self.transaction_dirs(root=root):
            path = directory / "manifest.json"
            if path.is_file():
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
        return manifests

    def add_request(self, entry_id: str, *, root=None, subject_key: str = "tx.subject") -> str:
        path = self.write_json(
            f"{entry_id}-request.json",
            {
                "metadata": make_metadata(entry_id, subject_key=subject_key),
                "body": make_body(f"{entry_id} 标题", f"{entry_id} 的结论。"),
                "reason": "T09 事务测试",
                "actor": "unittest",
            },
            root=root,
        )
        return str(path)

    # ---------------------------------------------------------- T09 prepared
    def test_t09_fault_prepared_then_recover_idempotent(self) -> None:
        request = self.add_request("mem-tx-prepared")
        failed = self.fault_cli(
            "prepared", "add", "--input", request, "--json", expect=6
        )
        payload = json.loads(failed.stdout)
        self.assertEqual(payload["error"]["code"], 6)
        self.assertFalse(self.entry_path("mem-tx-prepared").exists())
        self.assertEqual(len(self.transaction_dirs()), 1)
        manifests = self.tx_manifests()
        self.assertEqual(manifests[0]["phase"], "prepared")
        self.assertEqual(self.credentials(), [])

        blocked = self.cli_json(self.root, "search", "mem-tx-prepared", "--cwd", str(self.root), "--json", expect=5)
        self.assertEqual(blocked["data_status"], "needs_recovery")
        self.assertEqual(blocked["error"]["code"], 5)
        context_blocked = self.cli_json(self.root, "context", "--cwd", str(self.root), "--json", expect=5)
        self.assertEqual(context_blocked["error"]["code"], 5)

        dry = self.recover(apply=False)
        self.assertEqual(dry["result"], "recoverable")
        applied = self.recover(apply=True)
        self.assertEqual(applied["result"], "recovered")
        self.assertTrue(self.entry_path("mem-tx-prepared").is_file())
        self.assertEqual(self.show_entry("mem-tx-prepared")["revision"], 1)
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["invariant_errors"], 0)

        again = self.recover(apply=True)
        self.assertEqual(again["result"], "no_work")
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(len(self.entry_files()), 1)

    # ---------------------------------------------------------- T09 首项替换之后
    def test_t09_fault_after_first_replace_apply_two_targets(self) -> None:
        patch = {
            "reason": "T09 两目标事务",
            "actor": "unittest",
            "operations": [
                {
                    "op": "add",
                    "record": {
                        "metadata": make_metadata("mem-tx-multi-a", subject_key="tx.multi.a"),
                        "body": make_body("多目标 A", "A 结论。"),
                    },
                },
                {
                    "op": "add",
                    "record": {
                        "metadata": make_metadata("mem-tx-multi-b", subject_key="tx.multi.b"),
                        "body": make_body("多目标 B", "B 结论。"),
                    },
                },
            ],
        }
        patch_path = self.write_json("apply-multi.json", patch)
        failed = self.fault_cli(
            "after_first_replace", "apply", "--patch-file", str(patch_path), "--json", expect=6
        )
        self.assertIn("after_first_replace", failed.stdout)
        existing = [path for path in self.entry_files()]
        self.assertEqual(len(existing), 1, "第一项替换后应中断，第二项尚未写入")
        self.assertEqual(self.cli_json(self.root, "validate", "--json", expect=5)["error"]["code"], 5)

        dry = self.recover(apply=False)
        self.assertEqual(dry["result"], "recoverable")
        actions = [action for action in dry["transactions"][0]["actions"] if action.get("op") != "credential"]
        self.assertEqual(len(actions), 2)
        applied = self.recover(apply=True)
        self.assertEqual(applied["result"], "recovered")
        self.assertTrue(self.entry_path("mem-tx-multi-a").is_file())
        self.assertTrue(self.entry_path("mem-tx-multi-b").is_file())
        self.assertEqual(len(self.entry_files()), 2)
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["entries"], 2)
        self.assertEqual(self.recover(apply=True)["result"], "no_work")

    # ---------------------------------------------------------- T09 凭据阶段
    def test_t09_fault_before_credential_and_credential_write(self) -> None:
        for stage in ("before_credential", "credential_write"):
            with self.subTest(stage=stage):
                root = self.new_root()
                request = self.add_request("mem-tx-cred", root=root)
                failed = self.fault_cli(stage, "add", "--input", request, "--json", expect=6, root=root)
                self.assertIn(stage, failed.stdout)
                entry_file = self.entry_path("mem-tx-cred", root=root)
                self.assertTrue(entry_file.is_file())
                entry_hash = sha256_file(entry_file)
                self.assertEqual(self.credentials(root=root), [])
                manifests = self.tx_manifests(root=root)
                self.assertEqual(manifests[0]["phase"], "data_committed")

                dry = self.cli_json(root, "recover", "--dry-run", "--json")
                actions = dry["transactions"][0]["actions"]
                self.assertTrue(any(action["action"] == "write_credential" for action in actions))
                applied = self.cli_json(root, "recover", "--apply", "--json")
                self.assertEqual(applied["result"], "recovered")
                self.assertEqual(sha256_file(entry_file), entry_hash, "恢复不得重写已一致的数据")
                self.assertEqual(len(self.credentials(root=root)), 1)
                self.assertEqual(len(self.entry_files(root=root)), 1)
                self.assertEqual(self.show_entry("mem-tx-cred", root=root)["revision"], 1)
                self.assertEqual(self.cli_json(root, "validate", "--json")["counts"]["entries"], 1)
                self.assertEqual(self.cli_json(root, "recover", "--apply", "--json")["result"], "no_work")
                self.assertEqual(len(self.credentials(root=root)), 1)

    # ---------------------------------------------------------- T09 派生失败
    def test_t09_fault_derived_commits_data_then_only_repairs_derived(self) -> None:
        request = self.add_request("mem-tx-derived")
        failed = self.fault_cli("derived", "add", "--input", request, "--json", expect=7)
        payload = json.loads(failed.stdout)
        self.assertEqual(payload["error"]["code"], 7)
        entry_file = self.entry_path("mem-tx-derived")
        self.assertTrue(entry_file.is_file(), "数据已提交，不能因派生失败回滚")
        entry_hash = sha256_file(entry_file)
        self.assertEqual(len(self.credentials()), 1, "凭据已提交")
        manifests = self.tx_manifests()
        self.assertEqual(manifests[0]["phase"], "committed")
        self.assertFalse(manifests[0]["derived_complete"])
        # committed 后数据一致，查询可用（只是派生视图陈旧）
        search = self.search("mem-tx-derived", scenes=[])
        self.assertEqual(search["total_matched"], 1)
        self.assertIn(search["index_status"], ("missing", "stale", "corrupt"))

        applied = self.recover(apply=True)
        self.assertEqual(applied["result"], "recovered")
        self.assertEqual(sha256_file(entry_file), entry_hash, "只补派生，不重做数据")
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(len(self.entry_files()), 1)
        index_file = self.root / "中枢" / "记忆" / "运行" / "index.json"
        self.assertTrue(index_file.is_file())
        check = self.cli_json(self.root, "reindex", "--check", "--json")
        self.assertTrue(check["fresh"])
        self.assertEqual(self.recover(apply=True)["result"], "no_work")

    # ---------------------------------------------------------- T10 第三种 SHA
    def test_t10_third_sha_blocks_recovery_and_preserves_both_sides(self) -> None:
        request = self.add_request("mem-tx-third")
        failed = self.fault_cli("before_credential", "add", "--input", request, "--json", expect=6)
        self.assertEqual(json.loads(failed.stdout)["error"]["code"], 6)
        entry_file = self.entry_path("mem-tx-third")
        self.assertTrue(entry_file.is_file())
        after_bytes = entry_file.read_bytes()
        self.assertEqual(self.credentials(), [])

        # 第三方在恢复前把目标改成第三种内容
        third_bytes = after_bytes + "\n第三方编辑器追加的第三种内容。\n".encode("utf-8")
        entry_file.write_bytes(third_bytes)

        dry = self.recover(apply=False, expect=3)
        self.assertEqual(dry["result"], "blocked")
        self.assertTrue(dry["blocked"])
        self.assertIn("第三种 SHA", json.dumps(dry, ensure_ascii=False))

        applied = self.recover(apply=True, expect=3)
        self.assertEqual(applied["result"], "blocked")
        self.assertEqual(entry_file.read_bytes(), third_bytes, "冲突时必须保留双方材料，不覆盖第三方修改")
        self.assertEqual(self.credentials(), [])
        self.assertTrue(self.transaction_dirs(), "事务材料必须保留以供排查")
        self.assertEqual(self.tx_manifests()[0]["phase"], "data_committed")

        # 恢复为清单记录的新值后，recover 才能幂等补齐凭据
        entry_file.write_bytes(after_bytes)
        recovered = self.recover(apply=True)
        self.assertEqual(recovered["result"], "recovered")
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["entries"], 1)
        self.assertEqual(self.recover(apply=True)["result"], "no_work")

    # ---------------------------------------------------------- 审查修复回归
    def test_b1_manifest_id_path_traversal_is_blocked(self) -> None:
        """恢复不得信任清单里的 id；id/path 越界时停止覆盖并保留材料。"""

        request = self.add_request("mem-b1")
        self.fault_cli("prepared", "add", "--input", request, "--json", expect=6)
        tx_dirs = self.transaction_dirs()
        self.assertEqual(len(tx_dirs), 1)
        manifest_path = tx_dirs[0] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = manifest["targets"][0]
        target["id"] = "../escape"
        target["path"] = "中枢/记忆/条目/../escape.md"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        for dry in (True, False):
            report = self.recover(apply=not dry, expect=3)
            self.assertEqual(report["result"], "blocked")
            self.assertTrue(report["blocked"])
        escape = self.root / "中枢" / "记忆" / "escape.md"
        self.assertFalse(escape.exists(), "恢复绝不能在条目目录之外写文件")
        self.assertFalse(self.entry_path("mem-b1").exists())
        self.assertEqual(self.credentials(), [])
        self.assertTrue(tx_dirs[0].is_dir(), "冲突时保留事务材料供排查")

    def test_recover_rejects_empty_or_nonobject_targets(self) -> None:
        """清单 targets 为空或不是对象数组时必须 blocked，不能把半截事务标成 committed。"""

        request = self.add_request("mem-b1b")
        self.fault_cli("prepared", "add", "--input", request, "--json", expect=6)
        tx_dir = self.transaction_dirs()[0]
        manifest_path = tx_dir / "manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        for bad_targets in ([], {"id": "not-a-list"}, [{"path": "中枢/记忆/条目/x.md"}]):
            manifest = dict(original)
            manifest["targets"] = bad_targets
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            for apply in (False, True):
                report = self.recover(apply=apply, expect=3)
                self.assertEqual(report["result"], "blocked")
            self.assertFalse(self.entry_path("mem-b1b").exists())
            self.assertEqual(self.credentials(), [], "被拒绝的恢复不得写凭据")

    def test_m1_orphan_transaction_without_manifest_can_be_quarantined(self) -> None:
        """清单落盘前中断留下的无清单目录不应永久阻断全部命令。"""

        transactions = self.root / "中枢" / "记忆" / "运行" / "transactions"
        orphan = transactions / "tx-orphan-no-manifest"
        (orphan / "staged").mkdir(parents=True)
        (orphan / "staged" / "000.bin").write_bytes(b"partial staged bytes")

        blocked = self.cli_json(self.root, "validate", "--json", expect=5)
        self.assertEqual(blocked["data_status"], "needs_recovery")
        doctor = self.cli_json(self.root, "doctor", "--json", expect=5)
        codes = {finding["code"] for finding in doctor["findings"]}
        self.assertIn("pending_transaction", codes, "doctor 必须仍返回 findings，而不是只抛错")

        dry = self.cli_json(self.root, "recover", "--dry-run", "--json")
        self.assertEqual(dry["result"], "recoverable")
        self.assertEqual(dry["transactions"][0]["actions"][0]["action"], "quarantine_orphan")

        applied = self.cli_json(self.root, "recover", "--apply", "--json")
        self.assertEqual(applied["result"], "recovered")
        self.assertTrue(applied.get("quarantined"))
        self.assertFalse(orphan.exists(), "无清单目录应被移入 orphaned，而不是永久阻断")
        orphaned = self.root / "中枢" / "记忆" / "运行" / "orphaned"
        self.assertTrue(any(orphaned.iterdir()))
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["entries"], 0)
        self.assertEqual(self.recover(apply=True)["result"], "no_work")


if __name__ == "__main__":
    unittest.main()
