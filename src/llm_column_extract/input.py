# -*- coding: utf-8 -*-
"""输入读取（规格 §5）：把伙伴的 jsonl 读成带类型的记录。

伙伴的字段名**未冻结**（规格 §5.1），所以这里按「字段不定的袋子」处理：**只按形状识别**
（范围样式的字符串、该表对象下按列组织的单元格数组）。认不出来就抛 `InputFormatError`，
消息里带上**第几条记录**与**看到的键** —— **不猜、不静默跳过**。

术语用权威表述（`excel-to-md/GLOSSARY.md`）：**表范围**、**标题行区**、**数据区**。

解析只有一条路径：`parse_record` 解析**一行**；`load_records` 只是「读文件 + 逐行调它」的
严格循环（第一条坏记录就抛）。要逐记录隔离的调用方自己循环 `parse_record`。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 表范围的 A1 记法（允许 $ 绝对引用）。它是**声明高度**的唯一权威来源 ——
# 不引第二个来源（伙伴可能带的 RowCount 类字段一律不看）。
_RANGE_RE = re.compile(r"^\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+$")
_RANGE_PARTS_RE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)$")

_ID_KEYS = ("ID", "id")
_FILE_KEYS = ("FileName", "filename", "file")
_SHEET_KEYS = ("sheet", "Sheet")


class InputFormatError(Exception):
    """输入 jsonl 的形状认不出来。

    消息里必须带**第几条记录**与**看到的键**，否则人无法回到那一行核对。
    """


def location_text(record_index: int, line_no: int) -> str:
    """「第 N 条记录（jsonl 第 M 行）」——一条记录的定位措辞的**唯一来源**。

    错误消息与产物里的诊断都引它，免得同一个格式在两处各写一遍、然后慢慢漂开。
    """
    return "第 {} 条记录（jsonl 第 {} 行）".format(record_index, line_no)


def strip_location(message: str, record_index: int, line_no: int) -> str:
    """去掉消息开头那段**已经由调用方复述过**的定位前缀。

    定位前缀的措辞只有一处来源（上面的 `location_text`），所以这里比对的是**同一个字符串**，
    不是把格式又抄了一遍；万一消息不是那个形状（别处抛的异常），原样返回，**不硬切**。
    产物（`pipeline`）与 stderr 诊断（`cli`）都要「自己先说一遍位置、再引原因」，
    不共用这一处就会把「第 2 条记录（jsonl 第 2 行）」在**同一条消息里写两遍**。
    """
    prefix = location_text(record_index, line_no) + "："
    return message[len(prefix):] if message.startswith(prefix) else message


@dataclass(frozen=True)
class InputTable:
    """一张表的输入：**表范围** + **每列的单元格内容**（按行序，自上而下）。"""

    range: str
    columns: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class InputRecord:
    """一条记录 = 一个「文件 + 工作表」。"""

    record_id: str
    file_name: str
    sheet: str
    tables: tuple[InputTable, ...]


class _Loc:
    """一条记录的定位信息：出错时消息里要同时给出记录序号、行号与看到的键。"""

    __slots__ = ("record_index", "line_no", "record")

    def __init__(self, record_index: int, line_no: int, record: dict):
        self.record_index = record_index
        self.line_no = line_no
        self.record = record

    def fail(self, detail: str) -> InputFormatError:
        return InputFormatError(
            "{}：{}。看到的键：{}".format(
                location_text(self.record_index, self.line_no),
                detail,
                sorted(self.record),
            )
        )


def _as_text(value):
    """身份字段的值：标量转文本；认不出来返回 None，由调用方报错。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    return None


def _identity(loc: _Loc, keys, role: str) -> str:
    """按别名收集身份字段；缺失、不可用或互相矛盾都报错（不替它挑一个）。"""
    found = []
    for key in keys:
        if key not in loc.record:
            continue
        text = _as_text(loc.record[key])
        if text is None:
            raise loc.fail(
                "字段 {!r} 认不出来（{} 必须是字符串）".format(key, role)
            )
        found.append((key, text))

    if not found:
        raise loc.fail(
            "缺少可识别的 {} 字段（别名：{}）".format(role, " / ".join(keys))
        )
    if len({text for _, text in found}) > 1:
        raise loc.fail(
            "{} 字段有多个互相矛盾的值：{}".format(
                role, "，".join("{!r}={!r}".format(k, v) for k, v in found)
            )
        )
    return found[0][1]


def _range_in(table: dict, loc: _Loc):
    """表对象里那个范围样式的字符串；没有则 None。多个不同值 ⇒ 报错。"""
    found = [
        (key, value)
        for key, value in table.items()
        if isinstance(value, str) and _RANGE_RE.match(value)
    ]
    if not found:
        return None
    if len({value for _, value in found}) > 1:
        raise loc.fail(
            "一个表对象里有多个互相矛盾的表范围：{}".format(
                "，".join("{!r}={!r}".format(k, v) for k, v in found)
            )
        )
    return found[0][1]


def _declared_height(range_str: str):
    """表范围声明的高度（`A2:C4` ⇒ 3 行）；不成形或反序 ⇒ None。"""
    match = _RANGE_PARTS_RE.match(range_str)
    if match is None:
        return None
    first, last = int(match.group(2)), int(match.group(4))
    if last < first:
        return None
    return last - first + 1


