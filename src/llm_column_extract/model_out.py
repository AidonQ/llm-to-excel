# -*- coding: utf-8 -*-
"""模型输出的**兜底解析**与**结构校验**（规格 §6、§6.1、§8、§9）。

网关的结构化输出能力**不可依赖**（规格 §8），所以这里自己动手：剥 markdown 围栏、
容忍前后杂字、取第一个 `{` 到最后一个 `}`。**JSON 坏了就报错，不猜、不补全**（§8）。

规格 §6.1 的四条校验全在这里，任一条不合规即抛 `ModelOutputError`：

1. `header_rows` 是 `null`，或连续区间 `[起, 止]` 且 `1 ≤ 起 ≤ 止 ≤ 表行数`
2. `header_rows: null` 必须配 `columns: []`（自洽性，**两个方向都查**）
3. `columns` 是数组，每项是**非空字符串**
4. **列数 == 该表在输入里的列数**（唯一能与源对齐的硬检查，抓漏列/多列）

两处裁定（实施时明确下来的，见 Task 4 报告）：

- **`header_rows: null` 时列数检查不适用** —— 那时压根没有列路径要比对；自洽性（第 2 条）
  已经保证 `columns` 是 `[]`。否则「N 列的表判成没有标题行区」会被第 4 条无条件打回。
- **标题行区不能贴到底**：`[起, 止]` 里只要 `止 >= 表行数`（`止 > 表行数` 已被越界挡掉，
  所以实际命中的是 `止 == 表行数`），标题行区下面就没有**数据区**了（定义见 prompt 的
  标题行区：位于数据之上），答案非法；它的错误消息与普通越界分开写，重问时模型才知道该改哪一头。

`columns` 的元素必须是字符串（规格 §11.2）：对象会被下游 `str()` 悄悄字符串化成一坨、
列名全部错掉**且不报错** —— 所以这里**拒收，绝不转换**。本模块只查「非空且是字符串」，
**不解析路径**（各层 `" / "` 的拼接是模型的事）。

消息里必须带**哪个字段、看到的什么值、期望什么** —— Task 5 的重问循环把它原样回传给模型
（规格 §9「单表解析/校验失败 ⇒ 带具体错误重问一次」）。**绝不静默接受，也绝不因为校验失败
就交出一份看起来合格的列名。**

术语用权威表述：**表范围**、**标题行区**、**数据区**（见 `excel-to-md/GLOSSARY.md`）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: ```` ```json ... ``` ````。语言标记任写（`json`/`JSON`/没有），因为切片兜底会容错。
_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*(.*?)```", re.DOTALL | re.IGNORECASE)


class ModelOutputError(Exception):
    """模型输出认不出来或不合法（规格 §6.1、§9）。

    消息带**具体原因**，供重问时回传；调用方（Task 5）捕它并重问一次（有界）。
    """


@dataclass(frozen=True)
class ModelAnswer:
    """一条通过校验的模型回答。

    `header_rows` 是**表内 1-based** 的起止行号二元组，没有标题行区时是 `None`。
    `columns` 的元素是**路径**（各层用 `" / "` 拼接）、**且必须是字符串**（规格 §11.2）。
    没有标题行区的表：`header_rows=None` 且 `columns=()`，两者永远自洽。
    """

    header_rows: tuple[int, int] | None
    columns: tuple[str, ...]
    reason: str


def parse_model_output(raw: str, *, n_columns: int, n_rows: int) -> ModelAnswer:
    """把模型返回的原始文本解析成 `ModelAnswer`，不合法就抛 `ModelOutputError`。

    `n_columns` 是这张表在输入 jsonl 里的列数（校验四用它对齐源），`n_rows` 是表行数
    （校验一用它定 `header_rows` 的界）。两者都由调用方从 `InputTable` 得出。

    纯函数：不碰网络、不碰模型、不改全局状态（规格 §10「模型调用不进单元测试」）。
    """
    data = _load_object(raw)
    reason = _reason(data)
    header_rows, columns = _shape(data, n_columns, n_rows)
    return ModelAnswer(header_rows, columns, reason)


# --------------------------------------------------------------------- 解析


def _strip_fence(raw: str) -> str:
    """剥掉最外层 markdown 围栏；没有围栏就原样返回。

    必须发生在切片之前：前后杂字里也可能带花括号（例如模型先复述「{表范围}」）。

    但**捕获到的围栏里没有 `{` 就不能用**：模型可能先开一个空围栏再写答案
    （```` ``` ```` 紧跟 ```` ``` ````），此时用捕获块会把后面那个合格对象一起丢掉，
    报出来的还是误导性的「模型输出是空的」。宁可不剥，退回原文走切片兜底。
    """
    match = _FENCE_RE.search(raw)
    if match and "{" in match.group(1):
        return match.group(1)
    return raw


def _loads_object_slice(text: str, raw: str):
    """取第一个 `{` 到最后一个 `}` 再 `json.loads`（规格 §8 的兜底解析）。"""
    start = text.find("{")
    if start < 0:
        raise ModelOutputError(
            "模型输出里找不到 JSON 对象（没有 { 开头的对象）：%s" % _clip(raw)
        )
    end = text.rfind("}")
    if end < start:
        raise ModelOutputError(
            "模型输出的 JSON 像被截断了（有 { 但没有配对的 }）：%s" % _clip(raw)
        )
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelOutputError(
            "模型输出的 JSON 坏了（解析不了）：%s。原文：%s" % (exc, _clip(raw))
        ) from exc


def _load_object(raw: str) -> dict:
    """原始文本 ⇒ JSON 对象。

    先当整段就是 JSON 解析（最常见，也顺带把「合法 JSON 但不是对象」挡在门外：数组里包一个
    合格对象是**形状不对**，不是前后杂字，不许悄悄剥掉外层）；整段解析不了才退回切片兜底。
    """
    text = _strip_fence(raw).strip()
    if not text:
        raise ModelOutputError("模型输出是空的：没有任何文本可解析（连 JSON 都没有）")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _loads_object_slice(text, raw)

    if not isinstance(data, dict):
        raise ModelOutputError(
            "模型输出的顶层是 %s，不是 JSON 对象（契约只接受一个 JSON 对象）" % _type_name(data)
        )
    return data


# --------------------------------------------------------------------- 校验


def _reason(data: dict) -> str:
    """`reason` 必填（规格 §6）：它是审计线索，不是装饰，所以缺键 / 非字符串 / 空白都报错。"""
    if "reason" not in data:
        raise ModelOutputError(
            "模型输出缺少 reason 字段；reason 是必填的审计线索，"
            "要一句话说清凭什么这么判"
        )
    value = data["reason"]
    if not isinstance(value, str):
        raise ModelOutputError(
            "reason 必须是字符串，看到 %s：%s" % (_type_name(value), _clip(repr(value), 80))
        )
    if not value.strip():
        raise ModelOutputError(
            "reason 是空的（空字符串或只有空白）；它必须是一句话的审计线索，不能省略"
        )
    return value


def _shape(data: dict, n_columns: int, n_rows: int):
    """四条校验（规格 §6.1）里的形状部分：返回 `(header_rows, columns)`。

    校验顺序是刻意的：先 `columns` 的逐项形状（元素必须是字符串），再 `header_rows` 的区间，
    最后才查两者的自洽性与列数 —— 这样「空数组配了区间」报的是自洽性，而不是含糊的列数不符。
    """
    if "header_rows" not in data:
        raise ModelOutputError(
            "模型输出缺少 header_rows 字段；没有标题行区时必须显式给 header_rows: null"
        )
    if "columns" not in data:
        raise ModelOutputError(
            "模型输出缺少 columns 字段；没有标题行区时必须显式给 columns: []"
        )

    header_raw = data["header_rows"]
    columns_raw = data["columns"]

    # 校验三：数组，每项是**非空字符串**（规格 §11.2：对象绝不 str() 转换）
    if not isinstance(columns_raw, list):
        raise ModelOutputError(
            "columns 必须是数组，看到 %s：%s" % (_type_name(columns_raw), _clip(repr(columns_raw)))
        )
    for index, entry in enumerate(columns_raw, start=1):
        if not isinstance(entry, str):
            raise ModelOutputError(
                "columns 第 %d 项不是字符串（是 %s：%s）；每一项都必须是非空字符串路径，"
                "对象 / 数字 / 数组一律不接受" % (index, _type_name(entry), _clip(repr(entry), 80))
            )
        if not entry.strip():
            raise ModelOutputError(
                "columns 第 %d 项是空的（空字符串或只有空白）：模型没判出这一列，"
                "宁可失败也不写空列名" % index
            )

    # 校验二（自洽性）：null ⇔ []，两个方向都查
    if header_raw is None:
        if columns_raw:
            raise ModelOutputError(
                "header_rows 是 null，但 columns 给了 %d 项；没有标题行区时 "
                "columns 必须是空数组 []" % len(columns_raw)
            )
        # 裁定：没有列路径，列数检查不适用
        return None, tuple(columns_raw)

    header_rows = _header_pair(header_raw, n_rows)
    if not columns_raw:
        raise ModelOutputError(
            "columns 是空数组，但 header_rows 是 %s；空数组只能与 header_rows: null 配套"
            "（那表示这张表没有标题行区）" % list(header_rows)
        )

    # 校验四：列数 == 输入里这张表的列数
    if len(columns_raw) != n_columns:
        raise ModelOutputError(
            "列数不符：这张表在输入里有 %d 列，模型给了 %d 列（漏列 / 多列都要重问）"
            % (n_columns, len(columns_raw))
        )
    return header_rows, tuple(columns_raw)


def _header_pair(value, n_rows: int) -> tuple[int, int]:
    """校验一：`[起, 止]`，表内 1-based，`1 ≤ 起 ≤ 止 ≤ 表行数`，且**止不能到最后一行**。

    最后一条不是多余：标题行区**位于数据之上**，所以止 == 表行数就意味着一张表全是
    标题行区、没有数据区（极端到一行表时 `[1, 1]` 即如此）。
    """
    if not isinstance(value, list) or len(value) != 2:
        raise ModelOutputError(
            "header_rows 必须是 [起, 止] 这样两个整数行号（表内 1-based），或 null；"
            "看到 %s：%s" % (_type_name(value), _clip(repr(value)))
        )
    first, last = value
    if not _is_int(first) or not _is_int(last):
        raise ModelOutputError(
            "header_rows 的两个元素都必须是整数行号（表内 1-based）；看到 %s：%s"
            % (_type_name(value), _clip(repr(value)))
        )
    if first < 1 or last > n_rows:
        raise ModelOutputError(
            "header_rows %s 越界：本表 %d 行，起止是表内 1-based 行号，必须落在 1..%d 之间"
            "（1 ≤ 起 ≤ 止 ≤ %d）" % ([first, last], n_rows, n_rows, n_rows)
        )
    if first > last:
        raise ModelOutputError(
            "header_rows %s 反序：起（%d）必须 ≤ 止（%d），标题行区是一个连续行块"
            % ([first, last], first, last)
        )
    if last >= n_rows:
        raise ModelOutputError(
            "header_rows %s 的止（%d）已经是本表最后一行（共 %d 行），标题行区下面没有数据区；"
            "标题行区必须位于数据之上，所以止最多只能到 %d —— 整张表若真的是标题行区，"
            "那就没有数据区可言，只能判成 null" % ([first, last], last, n_rows, n_rows - 1)
        )
    return (first, last)


# --------------------------------------------------------------------- 小工具


def _is_int(value) -> bool:
    """`bool` 是 `int` 的子类，必须排除：否则 `[true, 1]` 会被静默当成 `[1, 1]`。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _type_name(value) -> str:
    """出错消息里说的类型名：JSON 侧的名字（`null`），不是 Python 的 `NoneType`。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return type(value).__name__


def _clip(text: str, limit: int = 200) -> str:
    """把回显的原文截断：错误消息要能回传给模型，不能把整段输出塞进去。"""
    return text if len(text) <= limit else text[:limit] + "…（后略）"
