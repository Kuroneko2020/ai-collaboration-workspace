# -*- coding: utf-8 -*-
"""T01/T02/T03/T19：编码格式、严格拒绝、只读不变、新增收敛。"""

from __future__ import annotations

import json
import unittest

from helpers import (
    IsolatedMemoryTestCase,
    make_body,
    make_metadata,
    sha256_file,
    utc_offset,
    writable_from_metadata,
)


class FormatAndStrictnessTests(IsolatedMemoryTestCase):
    # ---------------------------------------------------------- T01
    def test_t01_chinese_space_root_and_bom_crlf_request(self) -> None:
        metadata = make_metadata("mem-t01-main", subject_key="t01.subject", tags=["中文", "空格"])
        request = {
            "metadata": metadata,
            "body": make_body("中文标题 与 空格", "这是中文编码与换行测试数据。"),
            "reason": "T01 BOM/CRLF 请求",
            "actor": "unittest",
        }
        # 请求文件：UTF-8 BOM + CRLF
        text = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        path = self.root / "add-bom-crlf.json"
        path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))

        data = self.cli_json(self.root, "add", "--input", str(path), "--json")
        self.assertEqual(data["result"], "created")
        entry_file = self.entry_path("mem-t01-main")
        self.assertTrue(entry_file.is_file())
        raw = entry_file.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "新写条目不得带 BOM")
        self.assertNotIn(b"\r\n", raw, "新写条目必须 LF")
        self.assertTrue(raw.startswith(b"---\n"), "front matter 必须以独立 --- 行开始")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn("中文标题 与 空格".encode("utf-8"), raw)

        # 读取兼容 BOM + CRLF 的人工/导入文件
        text2 = entry_file.read_text(encoding="utf-8")
        entry_file.write_bytes(b"\xef\xbb\xbf" + text2.replace("\n", "\r\n").encode("utf-8"))
        shown = self.show_entry("mem-t01-main")
        self.assertEqual(shown["title"], "中文标题 与 空格")
        self.assertEqual(shown["conclusion"], "这是中文编码与换行测试数据。")
        self.assertFalse(entry_file.read_bytes().startswith(b"---"))

        # 无实质变化的 update：no_change，且不改写带 BOM/CRLF 的原文件
        before_hash = sha256_file(entry_file)
        current = self.show_entry("mem-t01-main")
        no_change = self.update(
            "mem-t01-main",
            writable_from_metadata(current["metadata"]),
            current["body"],
            expected_revision=current["revision"],
            expected_sha256=current["sha256"],
        )
        self.assertEqual(no_change["result"], "no_change")
        self.assertTrue(no_change["no_change"])
        self.assertIsNone(no_change["transaction_id"])
        self.assertEqual(sha256_file(entry_file), before_hash)

    def test_t01_body_with_separator_lines_does_not_confuse_parser(self) -> None:
        body = (
            "# 含分隔线的正文\n\n## 结论\n\n结论第一段。\n\n---\n\n## 使用边界\n\n"
            "分隔线 --- 出现在正文中，不影响 front matter 解析。\n"
        )
        self.add(make_metadata("mem-t01-sep", subject_key="t01.sep"), body)
        shown = self.show_entry("mem-t01-sep")
        self.assertIn("---", shown["body"])
        self.assertIn("分隔线", shown["body"])
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["parse_errors"], 0)

    def test_t01_nullable_time_fields_require_timezone(self) -> None:
        metadata = make_metadata("mem-t01-tz", subject_key="t01.tz", valid_from="2026-01-01")
        data = self.add(metadata, make_body(), expect=2)
        self.assertEqual(data["error"]["code"], 2)
        self.assertIn("时区", data["error"]["message"])
        self.assertEqual(self.entry_files(), [])

    # ---------------------------------------------------------- T02
    def test_t02_duplicate_json_key_rejected(self) -> None:
        metadata = make_metadata("mem-t02-dup", subject_key="t02.dup")
        request = {
            "metadata": metadata,
            "body": make_body(),
            "reason": "T02 重复键",
            "actor": "unittest",
        }
        text = json.dumps(request, ensure_ascii=False, indent=2)
        self.assertIn('"kind": "preference"', text)
        text = text.replace('"kind": "preference"', '"kind": "preference",\n        "kind": "fact"', 1)
        path = self.root / "dup-key.json"
        path.write_text(text, encoding="utf-8")
        data = self.cli_json(self.root, "add", "--input", str(path), "--json", expect=2)
        self.assertEqual(data["error"]["code"], 2)
        self.assertIn("重复键", data["error"]["message"])
        self.assertEqual(self.entry_files(), [])
        self.assertEqual(self.credentials(), [])

    def test_t02_missing_sources_unknown_fields_and_bad_types_rejected(self) -> None:
        # 缺来源
        metadata = make_metadata("mem-t02-nosrc", subject_key="t02.nosrc")
        metadata.pop("sources")
        data = self.add(metadata, make_body(), expect=2)
        self.assertIn("sources", data["error"]["message"])

        # 未知字段
        metadata = make_metadata("mem-t02-unknown", subject_key="t02.unknown")
        metadata["typo_field"] = 1
        data = self.add(metadata, make_body(), expect=2)
        self.assertIn("typo_field", data["error"]["message"])

        # 工具托管字段不得由请求设置
        metadata = make_metadata("mem-t02-tool", subject_key="t02.tool")
        metadata["revision"] = 7
        data = self.add(metadata, make_body(), expect=2)
        self.assertIn("工具托管", data["error"]["message"])

        # 错误类型
        metadata = make_metadata("mem-t02-type", subject_key="t02.type")
        metadata["pinned"] = "yes"
        data = self.add(metadata, make_body(), expect=2)
        self.assertIn("pinned", data["error"]["message"])

        self.assertEqual(self.entry_files(), [])
        self.assertEqual(self.credentials(), [])

    def test_t02_invalid_id_status_and_wrong_sha_argument(self) -> None:
        bad_id = self.add(make_metadata("mem-Bad-ID", subject_key="t02.bad"), make_body(), expect=2)
        self.assertIn("id", bad_id["error"]["message"].lower())

        bad_status = self.add(
            make_metadata("mem-t02-status", subject_key="t02.status", status="archived"),
            make_body(),
            expect=2,
        )
        self.assertIn("status", bad_status["error"]["message"])

        self.add(make_metadata("mem-t02-arg", subject_key="t02.arg"), make_body())
        path = self.write_json(
            "bad-update.json",
            {
                "metadata": make_metadata("mem-t02-arg", subject_key="t02.arg"),
                "body": make_body(),
                "reason": "坏参数",
                "actor": "unittest",
            },
        )
        bad_sha = self.cli_json(
            self.root,
            "update",
            "mem-t02-arg",
            "--input",
            str(path),
            "--expected-revision",
            "1",
            "--expected-sha256",
            "not-a-sha",
            "--json",
            expect=2,
        )
        self.assertIn("SHA-256", bad_sha["error"]["message"])

    def test_t02_handwritten_entry_unknown_schema_version_is_reported(self) -> None:
        metadata = make_metadata("mem-bad-schema", subject_key="t02.schema")
        full = {
            "schema_version": 2,
            "id": metadata.get("id"),
            "revision": 1,
            "kind": metadata["kind"],
            "subject_key": metadata["subject_key"],
            "status": metadata["status"],
            "scope": metadata["scope"],
            "pinned": metadata["pinned"],
            "tags": metadata["tags"],
            "sources": metadata["sources"],
            "created_at": utc_offset(-1),
            "updated_at": utc_offset(-1),
            "last_verified_at": None,
            "valid_from": None,
            "valid_until": None,
            "review_after": None,
            "end_condition": None,
            "superseded_by": None,
        }
        text = (
            "---\n"
            + json.dumps(full, ensure_ascii=False, indent=2)
            + "\n---\n\n# 非法版本\n\n## 结论\n\n测试。\n"
        )
        self.write_raw("mem-bad-schema.md", text, root=self.root / "中枢" / "记忆" / "条目")
        validate = self.cli_json(self.root, "validate", "--json", expect=2)
        self.assertEqual(validate["counts"]["parse_errors"], 1)
        self.assertIn("schema_version", json.dumps(validate["errors"], ensure_ascii=False))
        search = self.search("测试", scenes=[])
        self.assertEqual(search["data_status"], "degraded")
        self.assertEqual(search["total_matched"], 0)

    # ---------------------------------------------------------- T03
    def test_t03_readonly_commands_do_not_change_facts_or_credentials(self) -> None:
        self.add(
            make_metadata("mem-t03-readonly", subject_key="t03.subject", pinned=True),
            make_body("只读测试标题", "只读命令不应改变事实。"),
        )
        self.cli(self.root, "reindex")
        before = self.write_state_snapshot()
        revision_before = self.show_entry("mem-t03-readonly")["revision"]

        self.cli_json(self.root, "show", "mem-t03-readonly", "--json")
        self.search("只读", scenes=[])
        self.context()
        self.cli_json(self.root, "validate", "--json")
        self.cli_json(self.root, "review", "--json")
        self.cli_json(self.root, "doctor", "--json")
        self.cli_json(self.root, "reindex", "--check", "--json")

        self.assertEqual(self.write_state_snapshot(), before)
        self.assertEqual(self.show_entry("mem-t03-readonly")["revision"], revision_before)
        self.assertEqual(len(self.credentials()), 1)  # 只有最初 add 的凭据

    def test_t03_no_change_update_adds_no_revision_or_credential(self) -> None:
        self.add(make_metadata("mem-t03-noop", subject_key="t03.noop"), make_body())
        before_state = self.write_state_snapshot()
        before_entry = self.show_entry("mem-t03-noop")
        data = self.update(
            "mem-t03-noop",
            writable_from_metadata(before_entry["metadata"]),
            before_entry["body"],
            expected_revision=before_entry["revision"],
            expected_sha256=before_entry["sha256"],
        )
        self.assertEqual(data["result"], "no_change")
        self.assertTrue(data["no_change"])
        self.assertIsNone(data["transaction_id"])
        self.assertIsNone(data["credential"])
        self.assertEqual(self.show_entry("mem-t03-noop")["revision"], 1)
        self.assertEqual(self.write_state_snapshot(), before_state)
        self.assertEqual(len(self.credentials()), 1)

    # ---------------------------------------------------------- 其他格式合同
    def test_add_dry_run_writes_nothing(self) -> None:
        data = self.add(
            make_metadata("mem-dry", subject_key="dry.subject"),
            make_body(),
            dry_run=True,
        )
        self.assertEqual(data["result"], "dry_run")
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["operations"][0]["op"], "add")
        self.assertEqual(self.entry_files(), [])
        self.assertEqual(self.credentials(), [])

    def test_reindex_missing_markers_reports_without_touching_memory_md(self) -> None:
        root = self.new_root(memory_md="# 记忆\n\n人工区，没有生成标记。\n")
        self.add(make_metadata("mem-nomarker", subject_key="nomarker.key"), make_body(), root=root)
        memory_md = root / "中枢" / "记忆" / "记忆.md"
        before = sha256_file(memory_md)
        data = self.cli_json(root, "reindex", "--json", expect=7)
        self.assertFalse(data["fresh"], "链接区未刷新时不得报告 fresh")
        self.assertEqual(data["derived"]["links_status"], "markers_missing")
        self.assertFalse(data["derived"]["links_written"])
        self.assertEqual(data["derived"]["index_status"], "fresh")
        self.assertTrue((root / "中枢" / "记忆" / "运行" / "index.json").is_file())
        self.assertEqual(sha256_file(memory_md), before, "缺少标记时不得修改记忆.md")
        doctor = self.cli_json(root, "doctor", "--json")
        codes = {finding["code"] for finding in doctor["findings"]}
        self.assertIn("generated_links_markers_missing", codes)
        check = self.cli_json(root, "reindex", "--check", "--json")
        self.assertFalse(check["fresh"])
        self.assertEqual(check["derived"]["links"]["status"], "markers_missing")

        # 放置成对标记后，reindex 只刷新标记区
        text = memory_md.read_text(encoding="utf-8")
        memory_md.write_text(
            text + "\n<!-- BEGIN GENERATED MEMORY LINKS -->\n<!-- END GENERATED MEMORY LINKS -->\n",
            encoding="utf-8",
            newline="\n",
        )
        updated = self.cli_json(root, "reindex", "--json")
        self.assertEqual(updated["derived"]["links_status"], "updated")
        content = memory_md.read_text(encoding="utf-8")
        self.assertIn("mem-nomarker", content)
        self.assertIn("人工区，没有生成标记。", content)

    def test_reindex_duplicate_markers_not_touched(self) -> None:
        repeated = (
            "# 记忆\n\n<!-- BEGIN GENERATED MEMORY LINKS -->\nA\n<!-- END GENERATED MEMORY LINKS -->\n"
            "\n<!-- BEGIN GENERATED MEMORY LINKS -->\nB\n<!-- END GENERATED MEMORY LINKS -->\n"
        )
        root = self.new_root(memory_md=repeated)
        memory_md = root / "中枢" / "记忆" / "记忆.md"
        before = sha256_file(memory_md)
        data = self.cli_json(root, "reindex", "--json", expect=7)
        self.assertEqual(data["derived"]["links_status"], "markers_missing")
        self.assertEqual(sha256_file(memory_md), before, "重复标记时不得猜测或改写")

    def test_update_and_apply_dry_run_write_nothing(self) -> None:
        self.add(make_metadata("mem-dry2", subject_key="dry.two"), make_body("dry 标题", "dry 结论。"))
        current = self.show_entry("mem-dry2")
        entry_file = self.entry_path("mem-dry2")
        before_hash = sha256_file(entry_file)
        data = self.update(
            "mem-dry2",
            writable_from_metadata(current["metadata"]),
            make_body("dry 标题", "dry 新结论。"),
            expected_revision=current["revision"],
            expected_sha256=current["sha256"],
            dry_run=True,
        )
        self.assertEqual(data["result"], "dry_run")
        self.assertTrue(data["would_change"])
        self.assertEqual(sha256_file(entry_file), before_hash)
        self.assertEqual(self.show_entry("mem-dry2")["revision"], 1)
        self.assertEqual(len(self.credentials()), 1)

        applied = self.apply(
            [
                {
                    "op": "add",
                    "record": {
                        "metadata": make_metadata("mem-dry3", subject_key="dry.three"),
                        "body": make_body("dry3 标题", "dry3 结论。"),
                    },
                }
            ],
            dry_run=True,
        )
        self.assertEqual(applied["result"], "dry_run")
        self.assertFalse(self.entry_path("mem-dry3").exists())
        self.assertEqual(len(self.credentials()), 1)
        self.assertEqual(len(self.entry_files()), 1)

    def test_verification_is_only_entry_for_last_verified_at(self) -> None:
        verification = {
            "verified_at": "2026-10-18T02:00:00+08:00",
            "source_locator": "隔离测试#来源核实",
            "reason": "初次来源核实",
        }
        self.add(
            make_metadata("mem-verify", subject_key="verify.key"),
            make_body("核实条目", "核实测试。"),
            verification=verification,
        )
        entry = self.show_entry("mem-verify")
        self.assertEqual(entry["metadata"]["last_verified_at"], "2026-10-17T18:00:00Z")

        verification_only = self.update(
            "mem-verify",
            writable_from_metadata(entry["metadata"]),
            entry["body"],
            expected_revision=entry["revision"],
            expected_sha256=entry["sha256"],
            verification={
                "verified_at": "2026-10-18T03:00:00+08:00",
                "source_locator": "隔离测试#再次核实",
                "reason": "再次核实来源",
            },
        )
        self.assertEqual(verification_only["result"], "verified")
        after = self.show_entry("mem-verify")
        self.assertEqual(after["revision"], entry["revision"], "仅重新核实不制造新 revision")
        self.assertEqual(after["metadata"]["last_verified_at"], "2026-10-17T19:00:00Z")
        self.assertEqual(len(self.credentials()), 2)

        # 裸 datetime 的 verification 被拒绝
        bad = self.update(
            "mem-verify",
            writable_from_metadata(after["metadata"]),
            after["body"],
            expected_revision=after["revision"],
            expected_sha256=after["sha256"],
            verification={
                "verified_at": "2026-10-18T03:00:00",
                "source_locator": "x",
                "reason": "y",
            },
            expect=2,
        )
        self.assertIn("时区", bad["error"]["message"])
        self.assertEqual(self.show_entry("mem-verify")["metadata"]["last_verified_at"], "2026-10-17T19:00:00Z")

    def test_add_generates_stable_id_and_canonical_bytes(self) -> None:
        metadata = make_metadata(subject_key="gen.subject")
        data = self.add(metadata, make_body("自动 ID 条目", "自动生成的 ID 应稳定可读。"))
        entry_id = data["operations"][0]["id"]
        self.assertRegex(entry_id, r"^mem-[0-9a-f]{32}$")
        path = self.entry_path(entry_id)
        self.assertTrue(path.is_file())
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"---\n"))
        self.assertNotIn(b"\r\n", raw)
        self.assertNotIn(b"\xef\xbb\xbf", raw)
        shown = self.show_entry(entry_id)
        self.assertEqual(path.name, f"{shown['id']}.md")

    def test_t19_repeat_add_same_id_conflict_creates_no_duplicate(self) -> None:
        metadata = make_metadata("mem-t19-seed", subject_key="t19.seed")
        self.add(metadata, make_body("种子标题", "种子结论。"))
        again = self.add(metadata, make_body("种子标题", "种子结论。"), expect=3)
        self.assertEqual(again["error"]["code"], 3)
        self.assertEqual(len(self.entry_files()), 1)
        self.assertEqual(len(self.credentials()), 1)
        shown = self.show_entry("mem-t19-seed")
        self.assertEqual(shown["revision"], 1)

    def test_t19_reindex_rerun_is_noop(self) -> None:
        self.add(make_metadata("mem-t19-index", subject_key="t19.index"), make_body())
        self.cli(self.root, "reindex")
        index_file = self.root / "中枢" / "记忆" / "运行" / "index.json"
        first_hash = sha256_file(index_file)
        second = self.cli_json(self.root, "reindex", "--json")
        self.assertFalse(second["derived"]["index_written"])
        self.assertFalse(second["derived"]["links_written"])
        self.assertEqual(sha256_file(index_file), first_hash)
        third = self.cli_json(self.root, "reindex", "--json")
        self.assertFalse(third["derived"]["index_written"])
        self.assertEqual(sha256_file(index_file), first_hash)
        self.assertEqual(len(self.credentials()), 1)


if __name__ == "__main__":
    unittest.main()
