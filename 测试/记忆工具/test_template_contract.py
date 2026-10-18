# -*- coding: utf-8 -*-
"""提取副本的最小补充合同：六字段配置、合法空库与只读命令的可写锁。"""

from __future__ import annotations

import ast
import unittest

from helpers import BASE_CONFIG, MEMORY_PY, IsolatedMemoryTestCase
from memlib.config import CONFIG_KEYS, MemoryConfig, load_config, validate_config
from memlib.errors import SchemaError


class TemplateContractTests(IsolatedMemoryTestCase):
    def test_public_config_api_keeps_exact_six_fields(self) -> None:
        expected = {
            "schema_version",
            "workspace_id",
            "lock_timeout_seconds",
            "query_limit",
            "context_max_chars",
            "candidate_review_days",
        }
        self.assertEqual(set(CONFIG_KEYS), expected)
        parsed = validate_config(dict(BASE_CONFIG), "虚构测试配置")
        self.assertIsInstance(parsed, MemoryConfig)
        self.assertEqual(parsed.to_dict(), BASE_CONFIG)
        self.assertEqual(load_config(self.root), parsed)
        for key in sorted(expected):
            with self.subTest(missing=key):
                data = dict(BASE_CONFIG)
                del data[key]
                with self.assertRaises(SchemaError):
                    validate_config(data, "缺字段测试")
        for extra in ("enabled", "template_version"):
            with self.subTest(unknown=extra):
                with self.assertRaises(SchemaError):
                    validate_config({**BASE_CONFIG, extra: True}, "未知字段测试")

    def test_entrypoint_keeps_parents_three_contract(self) -> None:
        # 仅解析模板源码；不在模板根运行 CLI 或读取任何记忆数据。
        tree = ast.parse(MEMORY_PY.read_bytes(), filename="memory.py")
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DEFAULT_ROOT" for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        expected = ast.parse("Path(__file__).resolve().parents[3]", mode="eval").body
        self.assertEqual(
            ast.dump(assignments[0].value, include_attributes=False),
            ast.dump(expected, include_attributes=False),
        )

    def test_empty_store_returns_empty_context_and_only_creates_lock(self) -> None:
        before = self.write_state_snapshot()
        context = self.context()
        self.assertEqual(context["data_status"], "ok")
        self.assertEqual(context["index_status"], "missing")
        self.assertEqual(context["total_applicable"], 0)
        self.assertEqual(context["items"], [])
        self.assertEqual(context["omitted_count"], 0)
        search = self.search("虚构空库查询")
        self.assertEqual(search["items"], [])
        self.assertEqual(search["total_matched"], 0)
        validate = self.cli_json(self.root, "validate", "--json")
        self.assertEqual(validate["counts"]["entries"], 0)
        self.assertEqual(self.write_state_snapshot(), before)
        run_dir = self.root / "中枢" / "记忆" / "运行"
        self.assertEqual(sorted(path.name for path in run_dir.iterdir()), ["memory.lock"])
        self.assertEqual(self.credentials(), [])

    def test_readonly_context_fails_when_lock_cannot_be_opened_for_write(self) -> None:
        # 用锁路径为目录的跨平台夹具稳定制造不可写打开；不依赖管理员权限或 chmod 语义。
        lock_path = self.root / "中枢" / "记忆" / "运行" / "memory.lock"
        lock_path.mkdir(parents=True)
        before = self.write_state_snapshot()
        data = self.cli_json(
            self.root, "context", "--cwd", str(self.root), "--json", expect=6
        )
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], 6)
        self.assertIn("锁文件", data["error"]["message"])
        self.assertEqual(self.write_state_snapshot(), before)
        self.assertEqual(self.credentials(), [])


if __name__ == "__main__":
    unittest.main()
