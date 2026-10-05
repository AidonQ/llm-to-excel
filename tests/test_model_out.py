# -*- coding: utf-8 -*-
"""模型输出兜底解析与校验（规格 §6、§6.1、§8）。

这是**唯一拦在「弱模型的回答」与「产品里写错的列名」之间的东西**，所以这里喂的全是坏输出：
markdown 围栏、前后杂字、截断、合法 JSON 但不是对象、漏列/多列、`header_rows` 越界或盖满整表、
自洽性违反、非字符串列名（规格 §11.2）。

模型调用不进单元测试（规格 §10）：本模块是纯函数，喂字符串、看 `ModelAnswer` 或
`ModelOutputError`。错误消息必须**说清哪个字段、看到什么、期望什么** —— Task 5 的重问循环
把它原样回传给模型（规格 §9「带具体错误重问一次」），所以消息内容本身也是被测行为。
"""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields

from llm_column_extract.model_out import (
    ModelAnswer,
    ModelOutputError,
    parse_model_output,
)

#: 一份完全合规的回答（两层标题行区，路径用 " / " 拼接）
GOOD = (
    '{"header_rows": [1, 2], "columns": ["2024 年度 / Q1", "2024 年度 / Q2"], '
    '"reason": "行1 是标题行区的最下面一行之上的那一行，行2 是标题行区的最下面一行，行3 起是数据"}'
)


