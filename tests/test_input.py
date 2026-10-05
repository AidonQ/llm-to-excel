# -*- coding: utf-8 -*-
"""输入读取（规格 §5）：伙伴的字段名未冻结，因此按「字段不定的袋子」处理。

只按**形状**识别（范围样式的字符串、按列组织的单元格数组）；认不出来必须抛
`InputFormatError`，**不猜、不静默跳过**。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from llm_column_extract.input import (
    InputFormatError,
    InputRecord,
    InputTable,
    load_records,
    parse_record,
)


def _table(range_str, **columns):
    """一个按列组织的表对象：`range` 加若干「列名 -> 该列单元格」的数组字段。"""
    table = {"range": range_str}
    table.update(columns)
    return table


def _good_record(record_id="r1"):
    """一条**合法**记录的原始 jsonl 行文本（直接喂给 `parse_record`）。"""
    return json.dumps({
        "ID": record_id, "FileName": "a.xlsx", "sheet": "S",
        "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])],
    }, ensure_ascii=False)


class _InputTestCase(unittest.TestCase):
    """夹具写在 stdlib `tempfile` 造的临时文件里 —— 源码树里不留任何东西。

    每个文件一个 `mkstemp`（不建目录）：即使测试跑挂，仓库里也不会多出未跟踪的目录，
    以后一次 `git add -A` 也没有东西可误提交。
    """

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _write(self, name, lines, encoding="utf-8"):
        """写一份 jsonl；`lines` 里是 dict（当记录）或 str（当原始行，可为空行）。"""
        handle, path = tempfile.mkstemp(prefix=name + ".", suffix=".tmp.jsonl")
        os.close(handle)
        self.addCleanup(self._remove, path)
        with open(path, "w", encoding=encoding, newline="\n") as stream:
            for line in lines:
                if isinstance(line, str):
                    stream.write(line + "\n")
                else:
                    stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def _message(self, path):
        """跑 `load_records` 并返回 `InputFormatError` 的消息。"""
        with self.assertRaises(InputFormatError) as ctx:
            load_records(path)
        return str(ctx.exception)


class LoadRecordsTest(_InputTestCase):
    # --- 主路径 ---

    def test_reads_columns_as_per_column_cell_lists(self):
        path = self._write("happy.jsonl", [
            {
                "ID": "r1",
                "FileName": "clean.xlsx",
                "sheet": "Clean",
                "tables": [
                    _table("A2:C4",
                           col_0=["2024 年度", "产品", "北区"],
                           # {"value": 数字} 的载荷要转成文本（伙伴的单元格常有数字）
                           col_1=[{"value": "2024 年度"}, {"value": "Q1"}, {"value": 10}]),
                    _table("E2:F4",
                           col_0=["北区", "A", "1"],
                           col_1=["南区", "B", "2"]),
                ],
            },
            {
                "id": "r2",
                "filename": "other.xlsx",
                "Sheet": "Other",
                "Tables": [_table("B2:B4", col_0=["x", "y", "z"])],
            },
        ])

        records = load_records(path)

        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertIsInstance(first, InputRecord)
        self.assertEqual(
            (first.record_id, first.file_name, first.sheet),
            ("r1", "clean.xlsx", "Clean"),
        )
        self.assertEqual([table.range for table in first.tables], ["A2:C4", "E2:F4"])
        self.assertIsInstance(first.tables[0], InputTable)
        self.assertEqual(first.tables[0].columns, (
            ("2024 年度", "产品", "北区"),
            ("2024 年度", "Q1", "10"),
        ))
        # 别名同样要认：小写 id / filename / Sheet / Tables
        self.assertEqual(
            (records[1].record_id, records[1].file_name, records[1].sheet),
            ("r2", "other.xlsx", "Other"),
        )
        self.assertEqual(records[1].tables[0].columns, (("x", "y", "z"),))
        # 单元格必须是字符串（下游提示词按字符串渲染）
        for cell in records[1].tables[0].columns[0]:
            self.assertIsInstance(cell, str)

    # --- 必填字段认不出来：消息里要有记录序号与看到的键 ---

    def test_missing_sheet_field_raises_with_record_index(self):
        path = self._write("missing_sheet.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
            {"ID": "r2", "FileName": "b.xlsx", "meta_extra": True,
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
        ])

        message = self._message(path)

        self.assertIn("第 2 条记录", message)   # 1-based，可直接对上 jsonl 行号
        self.assertIn("sheet", message)
        self.assertIn("meta_extra", message)    # 必须列出看到的键

    def test_missing_file_raises(self):
        path = self._write("missing_file.jsonl", [
            {"ID": "r1", "sheet": "S", "name_extra": "x",
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)
        self.assertIn("FileName", message)
        self.assertIn("name_extra", message)

    def test_missing_id_raises(self):
        path = self._write("missing_id.jsonl", [
            {"FileName": "a.xlsx", "sheet": "S", "id_like_extra": 7,
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)
        # 断言要真的钉住「缺 ID」这件事：`assertIn("id", ...)` 会被 id_like_extra 满足
        self.assertIn("别名：ID / id", message)
        self.assertIn("id_like_extra", message)

    # --- 形状认不出来：不许静默跳过 ---

    def test_unrecognized_shape_raises_instead_of_silently_skipping(self):
        # 行优先的二维数组：不是「每列单元格」的形状 ⇒ 必须报错，不能当成没有表
        path = self._write("row_major.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [{"range": "A2:C4",
                         "rows": [["产品", "数量", "单价"],
                                  ["A", "1", "0.25"],
                                  ["B", "2", "0.5"]]}]},
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)
        self.assertIn("rows", message)   # 认不出的字段名要点出来

    def test_record_without_any_recognizable_table_raises(self):
        path = self._write("no_table.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S", "notes": "纯文本，没有表"},
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)
        self.assertIn("认不出", message)
        self.assertIn("notes", message)   # 看到的键

    def test_table_without_any_column_array_raises(self):
        path = self._write("no_columns.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [{"range": "A2:C4", "n_rows": 3}]},
        ])

        message = self._message(path)

        self.assertIn("A2:C4", message)
        self.assertIn("认不出", message)

    def test_bare_number_cell_element_raises(self):
        # 认列元素只认「字符串 / {"value": ...}」；裸数字要点出字段名，不静默当空值
        path = self._write("bare_number.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4", col_0=["a", 1, "c"], col_1=["d", "e", "f"])]},
        ])

        message = self._message(path)

        self.assertIn("col_0", message)
        self.assertIn("第 1 条记录", message)

    # --- Review Focus #4：某列与表范围声明的高度不齐 ---

    def test_column_shorter_than_declared_range_height_raises(self):
        path = self._write("short_column.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4",
                               col_0=["a", "b", "c"],
                               col_1=["d", "e"],          # 声明 3 行，这一列只有 2 个
                               col_2=["g", "h", "i"])]},
        ])

        message = self._message(path)

        self.assertIn("A2:C4", message)
        self.assertIn("col_1", message)   # 点名是哪一列（不是只看第一列）
        self.assertIn("3", message)       # 声明高度
        # 断言要真的钉住「这一列只有 2 个」：`assertIn("2", ...)` 会被范围 A2:C4 满足
        self.assertIn("有 2 个单元格", message)

    def test_column_longer_than_declared_range_height_raises(self):
        path = self._write("long_column.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4",
                               col_0=["a", "b", "c"],
                               col_1=["d", "e", "f", "g"],
                               col_2=["h", "i", "j"])]},
        ])

        message = self._message(path)

        self.assertIn("col_1", message)
        # 长的方向同理：`assertIn("4", ...)` 会被范围 A2:C4 满足，钉住实际单元格数
        self.assertIn("有 4 个单元格", message)

    # --- 表范围的其它表达方式 ---

    def test_range_shaped_key_is_recognized_as_a_table(self):
        path = self._write("range_key.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "A2:B4": {"c0": ["a", "b", "c"], "c1": ["d", "e", "f"]}},
        ])

        records = load_records(path)

        self.assertEqual(len(records[0].tables), 1)
        self.assertEqual(records[0].tables[0].range, "A2:B4")
        self.assertEqual(records[0].tables[0].columns, (("a", "b", "c"), ("d", "e", "f")))

    def test_range_shaped_key_with_non_object_value_raises(self):
        # 键自称是表范围，值却不是表对象 ⇒ 报错，不能静默跳过这张表
        path = self._write("range_key_bad_value.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S", "A2:B4": ["a", "b", "c"]},
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)
        self.assertIn("A2:B4", message)

    # --- 兜底解析（规格 §5.1）：JSON 坏了要响亮报错，不许猜、不许跳过 ---

    def test_malformed_json_line_raises_with_record_index_and_line(self):
        path = self._write("broken_json.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
            '{"ID": "r2", ',          # 第 2 行是截断的 JSON
        ])

        message = self._message(path)

        self.assertIn("第 2 条记录", message)
        self.assertIn("jsonl 第 2 行", message)
        self.assertIn("不是合法 JSON", message)

    def test_non_object_record_line_raises_with_record_index_and_line(self):
        path = self._write("not_object.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
            "[1, 2, 3]",              # 第 2 行是合法 JSON，但不是记录（对象）
        ])

        message = self._message(path)

        self.assertIn("第 2 条记录", message)
        self.assertIn("jsonl 第 2 行", message)
        self.assertIn("不是 JSON 对象", message)
        self.assertIn("list", message)          # 点出实际类型，便于人核对

    # --- 文件头的 UTF-8 BOM 不能把第 1 条记录带崩 ---

    def test_utf8_bom_at_head_of_file_does_not_fail_record_one(self):
        path = self._write("bom.jsonl", [
            {"ID": "r1", "FileName": "a.xlsx", "sheet": "S",
             "tables": [_table("A2:C4", c0=["a", "b", "c"], c1=["d", "e", "f"])]},
        ], encoding="utf-8-sig")    # 伙伴的文件常带 BOM

        records = load_records(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, "r1")
        self.assertEqual(records[0].tables[0].columns, (("a", "b", "c"), ("d", "e", "f")))

    # --- 序号约定 ---

    def test_blank_lines_do_not_shift_record_indexing(self):
        path = self._write("blank_line.jsonl", [
            "",
            {"ID": "r1", "FileName": "a.xlsx"},   # 缺 sheet
        ])

        message = self._message(path)

        self.assertIn("第 1 条记录", message)   # 记录序号按非空行数，1-based
        self.assertIn("第 2 行", message)       # 同时给出 jsonl 行号，便于定位


class ParseRecordTest(_InputTestCase):
    """`parse_record`：**单行**入口。逐记录隔离（规格 §9）由调用方在它上面自己循环。"""

    def test_good_line_returns_the_expected_record(self):
        record = parse_record(_good_record(), 3, 7)

        self.assertIsInstance(record, InputRecord)
        self.assertEqual(
            (record.record_id, record.file_name, record.sheet),
            ("r1", "a.xlsx", "S"),
        )
        self.assertEqual([table.range for table in record.tables], ["A2:C4"])
        self.assertEqual(record.tables[0].columns, (("a", "b", "c"), ("d", "e", "f")))

    def test_bad_line_raises_with_its_own_record_index_and_line(self):
        raw = json.dumps({"ID": "r1", "FileName": "a.xlsx"}, ensure_ascii=False)  # 缺 sheet

        with self.assertRaises(InputFormatError) as ctx:
            parse_record(raw, 3, 7)

        message = str(ctx.exception)
        self.assertIn("第 3 条记录", message)      # 用传进来的参数，不是写死的 1
        self.assertIn("jsonl 第 7 行", message)
        self.assertIn("别名：sheet / Sheet", message)

    def test_a_bad_line_does_not_block_the_other_records(self):
        # 规格 §9「单记录失败 ⇒ 不中断整批」：调用方逐行 parse_record，坏的那条自己降级
        raw_lines = [_good_record(), '{"ID": "r2", ', _good_record("r3")]
        parsed, failed = [], []
        for index, raw in enumerate(raw_lines, start=1):
            try:
                parsed.append(parse_record(raw, index, index).record_id)
            except InputFormatError as exc:
                failed.append((index, str(exc)))

        self.assertEqual(parsed, ["r1", "r3"])     # 第 2 条坏，第 1、3 条照样有结果
        self.assertEqual(len(failed), 1)
        self.assertIn("第 2 条记录", failed[0][1])


if __name__ == "__main__":
    unittest.main()
