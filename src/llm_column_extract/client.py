# -*- coding: utf-8 -*-
"""模型调用与**有界重试**（规格 §8、§9、§10）。

三件事：

- `ModelClient`：调用方只认这个接口（`complete(messages) -> str`）。Task 6 的编排与 Task 7 的
  CLI 都按它写，所以单元测试能塞 `FakeClient`，**模型调用不进单元测试**（规格 §10）。
- `LangChainClient`：真实实现。继续用 LangChain（规格 §8），在 `langchain_openai.ChatOpenAI`
  上**只** invoke 那两条消息 —— **不传 `response_format`、不依赖网关的结构化输出能力**：
  网关支持只是可选加速，兜底解析与校验已经在 Task 4 做完。
- `ask_table`：逐表一次调用（规格 §8），失败**有界重试**（规格 §9）。

两类失败都受 `max_attempts` 约束，而 `max_attempts` 就是**对 `client.complete` 的调用次数上限**
（默认 2 = 第一次 + 重问一次）：

| 失败 | 重试时怎么办 |
|---|---|
| 解析 / 校验失败（`ModelOutputError`） | 把**具体错误**追加成一条 user 消息**重问**（消息由 Task 4 写成，本就是为回传写的） |
| 传输异常（超时、网关 5xx…） | 原样重发；压根没有「上一次的回答」可纠正 |

上限用完仍失败 ⇒ **抛出去**（规格 §9）：解析失败抛 `ModelOutputError`，传输异常抛**原类型**
（超时还是超时，调用方仍能按类型分辨，这正是规格 §9 两行分别处理所需要的），两种都在消息里
补上**表范围** —— 500 条批次里炸了一张表，日志要能说清是哪张。

`langchain_openai` 的导入**放在 `__init__` 里**，不在模块顶层：运行时可以不装它，只用
`FakeClient` 的测试必须照样能 import 本模块。
"""
from __future__ import annotations

from typing import Protocol

from .input import InputTable
from .model_out import ModelAnswer, ModelOutputError, parse_model_output
from .prompt import build_messages

#: 重问时追加的那条 user 消息。`{exc}` 是 Task 4 的具体原因（哪个字段、看到什么、期望什么）。
_REASK = "上一次的回答不合规：{exc}。请只输出 JSON。"


class ModelClient(Protocol):
    """`ask_table` 要的最小接口：给两条消息，还一段文本。

    真实实现是下面的 `LangChainClient`，测试里是 `tests/_fake_client.py` 的 `FakeClient`。
    """

    def complete(self, messages: list[dict]) -> str: ...


class LangChainClient:
    """真实客户端：`langchain_openai.ChatOpenAI`（规格 §8 的配置项逐个落位）。"""

    def __init__(self, *, model: str, base_url: str, api_key: str, timeout: float) -> None:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # 缺依赖时说清缺哪个，别把 ImportError 原样抛给用户
            raise RuntimeError(
                "缺少 langchain-openai，请先安装（pip install langchain-openai）。"
                "它只有真实运行才需要；单元测试用 tests/_fake_client.py 的 FakeClient。"
            ) from exc
        self._chat = ChatOpenAI(
            model=model, base_url=base_url, api_key=api_key, timeout=timeout
        )

    def complete(self, messages: list[dict]) -> str:
        """调一次模型，返回文本。

        刻意**不传** `response_format` 之类的结构化输出参数（规格 §8）：能不能用网关的结构化
        输出都一样要跑通解析兜底，所以这里不引入这条依赖 —— 换模型、换网关都不改代码。
        """
        return self._chat.invoke(messages).content


def ask_table(
    client: ModelClient, table: InputTable, *, max_attempts: int = 2
) -> ModelAnswer:
    """问一张表并校验它的回答：`build_messages` 渲染 → `client.complete` → `parse_model_output`。

    `max_attempts` 是**对 `client.complete` 的调用次数上限**（默认 2 = 第一次 + 重问一次），
    解析失败与传输异常都算在内，所以不存在「两类失败各重试一次」的四次调用。

    成功 ⇒ `ModelAnswer`；上限用完 ⇒ 抛 `ModelOutputError`（解析 / 校验失败）或**原类型**的
    传输异常，两者的消息里都带 `table.range`。

    `n_columns` / `n_rows` 从 `table` 本身算（列优先：列数就是列数，行数取最长的那一列；
    读取器保证每列等长），不解析表范围字符串 —— 表范围只是给模型看的上下文。
    """
    if max_attempts < 1:
        raise ValueError(
            "max_attempts 至少要是 1（它是对 client.complete 的调用次数上限）：%r"
            % (max_attempts,)
        )

    n_columns = len(table.columns)
    n_rows = max(len(cells) for cells in table.columns)
    messages = build_messages(table)

    for attempt in range(1, max_attempts + 1):
        try:
            raw = client.complete(messages)
        except Exception as exc:  # 传输异常：超时、网关 5xx……类型不可预知，一律有界重试
            if attempt == max_attempts:
                _name_table(exc, table, "模型调用连续 {} 次失败（传输异常）".format(max_attempts))
                raise
            continue

        try:
            return parse_model_output(raw, n_columns=n_columns, n_rows=n_rows)
        except ModelOutputError as exc:
            if attempt == max_attempts:
                _name_table(
                    exc, table, "模型回答连续 {} 次不合规（解析 / 校验失败）".format(max_attempts)
                )
                raise
            # 重问的意义就在这条消息：原样带上「哪个字段、看到什么、期望什么」
            messages = messages + [{"role": "user", "content": _REASK.format(exc=exc)}]


def _name_table(exc: BaseException, table: InputTable, note: str) -> None:
    """把「哪张表、怎么放弃的」补进异常消息，**保留异常原类型**。

    保留类型是刻意的：`args` 是 `str(exc)` 的来源，改它就能带上下文，而 `except TimeoutError`
    之类的调用方（Task 6 要按类型分辨「解析失败」与「传输异常」）继续照常工作。另包一层
    自定义异常会把类型信息抹掉，逼下游去猜。
    """
    detail = "表 {}：{}；不再重试：{}".format(table.range, note, exc)
    exc.args = (detail,)
