# -*- coding: utf-8 -*-
"""T11/T12/T13/T14/T16/T17：索引降级、范围隔离、时间边界、状态过滤、检索与冲突。"""

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


class QueryTests(IsolatedMemoryTestCase):
    # ---------------------------------------------------------- T12 范围隔离
    def test_t12_project_scene_task_scope_isolation(self) -> None:
        for folder in ("proj-a", "proj-b", "proj-a-old", "proj-a/sub"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        common = "范围测试关键字"
        self.add(
            make_metadata(
                "mem-scope-ws", subject_key="scope.ws", scenes=[], tags=["范围测试"], pinned=True
            ),
            make_body("全局范围", common + "：workspace 条目。"),
        )
        self.add(
            make_metadata(
                "mem-scope-pa",
                subject_key="scope.pa",
                level="project",
                target="proj-a",
                scenes=["engineering"],
                tags=["范围测试"],
            ),
            make_body("项目 A 范围", common + "：project-a 条目。"),
        )
        self.add(
            make_metadata(
                "mem-scope-pb",
                subject_key="scope.pb",
                level="project",
                target="proj-b",
                scenes=[],
                tags=["范围测试"],
            ),
            make_body("项目 B 范围", common + "：project-b 条目。"),
        )
        self.add(
            make_metadata(
                "mem-scope-task",
                subject_key="scope.task",
                level="task",
                target="proj-a",
                task_id="task-1",
                scenes=[],
                tags=["范围测试"],
            ),
            make_body("任务范围", common + "：task-1 条目。"),
        )

        def ids(result: dict) -> set[str]:
            return {item["id"] for item in result["items"]}

        pa = self.search(common, cwd=self.root / "proj-a", scenes=["engineering"])
        self.assertIn("mem-scope-ws", ids(pa))
        self.assertIn("mem-scope-pa", ids(pa))
        self.assertNotIn("mem-scope-pb", ids(pa))
        self.assertNotIn("mem-scope-task", ids(pa))

        no_scene = self.search(common, cwd=self.root / "proj-a", scenes=[])
        self.assertIn("mem-scope-ws", ids(no_scene))
        self.assertNotIn("mem-scope-pa", ids(no_scene), "条件性条目不能在无 scene 时默认进入")

        wrong_scene = self.search(common, cwd=self.root / "proj-a", scenes=["writing"])
        self.assertNotIn("mem-scope-pa", ids(wrong_scene))

        with_task = self.search(
            common, cwd=self.root / "proj-a/sub", scenes=["engineering"], task_id="task-1"
        )
        self.assertIn("mem-scope-pa", ids(with_task))
        self.assertIn("mem-scope-task", ids(with_task))
        wrong_task = self.search(
            common, cwd=self.root / "proj-a", scenes=["engineering"], task_id="task-2"
        )
        self.assertNotIn("mem-scope-task", ids(wrong_task))

        root_view = self.search(common, cwd=self.root, scenes=["engineering"])
        self.assertEqual(ids(root_view), {"mem-scope-ws"})
        self.assertNotIn("mem-scope-pa", ids(root_view))

        # proj-a-old 不能按字符串前缀被当成 proj-a 的子目录
        sibling = self.search(common, cwd=self.root / "proj-a-old", scenes=["engineering"])
        self.assertEqual(ids(sibling), {"mem-scope-ws"})
        old_root = self.new_root(name="工作 区-old")
        outside = self.search(common, cwd=old_root, scenes=[], expect=2)
        self.assertIn("工作区", outside["error"]["message"])

    # ---------------------------------------------------------- T13 时间边界
    def test_t13_review_after_and_valid_until_boundaries(self) -> None:
        self.add(
            make_metadata(
                "mem-time-boundary",
                subject_key="time.boundary",
                tags=["边界标记"],
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-02-01T00:00:00Z",
                review_after="2026-01-15T00:00:00Z",
            ),
            make_body("时间边界条目", "半开区间边界测试。"),
        )

        def found(at: str) -> dict:
            return self.search("边界标记", scenes=[], at=at)

        self.assertEqual(found("2025-12-31T23:59:59Z")["total_matched"], 0, "valid_from 之前应排除")
        start = found("2026-01-01T00:00:00Z")
        self.assertEqual(start["total_matched"], 1, "valid_from 边界应包含（闭端）")
        self.assertFalse(start["items"][0]["needs_review"])
        before_review = found("2026-01-14T23:59:59Z")
        self.assertFalse(before_review["items"][0]["needs_review"])
        at_review = found("2026-01-15T00:00:00Z")
        self.assertEqual(at_review["total_matched"], 1, "review_after 到期不导致失效，只提示")
        self.assertTrue(at_review["items"][0]["needs_review"])
        self.assertEqual(found("2026-01-31T23:59:59Z")["total_matched"], 1)
        self.assertEqual(found("2026-02-01T00:00:00Z")["total_matched"], 0, "valid_until 边界应排除（开端）")
        self.assertEqual(self.search("边界标记", scenes=[])["total_matched"], 0, "当前时刻已过期")

        review = self.cli_json(self.root, "review", "--json")
        self.assertIn("mem-time-boundary", {item["id"] for item in review["review_due"]})
        self.assertIn("mem-time-boundary", {item["id"] for item in review["validity_expired"]})
        expired_finding = [
            finding for finding in review["findings"] if finding["code"] == "validity_expired"
        ]
        self.assertTrue(expired_finding)
        validate = self.cli_json(self.root, "validate", "--json")
        self.assertEqual(validate["counts"]["invariant_errors"], 0)

    # ---------------------------------------------------------- T14 状态过滤
    def test_t14_non_active_statuses_excluded_then_history_and_show(self) -> None:
        marker = "状态可见标记"
        self.add(
            make_metadata("mem-status-cand", subject_key="status.cand", status="candidate"),
            make_body("候选状态条目", marker + " 候选。", candidate_question="是否成立？"),
        )
        self.add(
            make_metadata("mem-status-retired", subject_key="status.retired"),
            make_body("停用状态条目", marker + " 停用。"),
        )
        retired = self.show_entry("mem-status-retired")
        retired_target = writable_from_metadata(retired["metadata"])
        retired_target["status"] = "retired"
        self.update(
            "mem-status-retired",
            retired_target,
            retired["body"],
            expected_revision=retired["revision"],
            expected_sha256=retired["sha256"],
        )
        self.add(
            make_metadata("mem-status-rejected", subject_key="status.rejected", status="candidate"),
            make_body("否决状态条目", marker + " 否决。", candidate_question="是否成立？"),
        )
        rejected = self.show_entry("mem-status-rejected")
        rejected_target = writable_from_metadata(rejected["metadata"])
        rejected_target["status"] = "rejected"
        self.update(
            "mem-status-rejected",
            rejected_target,
            rejected["body"],
            expected_revision=rejected["revision"],
            expected_sha256=rejected["sha256"],
        )
        self.add(make_metadata("mem-status-old", subject_key="status.old"), make_body("旧状态条目", marker + " 旧。"))
        self.add(make_metadata("mem-status-new", subject_key="status.new"), make_body("替代条目", "替代目标。"))
        old = self.show_entry("mem-status-old")
        self.apply(
            [
                {
                    "op": "update",
                    "id": "mem-status-old",
                    "expected_revision": old["revision"],
                    "expected_sha256": old["sha256"],
                    "record": {
                        "metadata": {
                            **writable_from_metadata(old["metadata"]),
                            "status": "superseded",
                            "superseded_by": "mem-status-new",
                        },
                        "body": old["body"],
                    },
                }
            ]
        )

        normal = self.search(marker, scenes=[])
        self.assertEqual(normal["total_matched"], 0, "candidate/retired/rejected/superseded 不得进入正常检索")
        history = self.search(marker, scenes=[], history=True)
        by_id = {item["id"]: item for item in history["items"]}
        self.assertEqual(
            set(by_id),
            {"mem-status-cand", "mem-status-retired", "mem-status-rejected", "mem-status-old"},
        )
        self.assertEqual(by_id["mem-status-cand"]["status"], "candidate")
        self.assertIn("候选", by_id["mem-status-cand"]["status_label"])
        self.assertIn("已停用", by_id["mem-status-retired"]["status_label"])
        self.assertIn("已否决", by_id["mem-status-rejected"]["status_label"])
        self.assertIn("已被替代", by_id["mem-status-old"]["status_label"])
        for entry_id in by_id:
            shown = self.show_entry(entry_id)
            self.assertEqual(shown["id"], entry_id)
            self.assertFalse(shown["is_current"])

    # ---------------------------------------------------------- T11 索引
    def test_t11_index_missing_corrupt_stale_does_not_change_facts(self) -> None:
        self.add(
            make_metadata("mem-index", subject_key="index.key", tags=["索引标记"]),
            make_body("索引降级条目", "索引不是事实来源。"),
        )
        self.cli_json(self.root, "reindex", "--json")
        index_file = self.root / "中枢" / "记忆" / "运行" / "index.json"
        self.assertTrue(index_file.is_file())
        self.assertEqual(self.search("索引标记", scenes=[])["index_status"], "fresh")
        self.assertTrue(self.cli_json(self.root, "reindex", "--check", "--json")["fresh"])

        index_file.unlink()
        missing = self.search("索引标记", scenes=[])
        self.assertEqual(missing["total_matched"], 1, "索引缺失仍以原文为准")
        self.assertEqual(missing["index_status"], "missing")
        stale_check = self.cli_json(self.root, "reindex", "--check", "--json")
        self.assertFalse(stale_check["fresh"], "--check 用 fresh=false 表达陈旧，且仍返回 0")
        self.cli_json(self.root, "reindex", "--json")
        self.assertTrue(self.cli_json(self.root, "reindex", "--check", "--json")["fresh"])

        index_file.write_text("{corrupt json", encoding="utf-8")
        corrupt = self.search("索引标记", scenes=[])
        self.assertEqual(corrupt["total_matched"], 1)
        self.assertEqual(corrupt["index_status"], "corrupt")
        self.cli_json(self.root, "reindex", "--json")
        self.assertTrue(self.cli_json(self.root, "reindex", "--check", "--json")["fresh"])

        entry = self.show_entry("mem-index")
        # 绕过 CLI 的人工编辑（不改 revision）→ 索引因内容哈希变化而陈旧，事实仍以原文为准
        entry_file = self.entry_path("mem-index")
        entry_file.write_bytes(
            entry_file.read_bytes() + "\n人工追加内容（索引应检测到哈希变化）。\n".encode("utf-8")
        )
        stale = self.search("索引标记", scenes=[])
        self.assertEqual(stale["index_status"], "stale")
        self.assertEqual(stale["total_matched"], 1)
        self.assertIn("人工追加内容", self.show_entry("mem-index")["body"])
        self.cli_json(self.root, "reindex", "--json")
        self.assertTrue(self.cli_json(self.root, "reindex", "--check", "--json")["fresh"])

    # ---------------------------------------------------------- T16 检索
    def test_t16_chinese_english_scoring_determinism_and_no_hit(self) -> None:
        self.add(
            make_metadata("mem-score-title", subject_key="score.title", tags=["scoretag"]),
            make_body("样例组件批量校验", "标题短语命中的条目。"),
        )
        self.add(
            make_metadata("mem-score-body", subject_key="score.body"),
            make_body("普通标题", "这里也提到样例组件，但没有完整标题短语。"),
        )
        self.add(
            make_metadata("mem-score-english", subject_key="score.english", tags=["deployment"]),
            make_body("英文标签条目", "部署相关记录。"),
        )
        first = self.search("样例组件批量校验", scenes=[])
        second = self.search("样例组件批量校验", scenes=[])
        self.assertGreaterEqual(first["total_matched"], 2)
        self.assertEqual(first["items"][0]["id"], "mem-score-title")
        self.assertGreater(first["items"][0]["score"], first["items"][1]["score"])
        self.assertTrue(first["items"][0]["matched"]["title_phrase"])
        self.assertEqual(
            [item["id"] for item in first["items"]], [item["id"] for item in second["items"]]
        )
        english = self.search("deployment", scenes=[])
        self.assertEqual([item["id"] for item in english["items"]], ["mem-score-english"])
        self.assertIn("deployment", english["items"][0]["matched"]["tag_tokens"])
        no_hit = self.search("zzz-nonexistent-token", scenes=[])
        self.assertEqual(no_hit["total_matched"], 0)
        self.assertEqual(no_hit["items"], [])
        self.cli_json(self.root, "search", "   ", "--cwd", str(self.root), "--json", expect=2)
        self.cli_json(
            self.root,
            "search",
            "技术",
            "--at",
            "2026-01-01",
            "--cwd",
            str(self.root),
            "--json",
            expect=2,
        )

    def test_t16_limit_and_deterministic_order(self) -> None:
        for index in range(3):
            self.add(
                make_metadata(
                    f"mem-limit-{index}", subject_key=f"limit.{index}", tags=["批量标记"]
                ),
                make_body(f"批量条目 {index}", f"批量结论 {index}。"),
            )
        limited = self.search("批量标记", scenes=[], limit=2)
        self.assertEqual(limited["total_matched"], 3)
        self.assertEqual(limited["returned"], 2)
        self.assertEqual(len(limited["items"]), 2)
        repeat = self.search("批量标记", scenes=[], limit=2)
        self.assertEqual(
            [item["id"] for item in limited["items"]],
            [item["id"] for item in repeat["items"]],
        )

    def test_t16_context_budget_trims_whole_items_and_keeps_json_valid(self) -> None:
        long_text = "这是一段很长的正文，用来触发上下文预算裁剪。" * 80
        for index in range(3):
            self.add(
                make_metadata(
                    f"mem-budget-{index}",
                    subject_key=f"budget.{index}",
                    pinned=True,
                    tags=["预算标记"],
                ),
                make_body(
                    f"预算条目 {index}",
                    f"预算结论 {index}。",
                    sections=[("使用边界", long_text), ("更多说明", "结尾条件必须保留。")],
                ),
            )
        result = self.context(max_chars=900)
        self.assertLessEqual(result["char_count"], 900)
        self.assertLessEqual(len(result["items"]), 8)
        self.assertGreater(len(result["items"]), 0)
        self.assertTrue(any(item["body_omitted"] for item in result["items"]))
        self.assertGreaterEqual(result["omitted_count"], 1)
        self.assertTrue(result["omitted_ids"], "整项丢弃时必须给出被丢弃条目 ID")
        self.assertTrue(
            any(cmd.startswith("show ") for cmd in result["more_commands"]),
            "必须提供定点读取命令",
        )
        self.assertIn("未进入上下文的条目", result["markdown"])
        self.assertIn("show", result["markdown"])
        for item in result["items"]:
            if item["body_omitted"]:
                self.assertIsNone(item["body"])
            self.assertGreater(item["rendered_chars"], 0)
        # 预算充足时按完整条目返回，不截断句子/否定条件
        full = self.context(max_chars=20000, query="预算标记")
        included = [item for item in full["items"] if not item["body_omitted"]]
        self.assertTrue(included)
        self.assertTrue(any("结尾条件必须保留。" in (item["body"] or "") for item in included))
        self.assertIn("结尾条件必须保留。", full["markdown"])

    # ---------------------------------------------------------- T17 冲突
    def test_t17_existing_conflicts_visible_and_not_hidden_by_topk(self) -> None:
        self.add(
            make_metadata("mem-conflict-a", subject_key="conflict.same", tags=["冲突双条目"]),
            make_body("冲突条目 A", "同键冲突内容 A。"),
        )
        # 手工复制成第二条同 subject_key/scope 的 active 条目（模拟绕过 CLI 的既存冲突）
        source = self.entry_path("mem-conflict-a").read_text(encoding="utf-8")
        copied = source.replace("mem-conflict-a", "mem-conflict-b")
        self.write_raw(
            "mem-conflict-b.md", copied, root=self.root / "中枢" / "记忆" / "条目"
        )
        validate = self.cli_json(self.root, "validate", "--json", expect=3)
        self.assertEqual(validate["counts"]["conflict_groups"], 1)
        group = validate["conflicts"][0]
        self.assertEqual(set(group["ids"]), {"mem-conflict-a", "mem-conflict-b"})
        self.assertIn("same_level_overlap", group["kinds"])

        search = self.search("冲突双条目", scenes=[], limit=1)
        self.assertEqual(len(search["items"]), 1, "top-k 只返回一条，但冲突不能被隐藏")
        self.assertEqual(len(search["conflicts"]), 1)
        self.assertEqual(set(search["conflicts"][0]["ids"]), {"mem-conflict-a", "mem-conflict-b"})
        self.assertIn("冲突", self.cli(self.root, "context", "--json").stdout)

    def test_t17_project_specific_exception_allowed_but_same_level_conflict_rejected(self) -> None:
        (self.root / "proj-a").mkdir()
        self.add(
            make_metadata(
                "mem-exception-ws", subject_key="exception.key", tags=["例外共享词"]
            ),
            make_body("全局偏好", "全局范围结论。"),
        )
        project = self.add(
            make_metadata(
                "mem-exception-project",
                subject_key="exception.key",
                level="project",
                target="proj-a",
                tags=["例外共享词"],
            ),
            make_body("项目 A 例外", "项目 A 更具体结论。"),
            expect=0,
        )
        self.assertEqual(project["result"], "created")
        validate = self.cli_json(self.root, "validate", "--json")
        self.assertEqual(validate["counts"]["conflict_groups"], 0)
        from_proj = self.search("例外共享词", cwd=self.root / "proj-a", scenes=[])
        self.assertEqual(
            {item["id"] for item in from_proj["items"]},
            {"mem-exception-ws", "mem-exception-project"},
        )
        self.assertEqual(from_proj["conflicts"], [])
        from_root = self.search("例外共享词", cwd=self.root, scenes=[])
        self.assertEqual({item["id"] for item in from_root["items"]}, {"mem-exception-ws"})

        same_level = self.add(
            make_metadata("mem-exception-ws2", subject_key="exception.key"),
            make_body("第二个全局条目", "同级重叠应冲突。"),
            expect=3,
        )
        self.assertIn("新", same_level["error"]["message"])
        self.assertEqual(self.cli_json(self.root, "validate", "--json")["counts"]["entries"], 2)


if __name__ == "__main__":
    unittest.main()