class ParseModelOutputTest(unittest.TestCase):
    """`parse_model_output(raw, *, n_columns, n_rows)`：纯函数进、纯函数出。"""

    def parse(self, raw, n_columns=2, n_rows=5):
        return parse_model_output(raw, n_columns=n_columns, n_rows=n_rows)

    def assert_rejected(self, raw, *expected, n_columns=2, n_rows=5):
        """断言被拒，且消息里带上指定关键字（消息要能回传给模型，规格 §9）。"""
        with self.assertRaises(ModelOutputError) as ctx:
            self.parse(raw, n_columns=n_columns, n_rows=n_rows)
        message = str(ctx.exception)
        self.assertTrue(message.strip(), "错误消息不能是空的，它要回传给模型")
        for token in expected:
            self.assertIn(token, message)
        return message

    # ---------------------------------------------------------------- 正常路径

    def test_plain_json(self):
        answer = self.parse(GOOD)
        self.assertEqual((1, 2), answer.header_rows)
        self.assertEqual(("2024 年度 / Q1", "2024 年度 / Q2"), answer.columns)
        self.assertIn("行1", answer.reason)

    def test_columns_are_a_tuple_of_str(self):
        # 规格 §11.2 的硬约束在这一层就得钉住：下游 str() 会把对象悄悄毁掉，而且不报错
        answer = self.parse(GOOD)
        self.assertIsInstance(answer.columns, tuple)
        for entry in answer.columns:
            self.assertIsInstance(entry, str)

    def test_strips_markdown_fence(self):
        # 提示词说了「不要 markdown 围栏」，但弱模型照样会给（规格 §8 的兜底解析）
        for template in ("```json\n{}\n```", "```JSON\n{}\n```", "```\n{}\n```", "```json {} ```"):
            with self.subTest(template=template):
                answer = self.parse(template.format(GOOD))
                self.assertEqual(("2024 年度 / Q1", "2024 年度 / Q2"), answer.columns)

    def test_tolerates_leading_and_trailing_prose(self):
        raw = "好的，我的判断是：\n" + GOOD + "\n以上，请核对。"
        self.assertEqual((1, 2), self.parse(raw).header_rows)

    def test_prose_braces_do_not_confuse_the_object_slice(self):
        # 前后杂字里也可能有花括号（这里就是「{表范围}」）：剥围栏必须发生在切片之前
        raw = "关于 {表范围} 的判断如下：\n```json\n" + GOOD + "\n```\n完。"
        self.assertEqual(("2024 年度 / Q1", "2024 年度 / Q2"), self.parse(raw).columns)

    def test_only_fence_stripping_can_rescue_this(self):
        # 只有「剥围栏」能救回来的一例：围栏**外**的花括号让切片彻底跑偏
        # （杂字里那个 `{` 没有配对的 `}`，切片取到的区间根本不是 JSON）。
        # 这条把 `_strip_fence` 钉死 —— 上面那条 `test_strips_markdown_fence` 做不到。
        raw = "判断依据是「{表范围}」这句话。\n```json\n" + GOOD + "\n```\n完。"
        answer = self.parse(raw)
        self.assertEqual((1, 2), answer.header_rows)
        self.assertEqual(("2024 年度 / Q1", "2024 年度 / Q2"), answer.columns)

    def test_empty_fence_does_not_shadow_the_real_answer(self):
        # 空围栏只是模型开了个围栏又没写；不能因此把后面那个**合格对象**丢掉，
        # 更不能报「模型输出是空的」——那会把 Task 5 的重问引向错误的方向
        raw = '```\n```\n{"header_rows": [1, 1], "columns": ["a", "b"], "reason": "x"}'
        answer = self.parse(raw)
        self.assertEqual((1, 1), answer.header_rows)
        self.assertEqual(("a", "b"), answer.columns)

    def test_ordinary_fenced_object_still_parses(self):
        # 收紧「空围栏不遮蔽」之后，普通围栏必须照旧能解析
        raw = '```json\n{"header_rows": [1, 1], "columns": ["a", "b"], "reason": "x"}\n```'
        answer = self.parse(raw)
        self.assertEqual((1, 1), answer.header_rows)
        self.assertEqual(("a", "b"), answer.columns)

    def test_reason_is_preserved(self):
        # 规格 §6：reason 是审计线索；原样带出来，一个字都不许改
        reason = "行1 是标题行区的最下面一行之上的那一行（2024 年度 横跨 Q1–Q4，重复取值），行2 是标题行区的最下面一行"
        raw = (
            '{"header_rows": [1, 2], "columns": ["2024 年度 / Q1", "2024 年度 / Q2"], '
            '"reason": "%s"}' % reason
        )
        self.assertEqual(reason, self.parse("```json\n" + raw + "\n```").reason)

    def test_paths_are_returned_verbatim_without_parsing(self):
        # 本任务只管「非空且是字符串」：不解析路径、不归一化空格（规格 §6、§11.2）
        raw = '{"header_rows": [1, 1], "columns": [" 产品 ", "2024 年度 / Q1"], "reason": "x"}'
        self.assertEqual((" 产品 ", "2024 年度 / Q1"), self.parse(raw).columns)

    def test_null_answer_is_accepted_on_a_multi_column_table(self):
        # 歧义裁定：header_rows 为 null 时压根没有列路径，列数检查不适用。
        # 这条要钉死，否则「3 列的表判成没有标题行区」会被列数检查无条件打回。
        raw = '{"header_rows": null, "columns": [], "reason": "第一行就是数据（100/200/300），本表没有标题行区"}'
        answer = self.parse(raw, n_columns=3, n_rows=3)
        self.assertIsNone(answer.header_rows)
        self.assertEqual((), answer.columns)
        self.assertIn("没有标题行区", answer.reason)

    def test_answer_has_the_contract_fields(self):
        # Task 6 按这三个字段消费，名字就是接口
        self.assertEqual(
            ["header_rows", "columns", "reason"], [f.name for f in fields(ModelAnswer)]
        )

    def test_answer_is_frozen(self):
        # 契约说好是 frozen：Task 5 / Task 6 之外的任何一层都不许改一条已校验的回答。
        # 只查字段名，挡不住后来有人把 frozen=True 拿掉。
        answer = self.parse(GOOD)
        with self.assertRaises(FrozenInstanceError):
            answer.header_rows = (1, 1)
        with self.assertRaises(FrozenInstanceError):
            answer.columns = ("a", "b")
        with self.assertRaises(FrozenInstanceError):
            answer.reason = "改了"

    def test_dimensions_are_keyword_only(self):
        # Task 5 只按关键字传 n_columns / n_rows，位置传参要立刻炸出来
        with self.assertRaises(TypeError):
            parse_model_output(GOOD, 2, 5)

    # ------------------------------------------------- 校验一：header_rows 区间

    def test_header_rows_out_of_range_raises(self):
        for bad, tokens in (
            ("[0, 1]", ("越界", "5")),   # 起 < 1
            ("[-1, 2]", ("越界", "5")),
            ("[1, 6]", ("越界", "5")),   # 止 > 表行数
            ("[3, 2]", ("反序",)),        # 起 > 止：与越界是两回事，消息要分开
        ):
            with self.subTest(bad=bad):
                raw = '{"header_rows": %s, "columns": ["a", "b"], "reason": "x"}' % bad
                self.assert_rejected(raw, "header_rows", *tokens)

    def test_header_rows_not_a_pair_raises(self):
        for bad in ("[2]", "[1, 2, 3]", "[]", "2", '"1"', "{}"):
            with self.subTest(bad=bad):
                raw = '{"header_rows": %s, "columns": ["a", "b"], "reason": "x"}' % bad
                self.assert_rejected(raw, "header_rows", "[起, 止]", "null")

    def test_header_rows_non_integer_entries_raise(self):
        # true 在 Python 里是 int 的子类，必须专门挡住，否则会被静默当成 1
        for bad in ('[true, 1]', '[1, "2"]', "[1.0, 2]", "[null, 1]", "[[1], [2]]"):
            with self.subTest(bad=bad):
                raw = '{"header_rows": %s, "columns": ["a", "b"], "reason": "x"}' % bad
                self.assert_rejected(raw, "header_rows", "整数")

    def test_header_rows_covering_every_row_raises(self):
        # Review Focus #2：区间本身合法（1 ≤ 起 ≤ 止 ≤ 表行数），但盖满整表 ⇒ 没有数据区，答案非法。
        # 必须与普通越界区分开（消息不同），否则重问时模型不知道该改哪一头。
        raw = '{"header_rows": [1, 3], "columns": ["a", "b"], "reason": "x"}'
        message = self.assert_rejected(raw, "header_rows", "数据区", n_rows=3)
        self.assertNotIn("越界", message)

        # 一行表同理：[1, 1] 就盖满了，等于没有数据区
        self.assert_rejected(
            '{"header_rows": [1, 1], "columns": ["a", "b"], "reason": "x"}',
            "数据区",
            n_rows=1,
        )

    def test_header_rows_ending_at_the_last_row_raises(self):
        # 「没有数据区」的判定看的是**止**，不是**起**：标题行区贴着最后一行 ⇒ 下面没有数据区。
        # 3 行表答 [2, 3]：起 > 1，所以旧的 `first == 1 and last == n_rows` 抓不到它。
        message = self.assert_rejected(
            '{"header_rows": [2, 3], "columns": ["a", "b"], "reason": "x"}',
            "header_rows",
            "数据区",
            n_rows=3,
        )
        self.assertNotIn("越界", message)

        # 2 行表答 [2, 2]：正是 prompt 例 3 的形状（标题行 + 列名行）。
        # 若放它过去，Task 6 会给一张**零数据行**的表写列名。
        self.assert_rejected(
            '{"header_rows": [2, 2], "columns": ["a", "b"], "reason": "x"}',
            "数据区",
            n_rows=2,
        )

        # 反面：止 < 表行数（下面还有数据区）必须照旧通过，别把判定收过头
        answer = self.parse(
            '{"header_rows": [2, 2], "columns": ["a", "b"], "reason": "x"}', n_rows=3
        )
        self.assertEqual((2, 2), answer.header_rows)

    # ------------------------------------------------ 校验二：null 与 [] 自洽

    def test_null_header_rows_requires_empty_columns(self):
        # 自洽性（规格 §6.1 第 2 行）：null 必须配 []，配了路径就是自相矛盾
        raw = '{"header_rows": null, "columns": ["a", "b"], "reason": "x"}'
        self.assert_rejected(raw, "header_rows", "null", "columns", "空数组")

    def test_empty_columns_requires_null_header_rows(self):
        # 反方向也要查：给了区间却给空数组 ⇒ 模型没说清这一列是什么
        raw = '{"header_rows": [1, 1], "columns": [], "reason": "x"}'
        self.assert_rejected(raw, "columns", "空数组", "null", n_columns=2)

    # --------------------------------------------------- 校验三：columns 形状

    def test_non_string_column_raises(self):
        # 规格 §11.2：对象（如 {"name": "Q1"}）会被下游 str() 字符串化成一坨，
        # 列名全错而且不报错 —— 这一层必须拒收，绝不 str() 转换
        for bad in ('{"name": "Q1"}', "1", "null", "true", '[["a"]]'):
            with self.subTest(bad=bad):
                raw = '{"header_rows": [1, 1], "columns": [%s, "b"], "reason": "x"}' % bad
                self.assert_rejected(raw, "columns", "第 1 项", "字符串")

    def test_empty_string_column_raises(self):
        # 单个空路径（藏在合规路径后面）＝ 模型没判出这一列。宁可失败也不写空列名
        raw = '{"header_rows": [1, 1], "columns": ["产品", ""], "reason": "x"}'
        self.assert_rejected(raw, "columns", "第 2 项", "空")

    def test_all_empty_column_path_raises(self):
        # Review Focus #1：全是空路径、列数还恰好对得上 —— 最危险的一种，绝不能滑过去
        raw = '{"header_rows": [1, 1], "columns": ["", ""], "reason": "x"}'
        self.assert_rejected(raw, "columns", "第 1 项", "空")

    def test_whitespace_only_column_raises(self):
        # 只有空白的路径等于空列名（提示词要求「空单元格跳过」，跳过 ≠ 留一个空白）
        raw = '{"header_rows": [1, 1], "columns": ["产品", "   "], "reason": "x"}'
        self.assert_rejected(raw, "columns", "第 2 项", "空")

    # ----------------------------------------------------- 校验四：列数对齐源

    def test_column_count_must_match_input(self):
        # 这是唯一能与源对齐的硬检查，漏列/多列都要抓（规格 §6.1 第 4 行）；
        # 消息要同时给出「源有多少列」和「模型给了多少列」，否则重问时模型无从下手
        fewer = '{"header_rows": [1, 1], "columns": ["a"], "reason": "x"}'
        more = '{"header_rows": [1, 1], "columns": ["a", "b", "c"], "reason": "x"}'
        self.assert_rejected(fewer, "列数不符", "输入里有 2 列", "给了 1 列", n_columns=2)
        self.assert_rejected(more, "列数不符", "输入里有 2 列", "给了 3 列", n_columns=2)

    # --------------------------------------------------------- 坏输入不许崩

    def test_non_object_json_raises(self):
        # Review Focus #3：合法 JSON 但不是对象 —— 包括把一个合格对象包在数组里（那是形状不对，
        # 不是「前后杂字」，不许悄悄剥掉外层）
        for raw in (
            '[{"header_rows": [1, 1], "columns": ["a", "b"], "reason": "x"}]',
            '"ok"',
            "42",
            "null",
            "true",
        ):
            with self.subTest(raw=raw):
                self.assert_rejected(raw, "JSON 对象")

    def test_broken_json_raises(self):
        # 规格 §8：JSON 坏了要报错而不是猜
        cases = (
            ('{"header_rows": [1, 1], "columns": ["产品", "数量"]', ("JSON", "截断")),
            ('{"header_rows": [1, 1] "columns": ["a", "b"], "reason": "x"}', ("JSON", "坏")),
            ("", ("空",)),
            ("我这张表判不出来", ("JSON", "找不到")),
        )
        for raw, tokens in cases:
            with self.subTest(raw=raw):
                self.assert_rejected(raw, *tokens)

    def test_missing_header_rows_key_raises(self):
        # 缺键与显式 null 是两回事：绝不能拿 .get() 把「忘了写」当成「没有标题行区」
        # 两种都不能滑过去：columns 恰好是空数组（看起来像「没有标题行区」）也不行
        self.assert_rejected('{"columns": [], "reason": "x"}', "header_rows")
        self.assert_rejected('{"columns": ["a", "b"], "reason": "x"}', "header_rows")

    def test_missing_columns_key_raises(self):
        # 同理：缺 columns 不能被当成空数组（那会把一张真表判成「没有标题行区」）
        self.assert_rejected('{"header_rows": null, "reason": "x"}', "columns")
        self.assert_rejected('{"header_rows": [1, 1], "reason": "x"}', "columns")

    def test_missing_reason_raises(self):
        # 规格 §6：reason 必填；没有它就分不清「提示词理解错了」还是「顺从了错误的表范围」
        for raw in (
            '{"header_rows": [1, 1], "columns": ["a", "b"]}',
            '{"header_rows": [1, 1], "columns": ["a", "b"], "reason": null}',
            '{"header_rows": [1, 1], "columns": ["a", "b"], "reason": 42}',
        ):
            with self.subTest(raw=raw):
                self.assert_rejected(raw, "reason")

    def test_blank_reason_raises(self):
        for raw in (
            '{"header_rows": [1, 1], "columns": ["a", "b"], "reason": ""}',
            '{"header_rows": [1, 1], "columns": ["a", "b"], "reason": "   "}',
        ):
            with self.subTest(raw=raw):
                self.assert_rejected(raw, "reason")


if __name__ == "__main__":
    unittest.main()
