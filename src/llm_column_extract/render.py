# -*- coding: utf-8 -*-
"""md 产物渲染（规格 §11.3）。md 由**代码**从 jsonl 渲染，因此两份产物不可能互相矛盾。

纯字符串拼接，不引依赖。术语用权威表述：**表范围**、**标题行区**（起止行号）、**数据区**。
"""
from __future__ import annotations

from typing import Sequence

_NONE = "（无）"


def _render_table(index: int, table: dict) -> list:
    lines = ["### 表 {}：表范围 {}".format(index, table.get("range", _NONE)), ""]

    header_rows = table.get("header_rows")
    if header_rows is None:
        lines.append("- 标题行区：无")
    else:
        lines.append("- 标题行区：第 {}–{} 行".format(header_rows[0], header_rows[1]))

    columns = table.get("columns") or []
    if columns:
        lines.append("- 列（全路径）：")
        lines.extend("- `{}`".format(column) for column in columns)
    else:
        lines.append("- 列（全路径）：{}".format(_NONE))

    lines.append("- 判定依据：{}".format(table.get("reason") or _NONE))

    error = table.get("error")
    if error is not None:
        lines.append("- **失败：{}**".format(error))

    lines.append("")
    return lines


def render_markdown(records: Sequence[dict], meta: dict) -> str:
    """把 jsonl 记录渲染成人读的 md。`meta` 只用于头部溯源。"""
    lines = ["# 列名提取结果", ""]

    for key, value in meta.items():
        lines.append("- {}：{}".format(key, value))
    if meta:
        lines.append("")

    for record in records:
        file_name = record.get("FileName", _NONE)
        sheet = record.get("sheet", _NONE)
        lines.append("## {} / {}".format(file_name, sheet))
        lines.append("")

        lines.append("- ID：{}".format(record.get("ID", _NONE)))
        lines.append(
            "- 模型：{}（提示词 {}，调用次数 {}）".format(
                record.get("model", _NONE),
                record.get("prompt_version", _NONE),
                record.get("attempts", _NONE),
            )
        )
        lines.append("")

        error = record.get("error")
        if error is not None:
            # 记录级失败（输入解析不了 ⇒ 整条跳过）：人必须在这里就看到它，
            # 否则 md 里只会静默少一条，看不出是「没有结果」还是「坏了被丢掉」
            lines.append("- **整条记录跳过：{}**".format(error))
            lines.append("")

        tables = record.get("tables") or []
        if not tables:
            lines.append("（本记录没有表）")
            lines.append("")
            continue

        for index, table in enumerate(tables, start=1):
            lines.extend(_render_table(index, table))

    return "\n".join(lines)
