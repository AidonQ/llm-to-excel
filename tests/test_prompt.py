# -*- coding: utf-8 -*-
"""提示词渲染（规格 §7）。这是全组件唯一由规格逐字钉死文本的地方。

术语用权威表述：**表范围**、**标题行区**、**数据区**；局部术语只出现在交付的提示词文本里，
且每次出现都必须同时给出权威表述「标题行区的最下面一行」（规格 §3、§7.1）。
"""
from __future__ import annotations

import unittest

from llm_column_extract.input import InputTable
from llm_column_extract.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_messages,
    render_user_message,
)


class PromptTest(unittest.TestCase):
    def test_row_numbers_start_at_one_and_are_labelled_as_in_table(self):
        msg = render_user_message(InputTable("A2:C4", (("产品", "A", "B"),)))
        self.assertIn("行1", msg)          # 1-based，且明确是「表内」
        self.assertNotIn("行0", msg)

    def test_authoritative_term_is_defined_inline(self):
        self.assertIn("标题行区", SYSTEM_PROMPT)
        # 权威表述必须与局部说明同时出现（规格 §7.1）
        self.assertIn("最下面", SYSTEM_PROMPT)

    #: 禁用词本词。分开拼写是刻意的：本断言要扫本文件的源码，若这里写成连续的两个字，
    #: 源码自身就会命中，断言永远失败（反之，写成连续形式也说明它压根没在扫源码）。
    FORBIDDEN_TERM = "表" + "头"
    #: 唯一允许的定式（规格 §3 的例外）；扫描前先整词剔除。
    ALLOWED_TERM = "分组表头"

    def test_forbidden_terms_never_appear_alone(self):
        # 规格 §3 的硬要求：允许的唯一定式是「分组表头」，光杆禁用词一律不许出现。
        # 同一约束也要钉住承载它的代码：交付的提示词与本文件的源码一起扫。
        with open(__file__, encoding="utf-8") as source_file:
            source_text = source_file.read()
        for text, origin in ((SYSTEM_PROMPT, "SYSTEM_PROMPT"), (source_text, __file__)):
            self.assertNotIn(self.FORBIDDEN_TERM, text.replace(self.ALLOWED_TERM, ""), origin)

    def test_no_header_exit_is_offered(self):
        self.assertIn("header_rows", SYSTEM_PROMPT)
        self.assertIn("null", SYSTEM_PROMPT)

    def test_reason_is_required(self):
        self.assertIn("reason", SYSTEM_PROMPT)

    def test_all_four_few_shots_present(self):
        for marker in ("单层", "分组表头", "标题行", "没有标题行区"):
            self.assertIn(marker, SYSTEM_PROMPT)

    #: 三个带标题行区的 few-shot 的输出 JSON，逐字钉死。
    #: `header_rows` 是**表内 1-based**（规格 §6、§6.1 的界 `1 ≤ a ≤ b ≤ 表行数`、§12.2 第 2 条），
    #: 与表范围的起始行（`A2` 的 2）无关；这三个 JSON 就是弱模型模仿的示范（§7.3）。
    FEW_SHOT_JSONS = (
        # 例 1：A2:C5，标题行区 = 表内行1 ⇒ [1, 1]（不是工作表行 2）
        '{"header_rows": [1, 1], "columns": ["产品", "数量", "单价"], '
        '"reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据"}',
        # 例 2：A2:D5，标题行区 = 表内行1–行2 ⇒ [1, 2]（不是工作表行 2–3）
        '{"header_rows": [1, 2], "columns": ["2024 年度 / Q1", "2024 年度 / Q2", '
        '"2024 年度 / Q3", "2024 年度 / Q4"], '
        '"reason": "行1 是标题行区的最下面一行之上的那一行（2024 年度 横跨 Q1–Q4，重复取值），'
        '行2 是标题行区的最下面一行，行3 起是数据"}',
        # 例 3：A2:D5，标题行区 = 表内行2 ⇒ [2, 2]（不是工作表行 3）
        '{"header_rows": [2, 2], "columns": ["产品", "Q1", "Q2", "Q3"], '
        '"reason": "行1 是标题行（横跨整表宽度、描述整张表），排除；'
        '行2 是列名行（标题行区的最下面一行）"}',
    )

    def test_few_shot_json_pins_header_rows_to_table_internal_rows(self):
        for expected in self.FEW_SHOT_JSONS:
            self.assertIn(expected, SYSTEM_PROMPT)
        # 反面：三个例子的表范围都以 A2 起，一旦把「起始行 + 表内行 − 1」写进示范，
        # 就会出现这些错值。它们必须一个都不在（否则模型学会整体偏移）。
        for wrong in (
            '{"header_rows": [2, 2], "columns": ["产品", "数量", "单价"]',
            '{"header_rows": [2, 3], "columns": ["2024 年度 / Q1"',
            '{"header_rows": [3, 3], "columns": ["产品", "Q1", "Q2", "Q3"]',
        ):
            self.assertNotIn(wrong, SYSTEM_PROMPT)

    def test_user_message_carries_the_range_and_a_row_numbered_grid(self):
        # `columns` 是每列内容（列优先），渲染成逐行网格时要转置；单元格照抄原文
        msg = render_user_message(
            InputTable("A2:C4", (("产品", "A", "B"), ("数量", "1", "2"), ("单价", "0.25", "0.5")))
        )
        self.assertIn("表范围 A2:C4", msg)
        self.assertIn("行1 | 产品 | 数量 | 单价", msg)
        self.assertIn("0.25", msg)
        self.assertNotIn("行4", msg)  # 表范围声明 3 行，不该出现第 4 行

    def test_messages_are_a_system_and_a_user_turn(self):
        messages = build_messages(InputTable("A2:C4", (("产品", "A", "B"),)))
        self.assertEqual(["system", "user"], [message["role"] for message in messages])
        self.assertEqual(SYSTEM_PROMPT, messages[0]["content"])
        self.assertIn("表范围 A2:C4", messages[1]["content"])

    def test_prompt_gives_no_hint_of_our_own(self):
        # 规格 §7.5：不给模型任何我们的推断或候选
        for hint in ("推测", "候选", "可能是第", "建议"):
            self.assertNotIn(hint, SYSTEM_PROMPT)

    def test_prompt_version_is_pinned(self):
        self.assertEqual("v1", PROMPT_VERSION)

    def test_table_without_rows_does_not_crash(self):
        # 形状异常的空表：不崩、也不编行
        self.assertIn("表范围 A2:A2", render_user_message(InputTable("A2:A2", ())))


if __name__ == "__main__":
    unittest.main()

