# -*- coding: utf-8 -*-
"""编排（规格 §9）：逐记录、逐表隔离、部分成功照样出产物。

规格 §9 的全部行为都在这里，一共三层「不中断」：

1. **逐表隔离** —— 每张表一次调用（`ask_table`，自带**有界重试**），单表失败只标记这张表，
   **继续下一张表**。捕的是 `Exception` 而不是只有 `ModelOutputError`：真实网关可能抛
   `TimeoutError`、连接错误或任何第三方异常，只捕解析异常会让**整批**死在一次网络故障上
   —— 正是规格 §9 点名的那个坑（`excel-to-md validate` 上刚被判过 Important）。
   不捕 `BaseException`：`KeyboardInterrupt` 要能打断。
2. **单表失败照样写记录** —— 失败的表 `columns=()`、`header_rows=None`、带具体 `error` 文本；
   **绝不写空列名**。这个判断由 `output.TableResult` 表达（失败只看 `error is not None`，
   不设第二个会漂移的状态位）。
3. **逐记录隔离** —— `number_lines` 给原始行编号（空白行不算一条记录）、`parse_lines` 一行一行调
   `input.parse_record`，一条记录解析不了只跳过它并留一条诊断，其余各条照常跑完。
   **不用 `load_records`**：它对坏记录是原子抛错的（那是文件级故障，见 Task 7）。
   坏记录在产物里**不伪造身份**（`ID`/`FileName`/`sheet` 是空串），位置由结构化的
   `source_line`/`record_index` 给出，而且它**留在原位**（顺序不按调用方给的序号排，见 `run_batch`）。

**串行执行，不做并发**（规格 §9 最后一段）：顺序对排查更重要。
**退出码**不在这里 —— 那是 Task 7 的事；本模块的职责是「产物记录一定产出」。

术语一律用权威表述：**表范围**、**标题行区**、**数据区**。失败表的身份一律来自结构化的
`TableResult.range`，**不依赖异常消息里有没有表名**（`client.py` 改写 `exc.args` 对
`OSError` 子类不生效）。
"""
from __future__ import annotations

import collections
from typing import Iterable, Sequence

from .client import ModelClient, ask_table
from .input import (
    InputFormatError,
    InputRecord,
    InputTable,
    location_text,
    parse_record,
    strip_location,
)
from .output import TableResult, build_record
from .prompt import PROMPT_VERSION

#: 坏记录的**人读标记**：只出现在 `error` 文本里，**绝不进机器可读字段**。
#: 身份字段（`ID`/`FileName`/`sheet`）恰恰是坏记录读不出来的东西，一律写**空串**
#: ——「这一行压根没成为记录」；填一个像 ID 的占位符会让 N 条坏记录在 §11.1 的身份键上
#: 无法区分，而且那是个会骗人的 ID。位置改用结构化的 `source_line`/`record_index`。
BROKEN_RECORD_PLACEHOLDER = "BROKEN_LINE"

#: 一条原始输入行：原文 + 它作为**第几条记录**（1-based，按非空行计数）+ jsonl 行号。
#: 三个值由 `number_lines` 数好（Task 7 的 CLI 从文件里逐行读时也用它，不自己再数一遍）。
InputLine = collections.namedtuple("InputLine", ["raw", "record_index", "line_no"])


def number_lines(raw_lines: Iterable[str]) -> list[InputLine]:
    """把**原始行文本**编号成 `InputLine`：空行跳过且**不推进记录序号**，行号始终是 jsonl 行号。

    这是「空行不算一条记录」这条约定的**生产实现**（`input.load_records` 读文件时同一口径）；
    两者由 `tests/test_pipeline.py` 的 `BlankLineCountingAgreementTest` 用同一份字节钉在一起 ——
    约定写两遍必然漂开，所以调用方（Task 7 的 CLI）从文件里逐行读时用这个函数编号，
    不必自己再数一遍。
    """
    lines = []
    record_index = 0
    for line_no, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        record_index += 1
        lines.append(InputLine(raw, record_index, line_no))
    return lines


def parse_lines(
    lines: Sequence[InputLine],
) -> tuple[list[InputRecord], list[tuple[str, int, int, str]]]:
    """逐行解析（规格 §9「单记录失败 ⇒ 不中断整批」），返回 `(解析好的记录, 失败清单)`。

    空行跳过（不计入记录序号），其余每行交给 `input.parse_record`；只有 `InputFormatError`
    表示「这条记录的形状认不出来」，跳过后继续下一条 —— 其余各条照样有结果。

    失败清单的每一项是 `(原文, 记录序号, 行号, 出错消息)`：原文与位置是调用方把它变成
    **一条带诊断的产物记录**所需的全部信息（见 `run_batch`），出错消息则留给 stderr 日志。
    调用方不能靠 `len(records)` 反推哪条坏了 —— 序号必须显式带出来。

    只捕 `InputFormatError`：`UnicodeDecodeError` 之类是**文件级**故障（调用方读文件时就会撞上），
    不该在这里被一条条吞掉。
    """
    return _split(_scan_lines(lines))


