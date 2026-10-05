# -*- coding: utf-8 -*-
"""模型调用与**有界重试**（规格 §8、§9、§10）。

模型调用不进单元测试（规格 §10）：所有用例一律经 `tests/_fake_client.py` 的 `FakeClient`，
**没有网络、没有模型**也能把重试与放弃路径测穿。

这里盯住两件容易被做丢的事：

1. **重问必须把具体错误回传** —— 第二次请求的 messages 里要出现第一次失败的那段说明
   （`parse_model_output` 的消息本就是为回传写的）；否则「重问」只是把同一个问题再问一遍。
2. **传输异常也要有界** —— 超时 / 网关 5xx 一样重试到上限就抛，且抛出的东西仍**带表范围**，
   让人能从 500 条批次的日志里认出是哪张表死了。
"""
from __future__ import annotations

import unittest

from _fake_client import FakeClient
from llm_column_extract.client import ask_table
from llm_column_extract.input import InputTable
from llm_column_extract.model_out import ModelOutputError, parse_model_output
from llm_column_extract.prompt import build_messages

#: 3 列 4 行的一张表（表范围 A2:C5 声明的正好是 4 行）
TABLE = InputTable(
    "A2:C5",
    (
        ("产品", "甲", "乙", "丙"),
        ("数量", "1", "2", "3"),
        ("单价", "10", "20", "30"),
    ),
)

#: 合规回答：单层标题行区，路径就是**标题行区的最下面一行**的值本身
GOOD = (
    '{"header_rows": [1, 1], "columns": ["产品", "数量", "单价"], '
    '"reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据"}'
)

#: 不合规回答：漏了一列（本表 3 列，它只给 2 列）
BAD = '{"header_rows": [1, 1], "columns": ["产品", "数量"], "reason": "漏了一列"}'


def bad_output_error() -> str:
    """`BAD` 会被 `parse_model_output` 判成什么错 —— 重问消息里应当**原样**出现这段文本。"""
    try:
        parse_model_output(BAD, n_columns=3, n_rows=4)
    except ModelOutputError as exc:
        return str(exc)
    raise AssertionError("BAD 应当被 parse_model_output 拒收，否则这组用例测的不是重问")


class AskTableTest(unittest.TestCase):
    """`ask_table(client, table, *, max_attempts=2)`：进的是预置响应，出的是答案或异常。"""

    def test_returns_answer_on_first_success(self):
        client = FakeClient([GOOD])

        answer = ask_table(client, TABLE)

        self.assertEqual(answer.header_rows, (1, 1))
        self.assertEqual(answer.columns, ("产品", "数量", "单价"))
        # 第一次请求就是 Task 3 渲染的那两条消息，没被改动
        self.assertEqual(client.calls, [build_messages(TABLE)])

    def test_retries_once_on_bad_output_and_passes_the_error_back(self):
        client = FakeClient([BAD, GOOD])

        answer = ask_table(client, TABLE)

        self.assertEqual(answer.columns, ("产品", "数量", "单价"))
        self.assertEqual(len(client.calls), 2)

        first, second = client.calls
        self.assertEqual(second[:2], first)  # 最初的 system + user 原样保留
        self.assertEqual(len(second), 3)  # 只多一条：把错误说清楚的那条
        self.assertEqual(second[2]["role"], "user")
        # 这就是重问的意义：第二次请求里必须带第一次失败的具体原因
        self.assertIn(bad_output_error(), second[2]["content"])

    def test_gives_up_after_max_attempts(self):
        client = FakeClient([BAD, BAD])

        with self.assertRaises(ModelOutputError) as ctx:
            ask_table(client, TABLE)

        # 默认上限 2 = 第一次 + 重问一次，不再多问
        self.assertEqual(len(client.calls), 2)
        self.assertIn(TABLE.range, str(ctx.exception))  # 日志里要认得出是哪张表

        # 上限就是「对 client.complete 的调用次数」：max_attempts=1 时一次都不重问
        once = FakeClient([BAD])
        with self.assertRaises(ModelOutputError):
            ask_table(once, TABLE, max_attempts=1)
        self.assertEqual(len(once.calls), 1)

    def test_max_attempts_must_be_at_least_one(self):
        # 0 或负数只会让循环一次都不跑（然后悄悄什么都返回），所以宁可直接报错
        with self.assertRaises(ValueError):
            ask_table(FakeClient([GOOD]), TABLE, max_attempts=0)

    def test_transport_error_is_retried(self):
        client = FakeClient([TimeoutError("网关超时"), GOOD])

        answer = ask_table(client, TABLE)

        self.assertEqual(answer.columns, ("产品", "数量", "单价"))
        self.assertEqual(len(client.calls), 2)
        # 传输异常没有「上一次的回答」可纠正，第二次请求必须原样重发
        self.assertEqual(client.calls[1], client.calls[0])

    def test_transport_error_gives_up_bounded(self):
        client = FakeClient([TimeoutError("网关超时"), TimeoutError("网关又超时")])

        with self.assertRaises(TimeoutError) as ctx:  # 类型不变：调用方仍能按传输异常分辨
            ask_table(client, TABLE)

        self.assertEqual(len(client.calls), 2)
        message = str(ctx.exception)
        self.assertIn(TABLE.range, message)  # 哪张表
        self.assertIn("网关又超时", message)  # 最后一次的原因


class FakeClientTest(unittest.TestCase):
    """测试替身自己的契约：照序发响应、异常照抛、记下每次请求、没响应了要吵。"""

    def test_serves_responses_in_order_and_records_messages(self):
        client = FakeClient([ValueError("坏了"), "好"])

        with self.assertRaises(ValueError):
            client.complete([{"role": "user", "content": "一"}])
        self.assertEqual(client.complete([{"role": "user", "content": "二"}]), "好")
        self.assertEqual([call[0]["content"] for call in client.calls], ["一", "二"])

    def test_running_out_of_responses_says_so(self):
        client = FakeClient([])

        with self.assertRaises(AssertionError) as ctx:
            client.complete([])

        self.assertIn("预置响应", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
