# -*- coding: utf-8 -*-
"""`FakeClient`：**测试替身**，放在 `tests/` 而不是产品代码里（规格 §10 的关键设计）。

规格 §10 把它定义成测试工具：模型调用不进单元测试，靠它把「有界重试」「逐表隔离」
「部分成功」这些行为全部测穿，而且跑得飞快。它是测试工具，所以**不进 `src/`**。

契约：

- 构造时给一串**预置响应**（`str` 或 `BaseException`），`complete` 按序取一个：
  是异常就抛，是字符串就返回。
- 每次调用都记进 `calls`（**调用时的消息快照**），测试才能断言「第二次请求里带没带上次的错误」。
- 响应发完还想再取 ⇒ 抛 `AssertionError` 并说清「预置响应用完了」：这必然是测试自己写错了
  次数，悄悄返回 `""` 只会把错误藏进下游的解析失败里。
"""
from __future__ import annotations

from typing import Sequence


class FakeClient:
    """按序发预置响应的 `ModelClient`（见 `client.ModelClient` 的协议）。"""

    def __init__(self, responses: Sequence[str | BaseException]) -> None:
        self._responses = list(responses)
        #: 每次 `complete` 时**当时的** messages（快照，之后 `ask_table` 再追加也不影响）
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError(
                "FakeClient 的预置响应用完了：这是第 {} 次调用，但只预置了 {} 个响应"
                "（检查测试里给的响应个数）".format(len(self.calls), len(self.calls) - 1)
            )
        response = self._responses.pop(0)
        # 用 BaseException 而不是 Exception：预置 `KeyboardInterrupt` 时也必须**真抛**，
        # 否则「编排不吞 Ctrl-C」这条根本测不出来（测试替身会把中断当成一段返回值）。
        if isinstance(response, BaseException):
            raise response
        return response
