# -*- coding: utf-8 -*-
"""输出契约（规格 §11）。jsonl 是给机器的产物，形状由这里固定。

概念一律用权威术语：**表范围**、**标题行区**、**数据区**（见 `excel-to-md/GLOSSARY.md`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class TableResult:
    """一张表的判定结果。

    失败与否只看 `error is not None` —— 不设第二个会与它漂移的状态位。
    `columns` 的元素是**路径**（各层用 `" / "` 拼接），且**必须是字符串**（规格 §11.2）。
    没有标题行区的表：`columns=()`、`header_rows=None`，字段照样写出。
    """

    range: str
    columns: tuple  # tuple[str, ...]
    header_rows: tuple  # tuple[int, int] | None
    reason: str
    error: str | None = None


def build_record(
    record_id: str,
    file_name: str,
    sheet: str,
    tables: Sequence[TableResult],
    *,
    model: str,
    prompt_version: str,
    attempts: int,
    error: str | None = None,
) -> dict:
    """把一条记录汇总成 jsonl 的一行（dict）。

    逐表不拍平；`columns`/`header_rows` 转成 list 以便 JSON 序列化；
    `error` 为 `None` 时不写该键（成功就不带噪声）。

    `error` 是**记录级**失败（输入压根解析不了 ⇒ 没有表可谈），与逐表的 `TableResult.error`
    各管一层：逐表的归 `tables[i]["error"]`，整条记录读不出身份的归这里。两处都不设
    「第二个状态位」—— 失败与否只看 `error is not None`。
    """
    out_tables = []
    for table in tables:
        entry = {
            "range": table.range,
            "columns": list(table.columns),
            "header_rows": None if table.header_rows is None else list(table.header_rows),
            "reason": table.reason,
        }
        if table.error is not None:
            entry["error"] = table.error
        out_tables.append(entry)

    out = {
        "ID": record_id,
        "FileName": file_name,
        "sheet": sheet,
        "tables": out_tables,
        "model": model,
        "prompt_version": prompt_version,
        "attempts": attempts,
    }
    if error is not None:
        out["error"] = error
    return out