def _split(
    scanned: Sequence[
        tuple[InputLine, InputRecord | None, tuple[str, int, int, str] | None]
    ],
) -> tuple[list[InputRecord], list[tuple[str, int, int, str]]]:
    """扫描结果 ⇒ `(解析好的记录, 失败清单)`，**两者都保持扫描顺序**。

    `run_batch` 与 `parse_lines` 共用这一处拆分（拆分规则只写一遍）；`run_batch` 另外还要
    逐行的配对，所以它拿的是扫描三元组、不从这里再反推位置。
    """
    records = [record for _, record, _ in scanned if record is not None]
    failures = [failure for _, _, failure in scanned if failure is not None]
    return records, failures


def _scan_lines(
    lines: Sequence[InputLine],
) -> list[tuple[InputLine, InputRecord | None, tuple[str, int, int, str] | None]]:
    """每行 ⇒ `(那一行, 解析结果或 None, 失败诊断或 None)`，**按输入顺序、一行对一项**。

    保留逐行的对应关系是刻意的：产物要按输入位置插回原位，而解析失败的行不在记录列表里，
    靠两个列表 zip 必然错位（本模块踩过这个坑）。所以配对在**扫描时就定下来**，
    出错消息也在同一处取回 —— 不为它再解析一遍。

    空白行跳过（与 `number_lines` / `input.load_records` 同一口径：空白行不算一条记录）；
    只捕 `InputFormatError`：`UnicodeDecodeError` 之类是**文件级**故障（调用方读文件时就该撞上），
    不该在这里被一条条吞掉。
    """
    scanned = []
    for line in lines:
        if not line.raw.strip():
            continue
        try:
            record = parse_record(line.raw, line.record_index, line.line_no)
        except InputFormatError as exc:
            failure = (line.raw, line.record_index, line.line_no, str(exc))
            scanned.append((line, None, failure))
        else:
            scanned.append((line, record, None))
    return scanned


def run_batch(
    lines: Sequence[InputLine],
    client: ModelClient,
    *,
    model: str,
    max_attempts: int = 2,
) -> tuple[list[dict], list[tuple[str, int, int, str]]]:
    """整批入口：逐行解析 + 逐表调用，返回 `(产物记录, 失败清单)`。

    产物**逐条对应输入的非空行、且保持原顺序**：解析不了的那条记录不是「没有产物」，
    而是被跳过后**合成一条带 `error` 的产物记录**（身份是空串 + `tables: []` + 结构化定位，
    形状见 `_unparsed_product`）。理由：沉默地少一行，下游无法区分「这条记录本来就没有结果」
    与「它坏了被丢掉」；而规格 §9 要的正是「整批跑完、产物照写」，跳过的痕迹必须留在产物里。

    位置是**结构上确定的**，不是事后排序猜的：坏记录就在扫描到它的那一轮里合成，因此
    「产物与输入同序」不依赖调用方给的 `record_index`（它可能重复或逆序，见
    `test_input_order_survives_wild_record_indexes`），也不是按行号排出来的。

    逐行配对（`_scan_lines` 的三元组）是这里必需的：`parse_lines` 的 `(records, failures)`
    两列表已经丢掉了「哪个产物对应哪一行」，只用它就得反过来猜位置 —— 那正是要避免的事。
    所以两个入口共用 `_scan_lines`（空行口径只有一处）与 `_split`（拆分规则只有一处），
    而不是各自再实现一遍。

    `attempts` 与 `prompt_version` 由 `run_pipeline` 统一写（单一来源，见那里的说明）。
    """
    scanned = _scan_lines(lines)
    records, failures = _split(scanned)
    docs = iter(run_pipeline(records, client, model=model, max_attempts=max_attempts))

    products = []
    for _, _, failure in scanned:
        if failure is None:
            # 成功的那几条按扫描顺序逐个取：与 `records` 的顺序一一对应
            products.append(next(docs))
        else:
            raw, record_index, line_no, message = failure
            products.append(
                _unparsed_product(raw, record_index, line_no, message, model, max_attempts)
            )
    return products, failures