def _cell_text(value):
    """`{"value": ...}` 里的值：字符串原样、null 当空单元格、数字转文本；其余返回 None。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    return None


def _column_cells(loc: _Loc, range_str: str, key: str, values: list):
    """一列的单元格：元素只认字符串、null、或 `{"value": ...}`；其余报错。"""
    cells = []
    for index, value in enumerate(values, start=1):
        if isinstance(value, dict):
            text = _cell_text(value["value"]) if "value" in value else None
        elif value is None or isinstance(value, str):
            text = _cell_text(value)
        else:
            # 裸数字之类不在识别规则里：报错，不能静默当空值
            text = None
        if text is None:
            raise loc.fail(
                '表 {} 的列 {!r} 第 {} 个元素认不出来（元素须是字符串或 {{"value": ...}}）'.format(
                    range_str, key, index
                )
            )
        cells.append(text)
    return tuple(cells)


def _build_table(loc: _Loc, range_str: str, table: dict) -> InputTable:
    height = _declared_height(range_str)
    if height is None:
        raise loc.fail("表范围 {!r} 认不出来".format(range_str))

    columns = []
    for key, value in table.items():
        if not isinstance(value, list):
            continue  # 非数组字段（表范围、计数、元数据）不是列
        columns.append((key, _column_cells(loc, range_str, key, value)))
    if not columns:
        raise loc.fail(
            "表 {} 下认不出任何列（列是该表对象下的数组字段）".format(range_str)
        )

    for key, cells in columns:
        if len(cells) != height:
            raise loc.fail(
                "表 {} 声明高度 {} 行，但列 {!r} 有 {} 个单元格"
                "（每一列都必须与声明高度一致）".format(
                    range_str, height, key, len(cells)
                )
            )

    return InputTable(range_str, tuple(cells for _, cells in columns))


def _discover_tables(loc: _Loc) -> tuple:
    """按形状找出记录里的表：范围样式的键、含范围样式字符串的对象、表对象数组。"""
    tables = []
    for key, value in loc.record.items():
        if _RANGE_RE.match(key):
            if not isinstance(value, dict):
                raise loc.fail(
                    "键 {!r} 是表范围，但它的值不是表对象，认不出列".format(key)
                )
            inner = _range_in(value, loc)
            if inner is not None and inner != key:
                raise loc.fail(
                    "键 {!r} 与它自己的表范围 {!r} 矛盾".format(key, inner)
                )
            tables.append(_build_table(loc, key, value))
            continue

        if isinstance(value, dict):
            range_str = _range_in(value, loc)
            if range_str is not None:
                tables.append(_build_table(loc, range_str, value))
            continue

        if isinstance(value, list):
            objects = [item for item in value if isinstance(item, dict)]
            if not objects:
                continue  # 不是表对象数组（`[]`、`["warn"]` 之类），不是表
            if len(objects) != len(value):
                raise loc.fail(
                    "字段 {!r} 的数组里混了非对象元素，认不出来".format(key)
                )
            for item in value:
                range_str = _range_in(item, loc)
                if range_str is None:
                    raise loc.fail(
                        "字段 {!r} 的数组里有元素认不出表范围".format(key)
                    )
                tables.append(_build_table(loc, range_str, item))

    if not tables:
        raise loc.fail("认不出任何表（表范围形如 A2:C4）")
    return tuple(tables)


def _build_record(loc: _Loc) -> InputRecord:
    record_id = _identity(loc, _ID_KEYS, "ID")
    file_name = _identity(loc, _FILE_KEYS, "FileName")
    sheet = _identity(loc, _SHEET_KEYS, "sheet")
    return InputRecord(record_id, file_name, sheet, _discover_tables(loc))


def parse_record(raw: str, record_index: int, line_no: int) -> InputRecord:
    """解析**一行**已经读进来的 jsonl 文本，返回 `InputRecord`。

    `raw` 是文件里原样的行文本（本函数自己 `json.loads`）；`record_index` 是 1-based 的
    记录序号、`line_no` 是 jsonl 行号 —— 两者只用于出错消息，由调用方提供。

    形状认不出来 ⇒ 抛 `InputFormatError`（消息里同时给出记录序号、行号、看到的键）。
    逐记录隔离（规格 §9「单记录失败 ⇒ 不中断整批」）就建立在这个入口上：调用方自己
    一行一行调它，捕 `InputFormatError` 后决定这条记录怎么降级。
    """
    try:
        # strip 掉行尾换行，免得解析器的报错把行号算成下一行
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise InputFormatError(
            "{}：不是合法 JSON：{}".format(location_text(record_index, line_no), exc)
        ) from exc
    if not isinstance(data, dict):
        raise InputFormatError(
            "{}：记录是 {}，不是 JSON 对象，没有键可列".format(
                location_text(record_index, line_no), type(data).__name__
            )
        )
    return _build_record(_Loc(record_index, line_no, data))


def load_records(path: str) -> list[InputRecord]:
    """读 jsonl（一行一条记录），返回 `InputRecord` 列表。

    空行跳过；**记录序号是 1-based、按非空行计数**，消息里同时给出 jsonl 行号，
    两者在没有空行时相同。

    形状认不出来 ⇒ 抛 `InputFormatError`（不猜、不静默跳过）。文件本身读不了（不存在等）
    仍抛 `OSError`：那是 I/O 问题，不是格式问题。

    **本函数是严格的**：遇到第一条坏记录就抛，不返回部分结果 —— 这是**文件级**的失败。
    需要「坏记录不打断整批」的调用方**自己**逐行调 `parse_record`（`pipeline.number_lines`
    给原始行编号，与这里的计数口径相同并由测试钉住），见它的文档。
    """
    records = []
    record_index = 0
    # utf-8-sig：伙伴的文件头可能带 BOM，它不该让第 1 条记录失败
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            record_index += 1
            records.append(parse_record(raw, record_index, line_no))
    return records
