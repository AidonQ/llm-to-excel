# -*- coding: utf-8 -*-
"""输出契约（规格 §11）：build_record 写出的形状。"""
from __future__ import annotations

import unittest

from llm_column_extract.output import TableResult, build_record


class BuildRecordTest(unittest.TestCase):
    def test_tables_are_nested_not_flattened(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("产品", "数量"), (2, 2), "第 2 行是标题行区"),
            TableResult("E2:F4", ("地区", "金额"), (2, 2), "第 2 行是标题行区"),
        ], model="m", prompt_version="v1", attempts=1)
        self.assertEqual([t["range"] for t in doc["tables"]], ["A2:C4", "E2:F4"])

    def test_no_header_table_keeps_empty_columns_field(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "本表没有标题行区")],
            model="m", prompt_version="v1", attempts=1)
        self.assertIn("columns", doc["tables"][0])          # 字段必须在
        self.assertEqual(doc["tables"][0]["columns"], [])   # 必须是空数组

    def test_failed_table_is_written_with_its_error(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "", error="列数不符")],
            model="m", prompt_version="v1", attempts=2)
        self.assertNotIn("failed", doc["tables"][0])   # 不设第二个状态位
        self.assertEqual(doc["tables"][0]["error"], "列数不符")

    def test_paths_are_plain_strings(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("2024 年度 / Q1",), (2, 3), "r")],
            model="m", prompt_version="v1", attempts=1)
        self.assertIsInstance(doc["tables"][0]["columns"][0], str)


if __name__ == "__main__":
    unittest.main()
