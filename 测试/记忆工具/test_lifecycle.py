# -*- coding: utf-8 -*-
"""T14/T15/T17：生命周期、状态流转、来源约束、合并替代与写事务冲突。"""

from __future__ import annotations

import json
import unittest

from helpers import (
    IsolatedMemoryTestCase,
    make_body,
    make_metadata,
    utc_offset,
    writable_from_metadata,
)


class LifecycleTests(IsolatedMemoryTestCase):
    # ---------------------------------------------------------- candidate
    def test_candidate_gets_default_review_after_and_requires_question(self) -> None:
        self.add(
            make_metadata("mem-cand-1", subject_key="cand.one", status="candidate"),
            make_body("候选标题", "候选结论。", candidate_question="这条偏好是否长期成立？"),
        )
        entry = self.show_entry("mem-cand-1")
        self.assertEqual(entry["status"], "candidate")
        self.assertIsNotNone(entry["review_after"])
        # 复核时间 = 创建时间 + 30 天
        from datetime import datetime, timezone

        def parse(value: str) -> datetime:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        delta = parse(entry["review_after"]) - parse(entry["created_at"])
        self.assertEqual(delta.days, 30)

        bad = self.add(
            make_metadata("mem-cand-2", subject_key="cand.two", status="candidate"),
            make_body("缺待澄清问题", "候选结论。"),
            expect=2,
        )
        self.assertIn("待澄清问题", bad["error"]["message"])
        self.assertFalse(self.entry_path("mem-cand-2").exists())

    def test_update_to_candidate_gets_default_review_after(self) -> None:
        """update 把 active 改为 candidate 时也要按候选政策设置默认复核提示。"""

        self.add(
            make_metadata("mem-to-cand", subject_key="tocand.key"),
            make_body("原当前条目", "原结论。"),
        )
        entry = self.show_entry("mem-to-cand")
        target = writable_from_metadata(entry["metadata"])
        target["status"] = "candidate"
        data = self.update(
            "mem-to-cand",
            target,
            make_body("原当前条目", "原结论。", candidate_question="这条是否应长期保留？"),
        )
        self.assertEqual(data["operations"][0]["status_after"], "candidate")
        updated = self.show_entry("mem-to-cand")
        self.assertIsNotNone(updated["review_after"], "update 路径也必须设置候选默认 review_after")

    def test_candidate_promotion_clears_candidate_review_after(self) -> None:
        self.add(
            make_metadata("mem-promote", subject_key="promote.key", status="candidate"),
            make_body("待转正候选", "候选结论。", candidate_question="是否转正？"),
        )
        entry = self.show_entry("mem-promote")
        self.assertEqual(entry["status"], "candidate")
        auto_review_after = entry["metadata"]["review_after"]
        self.assertIsNotNone(auto_review_after)
        target = writable_from_metadata(entry["metadata"])
        target["status"] = "active"
        data = self.update(
            "mem-promote",
            target,
            entry["body"],
            expected_revision=entry["revision"],
            expected_sha256=entry["sha256"],
        )
        self.assertEqual(data["result"], "updated")
        self.assertIn("review_after", " ".join(data["notes"]))
        after = self.show_entry("mem-promote")
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["revision"], entry["revision"] + 1)
        self.assertIsNone(after["metadata"]["review_after"])
        self.assertTrue(any("候选转正" in note for note in data["notes"]))
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["invariant_errors"], 0)

    # ---------------------------------------------------------- 修正 / 停用 / 否决
    def test_correction_same_id_increments_revision_and_credential(self) -> None:
        self.add(make_metadata("mem-correct", subject_key="correct.key"), make_body("原结论", "旧结论。"))
        entry = self.show_entry("mem-correct")
        target = writable_from_metadata(entry["metadata"])
        data = self.update(
            "mem-correct",
            target,
            make_body("原结论", "用户式纠正后的新结论。"),
            expected_revision=entry["revision"],
            expected_sha256=entry["sha256"],
        )
        self.assertEqual(data["result"], "updated")
        after = self.show_entry("mem-correct")
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["conclusion"], "用户式纠正后的新结论。")
        self.assertEqual(len(self.entry_files()), 1)
        self.assertEqual(len(self.credentials()), 2)
        credential = json.loads((self.root / data["credential"]).read_text(encoding="utf-8"))
        operation = credential["operations"][0]
        self.assertEqual(operation["op"], "update")
        self.assertEqual(operation["revision_before"], 1)
        self.assertEqual(operation["revision_after"], 2)
        credential_text = json.dumps(credential, ensure_ascii=False)
        self.assertNotIn("新结论", credential_text)
        self.assertNotIn("body", credential)
        for operation_record in credential["operations"]:
            self.assertNotIn("body", operation_record)

    def test_retire_excludes_from_current_and_reactivation_needs_user_source(self) -> None:
        self.add(
            make_metadata("mem-retire", subject_key="retire.key", tags=["停用标记"]),
            make_body("停用测试", "停用前结论。"),
        )
        entry = self.show_entry("mem-retire")
        retired_target = writable_from_metadata(entry["metadata"])
        retired_target["status"] = "retired"
        self.update(
            "mem-retire",
            retired_target,
            entry["body"],
            expected_revision=entry["revision"],
            expected_sha256=entry["sha256"],
        )
        self.assertEqual(self.search("停用标记", scenes=[])["total_matched"], 0)
        history = self.search("停用标记", scenes=[], history=True)
        self.assertEqual(history["total_matched"], 1)
        item = history["items"][0]
        self.assertEqual(item["status"], "retired")
        self.assertIn("已停用", item["status_label"])

        retired = self.show_entry("mem-retire")
        bad_target = writable_from_metadata(retired["metadata"])
        bad_target["status"] = "active"
        bad_target["sources"] = [
            {"kind": "agent_observation", "locator": "测试观察", "source_date": None, "observed_at": None}
        ]
        conflict = self.update(
            "mem-retire",
            bad_target,
            retired["body"],
            expected_revision=retired["revision"],
            expected_sha256=retired["sha256"],
            expect=3,
        )
        self.assertEqual(conflict["error"]["code"], 3)
        self.assertEqual(self.show_entry("mem-retire")["status"], "retired")

        good_target = writable_from_metadata(retired["metadata"])
        good_target["status"] = "active"
        good_target["sources"] = [
            {"kind": "user_explicit", "locator": "用户明确重新启用", "source_date": None, "observed_at": None}
        ]
        data = self.update(
            "mem-retire",
            good_target,
            retired["body"],
            expected_revision=retired["revision"],
            expected_sha256=retired["sha256"],
        )
        self.assertEqual(data["result"], "updated")
        self.assertEqual(self.show_entry("mem-retire")["status"], "active")

    def test_reject_candidate_and_history_visibility(self) -> None:
        self.add(
            make_metadata("mem-rejected", subject_key="rejected.key", status="candidate"),
            make_body("被否决候选", "候选结论。", candidate_question="是否成立？"),
        )
        entry = self.show_entry("mem-rejected")
        target = writable_from_metadata(entry["metadata"])
        target["status"] = "rejected"
        data = self.update(
            "mem-rejected",
            target,
            entry["body"],
            expected_revision=entry["revision"],
            expected_sha256=entry["sha256"],
        )
        self.assertEqual(data["result"], "updated")
        self.assertEqual(self.search("被否决候选", scenes=[])["total_matched"], 0)
        history = self.search("被否决候选", scenes=[], history=True)
        self.assertEqual(history["items"][0]["status"], "rejected")
        self.assertIn("已否决", history["items"][0]["status_label"])

    # ---------------------------------------------------------- 合并 / 替代
    def test_merge_and_supersede_in_one_apply_transaction(self) -> None:
        self.add(
            make_metadata("mem-merge-a", subject_key="merge.key", tags=["oldentrytag"]),
            make_body("旧条目", "旧结论内容。"),
        )
        old = self.show_entry("mem-merge-a")
        operations = [
            {
                "op": "add",
                "record": {
                    "metadata": make_metadata(
                        "mem-merge-b", subject_key="merge.key", tags=["newentrytag"]
                    ),
                    "body": make_body("合并后条目", "合并后的新结论。"),
                },
            },
            {
                "op": "update",
                "id": "mem-merge-a",
                "expected_revision": old["revision"],
                "expected_sha256": old["sha256"],
                "record": {
                    "metadata": {
                        **writable_from_metadata(old["metadata"]),
                        "status": "superseded",
                        "superseded_by": "mem-merge-b",
                    },
                    "body": old["body"],
                },
            },
        ]
        data = self.apply(operations)
        self.assertEqual(data["result"], "applied")
        self.assertEqual(len(data["operations"]), 2)
        credential = json.loads(
            (self.root / data["credential"]).read_text(encoding="utf-8")
        )
        self.assertEqual(len(credential["operations"]), 2)
        self.assertEqual(credential["counts"]["added"], 1)
        self.assertEqual(credential["counts"]["updated"], 1)

        old_after = self.show_entry("mem-merge-a")
        new_after = self.show_entry("mem-merge-b")
        self.assertEqual(old_after["status"], "superseded")
        self.assertEqual(old_after["metadata"]["superseded_by"], "mem-merge-b")
        self.assertEqual(new_after["status"], "active")
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["conflict_groups"], 0)
        self.assertEqual(self.search("oldentrytag", scenes=[])["total_matched"], 0)
        self.assertEqual(self.search("newentrytag", scenes=[])["total_matched"], 1)
        history = self.search("oldentrytag", scenes=[], history=True)
        self.assertEqual(history["items"][0]["status"], "superseded")
        self.assertIn("已被替代", history["items"][0]["status_label"])

    def test_delete_requires_explicit_apply_and_blocks_dangling_supersede(self) -> None:
        self.add(make_metadata("mem-del-a", subject_key="del.a"), make_body("待删除条目", "删除测试。"))
        self.add(make_metadata("mem-del-b", subject_key="del.b"), make_body("替代条目", "替代测试。"))
        current = self.show_entry("mem-del-a")
        # 先让 A 指向 B，再尝试删除 B：应因悬空替代指针被拒绝
        self.apply(
            [
                {
                    "op": "update",
                    "id": "mem-del-a",
                    "expected_revision": current["revision"],
                    "expected_sha256": current["sha256"],
                    "record": {
                        "metadata": {
                            **writable_from_metadata(current["metadata"]),
                            "status": "superseded",
                            "superseded_by": "mem-del-b",
                        },
                        "body": current["body"],
                    },
                }
            ]
        )
        b = self.show_entry("mem-del-b")
        dangling = self.apply(
            [
                {
                    "op": "delete",
                    "id": "mem-del-b",
                    "expected_revision": b["revision"],
                    "expected_sha256": b["sha256"],
                }
            ],
            expect=3,
        )
        self.assertEqual(dangling["error"]["code"], 3)
        self.assertTrue(self.entry_path("mem-del-b").is_file())
        self.assertEqual(self.show_entry("mem-del-b")["status"], "active")

        # 同一事务中删除 A 和 B 则合法
        a = self.show_entry("mem-del-a")
        data = self.apply(
            [
                {
                    "op": "delete",
                    "id": "mem-del-b",
                    "expected_revision": b["revision"],
                    "expected_sha256": b["sha256"],
                },
                {
                    "op": "delete",
                    "id": "mem-del-a",
                    "expected_revision": a["revision"],
                    "expected_sha256": a["sha256"],
                },
            ]
        )
        self.assertEqual(data["result"], "applied")
        self.assertFalse(self.entry_path("mem-del-a").exists())
        self.assertFalse(self.entry_path("mem-del-b").exists())
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["entries"], 0)
        self.assertEqual(self.search("删除测试", scenes=[])["total_matched"], 0)

    # ---------------------------------------------------------- T17 写冲突
    def test_t17_write_transaction_rejects_new_conflict(self) -> None:
        self.add(
            make_metadata("mem-clash-a", subject_key="clash.key", tags=["冲突共享词"]),
            make_body("全局条目", "全局结论。"),
        )
        self.add(
            make_metadata("mem-clash-b", subject_key="clash.key", status="candidate"),
            make_body("候选条目", "冲突候选结论。", candidate_question="是否覆盖全局？"),
        )
        candidate = self.show_entry("mem-clash-b")
        target = writable_from_metadata(candidate["metadata"])
        target["status"] = "active"
        data = self.update(
            "mem-clash-b",
            target,
            candidate["body"],
            expected_revision=candidate["revision"],
            expected_sha256=candidate["sha256"],
            expect=3,
        )
        self.assertEqual(data["error"]["code"], 3)
        self.assertIn("新", data["error"]["message"])
        self.assertEqual(self.show_entry("mem-clash-b")["status"], "candidate")
        self.assertEqual(self.show_entry("mem-clash-b")["revision"], candidate["revision"])
        self.assertEqual(len(self.credentials()), 2)
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["conflict_groups"], 0)

        dry = self.update(
            "mem-clash-b",
            target,
            candidate["body"],
            expected_revision=candidate["revision"],
            expected_sha256=candidate["sha256"],
            dry_run=True,
            expect=3,
        )
        self.assertEqual(dry["error"]["code"], 3)

    # ---------------------------------------------------------- 来源 / 时间不变量
    def test_active_preference_requires_user_source(self) -> None:
        agent_only = [
            {"kind": "agent_observation", "locator": "观察记录", "source_date": None, "observed_at": None}
        ]
        conflict = self.add(
            make_metadata("mem-agent-pref", subject_key="agent.pref", sources=agent_only),
            make_body("推断偏好", "推断内容。"),
            expect=3,
        )
        self.assertEqual(conflict["error"]["code"], 3)
        self.assertFalse(self.entry_path("mem-agent-pref").exists())

        # 相同来源作为 candidate 合法
        self.add(
            make_metadata(
                "mem-agent-candidate",
                subject_key="agent.cand",
                status="candidate",
                sources=agent_only,
            ),
            make_body("推断候选", "待确认。", candidate_question="是否属实？"),
        )
        self.assertEqual(self.show_entry("mem-agent-candidate")["status"], "candidate")

    def test_time_and_supersede_field_invariants(self) -> None:
        bad_range = self.add(
            make_metadata(
                "mem-bad-range",
                subject_key="time.range",
                valid_from="2026-02-01T00:00:00Z",
                valid_until="2026-01-01T00:00:00Z",
            ),
            make_body(),
            expect=2,
        )
        self.assertIn("valid_until", bad_range["error"]["message"])

        missing_pointer = self.add(
            make_metadata("mem-bad-superseded", subject_key="time.sup", status="superseded"),
            make_body(),
            expect=2,
        )
        self.assertIn("superseded_by", missing_pointer["error"]["message"])

        active_with_pointer = self.add(
            make_metadata(
                "mem-bad-pointer",
                subject_key="time.ptr",
                superseded_by="mem-somewhere",
            ),
            make_body(),
            expect=2,
        )
        self.assertIn("superseded_by", active_with_pointer["error"]["message"])


if __name__ == "__main__":
    unittest.main()
