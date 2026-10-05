# -*- coding: utf-8 -*-
"""编排（规格 §9）：逐记录、逐表隔离、部分成功照样出产物。

模型调用不进单元测试（规格 §10）：一律经 `tests/_fake_client.py` 的 `FakeClient`，
预置响应里既有坏 JSON 也有传输异常（`TimeoutError`），无网络、无模型也能把失败路径测穿。

这里盯住六件事：

1. **逐表隔离** —— 一张表炸掉，同记录里其余各表照常出结果（规格 §9 第一、二行）。
2. **异常面是 `Exception`** —— 网关可能抛 `TimeoutError`、连接错误或任何第三方异常；
   只捕解析异常会让**整批**死在一次网络故障上（规格 §9 点名的那个坑）。失败表的 `error`
   必须有字：异常自带空文本时退到类型名。
3. **部分成功就是产物** —— 失败的表带 `error`，`columns` 是空元组、`header_rows` 是 `None`，
   绝不写空列名；整条记录照样写出来。
4. **逐记录隔离** —— 一条记录解析不了不打断整批：坏记录被跳过，其余各条仍有结果。
5. **坏记录不伪造身份** —— `ID`/`FileName`/`sheet` 是空串（哨兵只留在 `error` 的人读文本里），
   位置由结构化记录级键 `source_line`/`record_index` 给出，且只在这种失败记录上出现。
6. **产物顺序 = 输入顺序** —— 由扫描顺序决定，不依赖调用方给的 `record_index`；而空行的
   计数口径与 `input.load_records` 由同一份字节钉在一起。

术语一律用权威表述：**表范围**、**标题行区**、**数据区**。
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

from _fake_client import FakeClient
from llm_column_extract.pipeline import (
    InputLine,
    number_lines,
    parse_lines,
    run_batch,
    run_pipeline,
)
from llm_column_extract.input import (
    InputFormatError,
    InputRecord,
    InputTable,
    load_records,
    parse_record,
)
from llm_column_extract.output import TableResult, build_record
from llm_column_extract.prompt import PROMPT_VERSION
from llm_column_extract.render import render_markdown

#: 3 列 4 行的一张表（表范围 A2:C5 声明的正好是 4 行）
TABLE_A = InputTable(
    "A2:C5",
    (
        ("产品", "甲", "乙", "丙"),
        ("数量", "1", "2", "3"),
        ("单价", "10", "20", "30"),
    ),
)

#: 另一张表，行内容与上一张不同 —— 模型回答因此可以分辨是哪张表被问了
TABLE_B = InputTable(
    "E2:F5",
    (
        ("地区", "东部", "西部", "北部"),
        ("金额", "1", "2", "3"),
    ),
)

#: 合规回答：单层标题行区，路径就是**标题行区的最下面一行**的值本身
GOOD_A = (
    '{"header_rows": [1, 1], "columns": ["产品", "数量", "单价"], '
    '"reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据"}'
)
GOOD_B = (
    '{"header_rows": [1, 1], "columns": ["地区", "金额"], '
    '"reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据"}'
)

#: 坏 JSON：拒收 ⇒ 该表在有界重试（两次）后标记失败
BAD_JSON = "这不是 JSON，模型跑题了"


class TextlessGatewayError(Exception):
    """一条**没有文本**的异常：`str(exc)` 是空串。

    异常面是**整个 `Exception`**（规格 §9），所以编排不能假设异常一定带文本。`client.py`
    会把放弃原因写进 `exc.args`，但这对**不读 `args` 的 `__str__`** 不生效（OSError 族的
    errno 分支同理：`args` 改写被 `str()` 忽略）。真实网关的库异常也可能长这样。
    """

    def __str__(self):
        return ""


def input_record(record_id: str, tables, file_name: str = "book.xlsx", sheet: str = "Sheet1"):
    """一条已解析好的记录（`parse_record` 的产物形状）。"""
    return InputRecord(record_id, file_name, sheet, tuple(tables))


def raw_record(record_id: str, tables) -> str:
    """一条记录的 jsonl 原文：`ID` + 每张表用**表范围**当键、列名是列键的数组。"""
    tables_json = []
    for table in tables:
        body = ['"range": "{}"'.format(table.range)]
        for index, cells in enumerate(table.columns, start=1):
            body.append(
                '"列{}": [{}]'.format(index, ", ".join('"{}"'.format(cell) for cell in cells))
            )
        tables_json.append("{" + ", ".join(body) + "}")
    return '{{"ID": "{0}", "FileName": "book.xlsx", "sheet": "Sheet1", "tables": [{1}]}}'.format(
        record_id, ", ".join(tables_json)
    )


class PipelineTest(unittest.TestCase):
    """`run_pipeline(records, client, *, model=..., max_attempts=...)`。"""

    def test_happy_path_one_record_two_tables(self):
        # 一条记录、两张表：两张表各自问一次、各自出一份结果，逐表不拍平
        client = FakeClient([GOOD_A, GOOD_B])
        record = input_record("7", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="deepseek-v4", max_attempts=1)

        self.assertEqual(len(products), 1)
        doc = products[0]
        self.assertEqual(doc["ID"], "7")
        self.assertEqual(doc["FileName"], "book.xlsx")
        self.assertEqual(doc["sheet"], "Sheet1")
        self.assertEqual([t["range"] for t in doc["tables"]], ["A2:C5", "E2:F5"])
        self.assertEqual(
            [t["columns"] for t in doc["tables"]],
            [["产品", "数量", "单价"], ["地区", "金额"]],
        )
        # 诊断字段直接放在记录里，且两个来源都指得出来（裁决：prompt_version 取单一来源常量，
        # attempts 写本次运行的调用上界）
        self.assertEqual(doc["model"], "deepseek-v4")
        self.assertEqual(doc["prompt_version"], PROMPT_VERSION)
        self.assertEqual(doc["attempts"], 1)
        # 成功就不带噪声
        self.assertNotIn("error", doc["tables"][0])
        self.assertEqual(len(client.calls), 2)

    def test_one_failed_table_does_not_lose_the_other(self):
        # 第一张表永远返回坏 JSON ⇒ 该表 failed；第二张表照常 match
        client = FakeClient([BAD_JSON, BAD_JSON, GOOD_B])
        record = input_record("7", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="m", max_attempts=2)

        first, second = products[0]["tables"]
        self.assertEqual(first["range"], "A2:C5")
        self.assertEqual(first["columns"], [])  # 不写空列名：失败的表一列都不给
        self.assertIsNone(first["header_rows"])
        self.assertIsNotNone(first["error"])
        # 另一张表照常有结果 —— 一张表炸掉不影响其余各表
        self.assertEqual(second["range"], "E2:F5")
        self.assertEqual(second["columns"], ["地区", "金额"])
        self.assertEqual(second["header_rows"], [1, 1])
        self.assertNotIn("error", second)
        # 失败表的两次重问之后，第二张表还是被照常问了（隔离，不是提前收工）
        self.assertEqual(len(client.calls), 3)

    def test_record_is_still_written_when_a_table_fails(self):
        # 规格 §9：「单表失败 ⇒ 该记录照样写出来」。两张表都失败时记录也不能丢
        client = FakeClient([BAD_JSON, BAD_JSON, BAD_JSON, BAD_JSON])
        record = input_record("9", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="m", max_attempts=2)

        self.assertEqual(len(products), 1)
        doc = products[0]
        self.assertEqual(doc["ID"], "9")
        self.assertEqual(len(doc["tables"]), 2)
        for entry in doc["tables"]:
            self.assertIsNone(entry["header_rows"])
            self.assertEqual(entry["columns"], [])
            self.assertTrue(entry["error"])

    def test_transport_failure_does_not_lose_the_other_table(self):
        # 裁决：异常面是 Exception —— 网关超时（TimeoutError）不是解析异常，
        # 但也必须只毁掉那一张表，同记录里其余各表照常出结果
        client = FakeClient(
            [TimeoutError("网关超时"), TimeoutError("网关又超时"), GOOD_B]
        )
        record = input_record("7", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="m", max_attempts=2)

        first, second = products[0]["tables"]
        self.assertEqual(first["range"], "A2:C5")
        self.assertEqual(first["columns"], [])
        self.assertIsNotNone(first["error"])
        self.assertIn("超时", first["error"])
        self.assertEqual(second["columns"], ["地区", "金额"])
        self.assertNotIn("error", second)
        # 超时走的是 client.py 的有界重试（两次 = max_attempts），不是一次就放弃；
        # 加上第二张表那一次，一共 3 次调用
        self.assertEqual(len(client.calls), 3)

    def test_failed_table_carries_its_error_text(self):
        client = FakeClient([BAD_JSON, BAD_JSON, GOOD_B])
        record = input_record("7", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="m", max_attempts=2)

        error = products[0]["tables"][0]["error"]
        self.assertIsInstance(error, str)
        self.assertTrue(error.strip())  # 空错误文本等于没线索
        # 具体原因要在里面（Task 4 的那句话），人才能判断该改什么
        self.assertIn("JSON", error)
        # 失败表的身份一律来自结构化的表范围，不依赖异常消息里有没有表名
        self.assertEqual(products[0]["tables"][0]["range"], "A2:C5")

    def test_textless_exception_still_yields_an_error_text(self):
        # `str(exc)` 可能是空串（异常自带空文本、`__str__` 不读 args）：失败表的 error
        # 仍必须有字，退到**异常类型名**。空 error 等于 §9 的「带 error 文本」什么也没给。
        client = FakeClient([TextlessGatewayError()])
        record = input_record("7", [TABLE_A])

        products = run_pipeline([record], client, model="m", max_attempts=1)

        first = products[0]["tables"][0]
        self.assertTrue(first["error"].strip())  # 空错误文本等于没线索
        self.assertEqual(first["error"], "TextlessGatewayError")  # 认得出是哪类异常
        self.assertEqual(first["range"], "A2:C5")  # 失败表的身份仍来自结构化表范围

    def test_failed_table_differs_from_a_table_without_a_header_area(self):
        # 两种「columns 是空数组」的形状完全不同：没有标题行区是**成功**（有 reason、没有 error，
        # 见 Task 4 裁定：那时列数检查不适用）；失败是 columns 缺失标记 + error。
        # 下游只看 columns 区分不出来，所以两者的其余标记都必须准。
        no_header = (
            '{"header_rows": null, "columns": [], '
            '"reason": "第一行就是数据，本表没有标题行区"}'
        )
        client = FakeClient([BAD_JSON, BAD_JSON, no_header])
        record = input_record("7", [TABLE_A, TABLE_B])

        products = run_pipeline([record], client, model="m", max_attempts=2)

        failed, no_header_rows = products[0]["tables"]
        self.assertEqual(failed["columns"], [])
        self.assertIsNotNone(failed["error"])
        self.assertEqual(no_header_rows["columns"], [])  # 字段必须在，且是空数组
        self.assertIsNone(no_header_rows["header_rows"])
        self.assertNotIn("error", no_header_rows)  # 没判出标题行区不是失败
        self.assertTrue(no_header_rows["reason"].strip())

    def test_keyboard_interrupt_is_not_swallowed(self):
        # 异常面是整个 Exception，但**不是** BaseException：Ctrl-C 必须还能打断整批
        client = FakeClient([KeyboardInterrupt()])
        record = input_record("7", [TABLE_A])

        with self.assertRaises(KeyboardInterrupt):
            run_pipeline([record], client, model="m", max_attempts=1)

    def test_every_record_gets_a_product_in_order(self):
        # 串行、按序：两张表各问一次，产物顺序与输入记录顺序一致（规格 §9 最后一段）
        client = FakeClient([GOOD_A, GOOD_B, GOOD_A, GOOD_B])
        records = [
            input_record("1", [TABLE_A, TABLE_B]),
            input_record("2", [TABLE_A, TABLE_B], file_name="two.xlsx", sheet="S2"),
        ]

        products = run_pipeline(records, client, model="m", max_attempts=1)

        self.assertEqual([doc["ID"] for doc in products], ["1", "2"])
        self.assertEqual(products[1]["FileName"], "two.xlsx")
        self.assertEqual(products[1]["sheet"], "S2")

    def test_products_match_build_record(self):
        # 产物形状由 Task 1 的输出契约固定，编排层不另起一套：把同一次运行的结果交给
        # `build_record` 重新汇总，必须与 `run_pipeline` 的产物逐字段一致。
        client = FakeClient([GOOD_A])
        record = input_record("7", [TABLE_A])

        products = run_pipeline([record], client, model="m", max_attempts=1)

        entry = products[0]["tables"][0]
        rebuilt = build_record(
            "7",
            "book.xlsx",
            "Sheet1",
            [
                TableResult(
                    entry["range"],
                    tuple(entry["columns"]),
                    tuple(entry["header_rows"]),
                    entry["reason"],
                )
            ],
            model="m",
            prompt_version=PROMPT_VERSION,
            attempts=1,
        )
        self.assertEqual(products[0], rebuilt)


class ParseLinesTest(unittest.TestCase):
    """`parse_lines(lines)`：逐记录隔离的解析入口（规格 §9「单记录失败不中断整批」）。"""

    GOOD_LINE = raw_record("1", [TABLE_A])
    ANOTHER_LINE = raw_record("3", [TABLE_A, TABLE_B])
    BROKEN_LINE = "这不是 JSON"

    def test_skips_the_bad_record_and_keeps_the_others_in_order(self):
        lines = number_lines([self.GOOD_LINE, self.BROKEN_LINE, self.ANOTHER_LINE])

        records, failures = parse_lines(lines)

        self.assertEqual([r.record_id for r in records], ["1", "3"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], self.BROKEN_LINE)

    def test_bad_record_reports_its_index_and_line_number(self):
        lines = number_lines([self.GOOD_LINE, self.BROKEN_LINE])

        _, failures = parse_lines(lines)

        raw, index, line_no, message = failures[0]
        # 记录序号按非空行计数、1-based；行号是 jsonl 里的行号
        self.assertEqual((index, line_no), (2, 2))
        self.assertEqual(raw, self.BROKEN_LINE)
        self.assertIn("第 2 条记录", message)
        self.assertIn("第 2 行", message)
        self.assertIn("不是合法 JSON", message)

    def test_blank_lines_do_not_advance_the_record_index(self):
        # 计数约定必须与 load_records 一致：空行不计入记录序号（只对非空行推进）
        lines = number_lines(
            [self.GOOD_LINE, "   ", "", self.BROKEN_LINE, self.ANOTHER_LINE]
        )

        records, failures = parse_lines(lines)

        self.assertEqual([r.record_id for r in records], ["1", "3"])
        _, index, line_no, _ = failures[0]
        self.assertEqual(index, 2)  # 空行不算一条记录
        self.assertEqual(line_no, 4)  # 但行号照实给
        # 坏记录的原文与位置必须一起带出来，调用方才能把它们写进产物
        self.assertEqual(failures[0][0], self.BROKEN_LINE)
        self.assertEqual(failures[0][1:3], (2, 4))

    def test_empty_input_yields_nothing(self):
        self.assertEqual(parse_lines([]), ([], []))
        self.assertEqual(parse_lines(number_lines(["", "   "])), ([], []))


class ParseLinesToRecordTest(unittest.TestCase):
    """`parse_lines` 与 `parse_record` 是同一套解析路径，产物因此可以逐字段对齐。"""

    def test_parsed_records_match_parse_record(self):
        line = raw_record("1", [TABLE_A, TABLE_B])

        records, failures = parse_lines(number_lines([line]))

        self.assertEqual(failures, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0], parse_record(line, 1, 1))


class BlankLineCountingAgreementTest(unittest.TestCase):
    """空行的计数约定：`input.load_records`（读文件）与 `pipeline.number_lines`（给行编号）
    必须是**同一套**。

    这条约定有两处实现，所以本用例把**同一份字节**同时喂给两边：把第 k 条非空行弄坏，
    `load_records` 抛出的真实消息里的「第 N 条记录 / jsonl 第 M 行」必须与 `parse_lines`
    在按同一份字节编号后的真实结果逐位相同。比较的是两个真实实现，不是把约定在测试里再抄一遍。
    """

    #: 6 个物理行：非空行 1、2、3 分别落在 jsonl 第 1、3、5 行（第 2、4、6 行是空白行）
    RAW_LINES = (
        raw_record("1", [TABLE_A]),
        "   ",
        raw_record("2", [TABLE_A]),
        "",
        raw_record("3", [TABLE_A]),
        "\t",
    )
    #: 每条非空行 ⇒ (它作为第几条记录（1-based、按非空行计数）, 它的 jsonl 行号)
    NON_BLANK = ((1, 1), (2, 3), (3, 5))

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _write(self, raws):
        """把行写进临时文件（stdlib `tempfile`，仓库里不留东西）。"""
        handle, path = tempfile.mkstemp(prefix="counting.", suffix=".jsonl")
        os.close(handle)
        self.addCleanup(self._remove, path)
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            for raw in raws:
                stream.write(raw + "\n")
        return path

    def test_same_bytes_give_the_same_record_index_and_line_number(self):
        for record_index, line_no in self.NON_BLANK:
            with self.subTest(record_index=record_index, line_no=line_no):
                raws = list(self.RAW_LINES)
                raws[line_no - 1] = "坏记录"  # 只把这一条非空行弄坏
                path = self._write(raws)

                with self.assertRaises(InputFormatError) as ctx:
                    load_records(path)
                match = re.search(
                    "第 (\\d+) 条记录（jsonl 第 (\\d+) 行）", str(ctx.exception)
                )
                self.assertIsNotNone(match, str(ctx.exception))
                self.assertEqual(
                    (int(match.group(1)), int(match.group(2))), (record_index, line_no)
                )

                # 同一份字节（真的从文件里读回来）经 number_lines + parse_lines
                with open(path, "r", encoding="utf-8") as stream:
                    file_lines = list(stream)
                records, failures = parse_lines(number_lines(file_lines))

                self.assertEqual(failures[0][1:3], (record_index, line_no))
                # 坏的那条只丢它自己：另外两条非空行两边都有记录，空白行两边都不算记录
                self.assertEqual(len(records), 2)
                self.assertEqual(
                    [record.record_id for record in records],
                    [rid for rid in ("1", "2", "3") if rid != str(record_index)],
                )


class RunBatchTest(unittest.TestCase):
    """`run_batch(lines, client, ...)`：逐记录隔离 + 逐表隔离，两条合起来才是规格 §9。"""

    def test_whole_batch_continues_after_a_record_fails(self):
        # 3 条记录，第 2 条的输入不可解析 ⇒ 第 1、3 条仍有结果
        client = FakeClient([GOOD_A, GOOD_A, GOOD_B])
        lines = number_lines(
            [
                raw_record("1", [TABLE_A]),
                "这条记录不是合法 JSON —— 它就是那条坏记录",
                raw_record("3", [TABLE_A, TABLE_B]),
            ]
        )

        products, failures = run_batch(lines, client, model="m", max_attempts=1)

        # 三条输入都留下产物：坏记录被跳过的痕迹写在产物里，不是静默少一行
        self.assertEqual([doc["ID"] for doc in products], ["1", "", "3"])
        # **不伪造身份**：读不出来的身份字段写空串（「这一行压根没成为记录」），
        # 而不是一个看起来像 ID 的占位符
        self.assertEqual(products[1]["FileName"], "")
        self.assertEqual(products[1]["sheet"], "")
        self.assertEqual(products[1]["tables"], [])
        # 位置改用**结构化字段**给（规格 §11.1：诊断字段直接放在记录里），
        # 人读的 error 文本里同一事实只出现一次
        self.assertEqual(products[1]["record_index"], 2)
        self.assertEqual(products[1]["source_line"], 2)
        self.assertIn("第 2 条记录", products[1]["error"])
        self.assertIn("第 2 行", products[1]["error"])
        # 第 1、3 条照常跑完
        self.assertEqual(products[0]["tables"][0]["columns"], ["产品", "数量", "单价"])
        self.assertEqual(
            [t["range"] for t in products[2]["tables"]], ["A2:C5", "E2:F5"]
        )
        # 诊断字段与正常记录同源
        self.assertEqual(products[1]["prompt_version"], PROMPT_VERSION)
        self.assertEqual(products[1]["attempts"], 1)
        self.assertEqual(len(failures), 1)

    def test_bad_record_does_not_consume_a_model_call(self):
        # 坏记录压根没有表可问，不该白烧调用次数（也不该让后续各条的响应错位）
        client = FakeClient([GOOD_A])
        lines = number_lines(["坏记录", raw_record("2", [TABLE_A])])

        products, _ = run_batch(lines, client, model="m", max_attempts=1)

        self.assertEqual(products[1]["tables"][0]["columns"], ["产品", "数量", "单价"])
        self.assertEqual(len(client.calls), 1)

    def test_skipped_record_keeps_its_place_in_the_product(self):
        # 坏记录必须在**原位置**上留下带诊断的产物，而不是挪到末尾或干脆消失
        client = FakeClient([GOOD_A, GOOD_B])
        lines = number_lines(
            [
                raw_record("1", [TABLE_A]),
                "坏记录",
                raw_record("3", [TABLE_B]),
            ]
        )

        products, _ = run_batch(lines, client, model="m", max_attempts=1)

        self.assertEqual(
            [doc["ID"] for doc in products], ["1", "", "3"]
        )
        self.assertEqual(products[2]["tables"][0]["range"], "E2:F5")

    def test_input_order_survives_wild_record_indexes(self):
        # 记录序号是**调用方提供的**（`parse_record` 只拿它写消息），可能重复或逆序；
        # 产物顺序却必须仍由**扫描顺序**决定。曾经按 record_index 排序，于是产物变成
        # 「所有成功的在前、坏记录挤在后」，与输入顺序不符（审查 Minor 2）。
        lines = [
            InputLine(raw_record("1", [TABLE_A]), 5, 1),
            InputLine("坏记录", 5, 2),
            InputLine(raw_record("3", [TABLE_A]), 2, 3),
        ]
        client = FakeClient([GOOD_A, GOOD_A])

        products, _ = run_batch(lines, client, model="m", max_attempts=1)

        self.assertEqual([doc["ID"] for doc in products], ["1", "", "3"])  # 输入顺序
        # 这两个定位键只属于**记录级失败**（见 test_healthy_record_does_not_grow_locator_keys），
        # 健康记录上没有；坏记录上照实写着调用方给的序号与行号
        self.assertEqual([doc.get("record_index") for doc in products], [None, 5, None])
        self.assertEqual([doc.get("source_line") for doc in products], [None, 2, None])

    def test_skipped_record_error_says_which_line_and_shows_the_text(self):
        raw = "坏记录，原文要能回看"
        products, _ = run_batch(
            number_lines([raw]), FakeClient([]), model="m", max_attempts=2
        )

        error = products[0]["error"]
        self.assertIn("第 1 条记录", error)
        self.assertIn("第 1 行", error)
        self.assertIn(raw, error)  # 原文回显：人才能回到那一行核对
        self.assertEqual(products[0]["attempts"], 2)
        # 哨兵只作为**人读标记**留在 error 里：身份字段是空的（见
        # test_whole_batch_continues_after_a_record_fails），机器读的 ID 不会是它
        self.assertIn("BROKEN_LINE", error)

    def test_healthy_record_does_not_grow_locator_keys(self):
        # 结构化定位（source_line / record_index）只属于**记录级失败**：
        # 健康记录的形状与 Task 1 的产物契约逐字段一致，一个新键都不长
        products, _ = run_batch(
            number_lines([raw_record("1", [TABLE_A])]),
            FakeClient([GOOD_A]),
            model="m",
            max_attempts=1,
        )

        self.assertEqual(
            set(products[0]),
            {"ID", "FileName", "sheet", "tables", "model", "prompt_version", "attempts"},
        )

    def test_skipped_record_error_repeats_no_location_fact(self):
        # parse_record 的消息本来就以「第 N 条记录（jsonl 第 M 行）」开头，产物不该再抄一遍：
        # 同一事实写两次，读的人会以为有两处线索（审查 Minor 4）
        products, _ = run_batch(
            number_lines(["坏记录"]), FakeClient([]), model="m", max_attempts=1
        )

        error = products[0]["error"]
        self.assertEqual(error.count("第 1 条记录"), 1)
        self.assertEqual(error.count("jsonl 第 1 行"), 1)
        self.assertIn("不是合法 JSON", error)  # 具体原因不能连去重一起去掉

    def test_long_broken_line_is_clipped_in_the_product(self):
        # 一条坏记录可能有整行几万个字符，产物不该被它撑爆
        products, _ = run_batch(
            number_lines(["坏" * 5000]), FakeClient([]), model="m", max_attempts=1
        )

        self.assertLess(len(products[0]["error"]), 500)
        self.assertIn("后略", products[0]["error"])

    def test_run_batch_is_parse_lines_then_run_pipeline(self):
        # run_batch 只是「parse_lines + run_pipeline」的合并写法，产物必须逐字段一致
        line = raw_record("1", [TABLE_A])

        products, failures = run_batch(
            number_lines([line]), FakeClient([GOOD_A]), model="m", max_attempts=1
        )
        expected = run_pipeline(
            parse_lines(number_lines([line]))[0],
            FakeClient([GOOD_A]),
            model="m",
            max_attempts=1,
        )

        self.assertEqual(products, expected)
        self.assertEqual(failures, [])


class RenderSkippedRecordTest(unittest.TestCase):
    """md 是给人看的：跳过的记录必须在 md 里看得见，否则只是静默少一条。"""

    def test_markdown_shows_the_skipped_record_and_its_error(self):
        products, _ = run_batch(
            number_lines(["坏记录"]), FakeClient([]), model="m", max_attempts=1
        )

        markdown = render_markdown(products, {"模型": "m"})

        self.assertIn("BROKEN_LINE", markdown)
        self.assertIn("整条记录跳过", markdown)
        self.assertIn("第 1 条记录", markdown)


if __name__ == "__main__":
    unittest.main()