def run_pipeline(
    records: Sequence[InputRecord],
    client: ModelClient,
    *,
    model: str,
    max_attempts: int = 2,
) -> list[dict]:
    """把已解析的记录跑成产物记录（`output.build_record` 的形状），**逐表隔离**。

    - 每张表一次 `ask_table`（失败在它内部**有界重试**，上限就是这里的 `max_attempts`）；
      某张表最终失败 ⇒ 该表 `columns=()`、`header_rows=None`、`error` 带具体文本，**继续下一张表**。
    - 一条记录**永远**产出一份产物：唯一能让它缺席的是调用方自己抛出的 `KeyboardInterrupt`。
    - `prompt_version` 取 `prompt.PROMPT_VERSION`（渲染提示词时用的那个常量，单一来源）；
      `attempts` 写本次运行的 `max_attempts`，即**每表允许的调用上界**。`ask_table` 不回传
      「实际用了几次」，为拿精确值去改它的返回契约代价大于收益 —— 逐表的审计线索本来就在
      `reason` 与 `error` 里。代价（若错）：`attempts` 的语义是「配置的上界」而不是「实际消耗」。
    - **串行**：按记录、按表的顺序依次调用，不并发（规格 §9 最后一段）。
    """
    products = []
    for record in records:
        tables = _table_results(record, client, max_attempts)
        products.append(
            build_record(
                record.record_id,
                record.file_name,
                record.sheet,
                tables,
                model=model,
                prompt_version=PROMPT_VERSION,
                attempts=max_attempts,
            )
        )
    return products


# --------------------------------------------------------------------- 逐表


def _table_results(
    record: InputRecord, client: ModelClient, max_attempts: int
) -> list[TableResult]:
    """记录里每张表各跑一次，**一张失败不碰下一张**。结果与输入的表一一对应（不拍平）。"""
    return [
        _table_result(table, client, max_attempts) for table in record.tables
    ]


def _table_result(
    table: InputTable, client: ModelClient, max_attempts: int
) -> TableResult:
    """一张表 ⇒ 一条 `TableResult`：成功带路径，失败带标记。

    捕 `Exception`（裁决：规格 §9 的异常面是整个 `Exception`）而不是只捕解析异常，
    但不捕 `BaseException` —— `KeyboardInterrupt` 必须仍能打断整批。
    失败时 `columns=()`、`header_rows=None`：**不写空列名**，也不给下游一个「看起来合格」的空结果。
    `error` 退到**异常类型名**：`str(exc)` 可能是空串（异常自带空文本，或 `__str__` 不读
    `client.py` 改写的 `args`），而 §9 要的是「带 error 文本」—— 空文本等于没线索。
    """
    try:
        answer = ask_table(client, table, max_attempts=max_attempts)
    except Exception as exc:  # noqa: BLE001 —— 刻意的宽异常面，理由见模块与函数文档
        return TableResult(
            table.range, (), None, "", error=str(exc) or type(exc).__name__
        )
    return TableResult(table.range, answer.columns, answer.header_rows, answer.reason)


# --------------------------------------------------------------------- 坏记录


def _unparsed_product(
    raw: str,
    record_index: int,
    line_no: int,
    message: str,
    model: str,
    max_attempts: int,
) -> dict:
    """解析不了的记录 ⇒ 一条**带诊断**的产物记录（身份是空串，`tables` 是空数组）。

    **不伪造身份**：`ID`/`FileName`/`sheet` 读不出来（那正是它坏掉的原因），所以一律写空串
    ——空身份如实地说「这一行没有成为记录」；占位符会谎报一个机器可读的 ID，还会让 N 条坏记录
    在规格 §11.1 的身份键上互相不可区分。哨兵只作为**人读标记**留在 `error` 文本里。

    **结构化定位**（规格 §11.1：诊断字段直接放在记录里）：`source_line` 是 jsonl 行号、
    `record_index` 是记录序号（与 `input.parse_record` 同一套约定，1-based、按非空行计数）。
    这两个键**只在记录级失败时**才写 —— 健康记录的形状与 Task 1 的产物契约逐字段一致。

    `error` 里给出序号、行号与截断后的原文，且**不复述** `parse_record` 消息里已有的定位前缀
    （同一事实写两遍，读的人会以为有两处线索）。原文截断是刻意的：一条几百 KB 的坏行不该把
    jsonl 撑爆。
    """
    detail = "{}解析失败，整条跳过（{}）：{}；原文：{}".format(
        location_text(record_index, line_no),
        BROKEN_RECORD_PLACEHOLDER,
        strip_location(message, record_index, line_no),
        _clip(raw),
    )
    product = build_record(
        "",
        "",
        "",
        (),
        model=model,
        prompt_version=PROMPT_VERSION,
        attempts=max_attempts,
        error=detail,
    )
    product["source_line"] = line_no
    product["record_index"] = record_index
    return product


def _clip(text: str, limit: int = 200) -> str:
    """回显原文时截断：坏记录里可能有整行几万个字符，产物与日志都不该被它淹掉。"""
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit] + "…（后略）"
