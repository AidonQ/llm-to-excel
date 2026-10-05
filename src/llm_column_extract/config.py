# -*- coding: utf-8 -*-
"""配置（规格 §8「运行形态与配置」）：配置项全部来自**环境变量**。

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `LLM_MODEL` | ✅ | 模型名 |
| `LLM_BASE_URL` | ✅ | 内网 OpenAI 兼容网关的 base_url |
| `LLM_API_KEY` | ❌ | 私有化网关常常不校验；缺省是空串（真实调用需要时才必须给） |
| `LLM_TIMEOUT` | ❌ | 秒，缺省 60；`0` 表示不设超时（`cli._client_timeout` 把它映射成客户端的 `None`） |
| `LLM_MAX_ATTEMPTS` | ❌ | 每表对模型调用的次数上界，缺省 2（= 第一次 + 重问一次）；必须 ≥ 1 |

**缺必需项 / 数值不合法 / 不在识别范围内**都抛 `ConfigError`，消息里点名**哪个环境变量**：
调用方（CLI）把它原样写到 **stderr** 并返回非 0（规格 §9 的诊断一律不上 stdout）。
**不猜、不给默认的模型名** —— 一个猜错的模型名会让整批跑出看似正常的垃圾。

`load_config(env)` 只读传进来的映射，**不读 `os.environ`**：生产路径的默认值由 `cli.main`
给（`os.environ`），测试因此可以塞一份假环境而不污染进程。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

#: 环境变量名（唯一来源，测试与 README 都引这里）
MODEL_ENV = "LLM_MODEL"
BASE_URL_ENV = "LLM_BASE_URL"
API_KEY_ENV = "LLM_API_KEY"
TIMEOUT_ENV = "LLM_TIMEOUT"
MAX_ATTEMPTS_ENV = "LLM_MAX_ATTEMPTS"

#: 缺省值：超时 60 秒、每表最多两次调用（= 第一次 + 重问一次，规格 §9）
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_ATTEMPTS = 2


class ConfigError(Exception):
    """配置缺失或不合法。消息里必须点名**哪个环境变量**、看到的什么值、期望什么。"""


@dataclass(frozen=True)
class Config:
    """一次运行的全部配置项（规格 §8）。`frozen` 是因为它跑批过程中不该变。"""

    model: str
    base_url: str
    api_key: str
    timeout: float
    max_attempts: int


def load_config(env: Mapping[str, str]) -> Config:
    """从环境变量映射读出 `Config`；缺必需项或数值不合法 ⇒ 抛 `ConfigError`。

    只读 `env`，**不碰 `os.environ`**（见模块说明）。
    """
    model = _required(env, MODEL_ENV)
    base_url = _required(env, BASE_URL_ENV)
    api_key = env.get(API_KEY_ENV, "")
    timeout = _timeout(env)
    max_attempts = _max_attempts(env)
    return Config(model, base_url, api_key, timeout, max_attempts)


def _required(env: Mapping[str, str], name: str) -> str:
    """必填项：缺失或**只有空白**都算缺失 —— 一个空白的 base_url 报错总比连不上好猜。"""
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(
            "缺少必需的环境变量 {}（配置项：模型名 / base_url / 密钥 / 超时 / 重试上限）".format(
                name
            )
        )
    return value.strip()


def _timeout(env: Mapping[str, str]) -> float:
    """超时（秒）：缺省 60，可以是小数，`0` 表示不设超时。

    **用 `float()` 解析**：`LLM_TIMEOUT=30.5` 与 `LLM_TIMEOUT=30` 都该被接受。

    `0` 这个哨兵的含义到 `cli._client_timeout` 才变成客户端的参数（`0` ⇒ `None`）：
    httpx 的 `timeout=0` 是**零秒**，不是「不限时」。
    """
    raw = env.get(TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT
    return _number(raw, TIMEOUT_ENV, cast=float, integer=False, minimum=0.0)


def _max_attempts(env: Mapping[str, str]) -> int:
    """每表调用次数上界：缺省 2，必须 ≥ 1。

    **用 `int()` 解析，刻意不接受 `2.5` 或 `2.0`**：它是对 `client.complete` 的循环次数，
    一个从没被真正核过的期望次数既不好解释也不好用；小数在这里是**写错**，如实报错。
    """
    raw = env.get(MAX_ATTEMPTS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_ATTEMPTS
    return _number(raw, MAX_ATTEMPTS_ENV, cast=int, integer=True, minimum=1)


def _number(raw: str, name: str, *, cast, integer: bool, minimum):
    """一个数值项：不是数、越界或不是有限数 ⇒ 抛 `ConfigError` 并点名变量。

    **不静默取默认**：写错一个字符（`LLM_MAX_ATTEMPTS=two`）却照默认值跑，等于配置被忽略
    而没人知道。

    `nan` / `inf` 也要挡掉：`float("nan")` 与任何数比较都是 `False`，只查 `>= minimum`
    会把它放过去，超时值从此变成一个随机行为。
    """
    try:
        value = cast(raw.strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "环境变量 {} 认不出来：{!r} 不是合法的{}".format(
                name, raw, "整数" if integer else "数字"
            )
        ) from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError("环境变量 {} 必须是有限数字，实际是 {!r}".format(name, raw))
    if value < minimum:
        raise ConfigError(
            "环境变量 {} 必须 ≥ {}，实际是 {}".format(name, _as_text(minimum), _as_text(value))
        )
    return value


def _as_text(value) -> str:
    """非法数值的报错文案里，整数不要显示成 `1.0`。"""
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
