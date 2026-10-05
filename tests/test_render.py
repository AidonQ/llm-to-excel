# -*- coding: utf-8 -*-
"""md 渲染（规格 §11.3）：md 由代码从 jsonl 渲染，不由模型生成。"""
from __future__ import annotations

import unittest

from llm_column_extract.output import TableResult, build_record
from llm_column_extract.render import render_markdown


class RenderTest(unittest.TestCase):
    def test_markdown_lists_every_table_with_its_range(self):
        md = render_markdown([build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("产品", "数量"), (2, 2), "第 2 行")],
            model="m", prompt_version="v1", attempts=1)], {"input": "in.jsonl"})
        self.assertIn("A2:C4", md)
        self.assertIn("产品", md)

    def test_markdown_shows_failed_tables_as_failed(self):
        md = render_markdown([build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "", error="列数不符")],
            model="m", prompt_version="v1", attempts=1)], {"input": "in.jsonl"})
        self.assertIn("列数不符", md)


if __name__ == "__main__":
    unittest.main()
