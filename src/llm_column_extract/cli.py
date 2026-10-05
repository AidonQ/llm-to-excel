# -*- coding: utf-8 -*-
"""CLI（规格 §8「运行形态与配置」、§9「错误处理」、§11「产出契约」）。

```
python -m llm_column_extract <input.jsonl> [-o <dir>]
```

**两份产物，都强制产出**（规格 §8）：`<name>.out.jsonl`（给机器）+ `<name>.out.md`（给人），
都 UTF-8、`newline="\\n"`；`<name>` 是输入文件名的 **stem**（去掉最后一个扩展名），两份产物
共享它。md 由**代码**从 jsonl 渲染
（`render.render_markdown`），不由模型生成 —— 所以两份不可能互相矛盾，解析失败也不会
连带毁掉 md。头部形状与 `render.py` 的既有实现对齐（`meta` 逐键渲染），**不在这里另拼一层**。

**逐记录隔离的接线方式**（Task 6 裁决）：**不用** `load_records` —— 它对坏记录是原子抛错的，
用了它逐记录隔离在真实 CLI 里永远不会触发。这里自己逐行读文件（`encoding="utf-8-sig"`，
否则带 BOM 的第 1 条会莫名其妙变成一条坏记录），交给 `pipeline.number_lines` 编号
（空行跳过、不推进记录序号，口径与 `input.load_records` 由测试钉在一起），再走
`pipeline.run_batch`。坏记录**留在产物原位**、身份写空串、带结构化定位，不静默丢弃。

**五种结局**（本节就是报告里要交代的那份取舍）：

| 结局 | 产物 | stdout | stderr | 退出码 |
|---|---|---|---|---|
| 全好 | 两份都写 | 一行摘要 | 空 | `0` |
| 有记录级或表级失败 | **两份照写**（部分成功要留下，规格 §9 最后一行） | 一行摘要 | 逐条诊断 | `1` |
| 文件级故障（读不了 / 空文件 / 整体解码失败） | **不写任何产物** | 空 | 一条错误 | `3` |
| 配置不全 / 客户端造不出来 | 不写任何产物 | 空 | 一条错误 | `2` |
| 产物写不下去 | 尽力 | 空 | 一条错误 | `4` |

⇒ **诊断一律走 stderr，stdout 只有调用方会拿去管道的那一行摘要**（规格 §9；与
`excel-to-md` 的取舍刻意不同：这里是批量，500 条里坏 3 条不该让你损失另外 497 条的结果）。
**串行执行**，不做并发（规格 §9 最后一段）。

`client_factory` 与 `env` 是**端到端测试的注入点**：有了它们，整条 CLI 路径可以在
**无网络、无模型**下测通（假 client 在 `tests/_fake_client.py`），而且不必改进程环境。
真实路径的默认值是 `os.environ` + `client.LangChainClient`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence

from .client import LangChainClient, ModelClient
from .config import Config, ConfigError, load_config
from .input import location_text, strip_location
from .pipeline import number_lines, run_batch
from .prompt import PROMPT_VERSION
from .render import render_markdown

#: 退出码：0 只在**什么都没失败**时给（规格 §9 最后一行要的就是这个区分）。
EXIT_OK = 0
#: 整批跑完了，但有记录级或表级失败 —— **产物已经写出来了**。
EXIT_PARTIAL = 1
#: 配置不全、或真实客户端造不出来（缺 `langchain-openai` 在这里报）。
#: **用法错误也走这个码**（argparse 自己的约定）—— 2 因此是「启动前就停了」这一类。
EXIT_CONFIG = 2
#: 文件级故障：文件读不了、是空文件、或整体无法解码 ⇒ **不写任何产物**。
EXIT_INPUT = 3
#: 产物写不下去（盘满、没有写权限……）。
EXIT_OUTPUT = 4

#: JSONL 用 `ensure_ascii=False` 写：产物是人也要扫一眼的中文列名，转成 `\uXXXX` 没法读。
_PROGRAM = "llm-column-extract"


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[Config], ModelClient] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """跑一次批处理，返回退出码（不调 `sys.exit`，`__main__.py` 才调）。

    `client_factory` 缺省是真实客户端（`client.LangChainClient`，它在 `__init__` 里才
    import `langchain_openai`，所以没有这个依赖时**只在真跑**才报错）；测试注入假 client。
    `env` 缺省是 `os.environ`：本模块**只在这里**读进程环境。
    """
    env = os.environ if env is None else env
    try:
        args = _parse_args(argv)
    except _UsageError as exc:
        return _fail(exc, EXIT_CONFIG)
    except SystemExit as exc:
        # `-h/--help`（码 0）与「没给 input」（码 2）：argparse 已经把帮助/用法与原因写到
        # **stderr** 了，这里只把它的退出码**变成返回值** —— `main` 的契约是「返回退出码」，
        # 而要拿它当库来调的人（报告里的冒烟脚本、将来的调度器）不该为了看一眼用法被
        # `SystemExit` 打死。`__main__.py` 再把这个值交给 `sys.exit`，命令行行为不变。
        code = exc.code
        return code if isinstance(code, int) else 1 if code else 0

    try:
        config = load_config(env)
    except ConfigError as exc:
        return _fail(exc, EXIT_CONFIG)

    # 文件级故障优先：读不了的文件不该先花一次客户端构造（缺依赖时它会抢先报错）
    try:
        lines = _read_lines(args.input)
    except _InputError as exc:
        return _fail(exc, EXIT_INPUT)

    try:
        client = _make_client(config, client_factory)
    except Exception as exc:  # 缺 langchain-openai、base_url 不合法、网关参数被拒……
        # 文案只说「造不出来 + 真实原因」：真实运行还需要 langchain-openai，但它只是众多
        # 可能原因之一（`base_url` 写成 `ftp://` 也在这里炸），把依赖当成结论会误导排查。
        return _fail(
            "造不出模型客户端（{}）。真实运行需要 langchain-openai（pip install "
            "langchain-openai）".format(exc),
            EXIT_CONFIG,
        )

    products, failures = run_batch(
        number_lines(lines), client, model=config.model, max_attempts=config.max_attempts
    )

    jsonl_path, md_path = _product_paths(args.input, args.output_dir)
    try:
        _write_products(products, {"input": args.input, "model": config.model,
                                   "prompt_version": PROMPT_VERSION}, jsonl_path, md_path)
    except OSError as exc:
        return _fail("写产物失败：{}".format(exc), EXIT_OUTPUT)

    problems = _diagnose(products, failures)
    _summary(products, failures, jsonl_path, md_path)
    return EXIT_OK if not problems else EXIT_PARTIAL


# ------------------------------------------------------------------ 参数与读入


class _UsageError(Exception):
    """命令行参数认不出来（`-o` 后面没跟值之类）。"""


class _InputError(Exception):
    """**文件级**输入故障：读不了、空文件、整体解码失败。"""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """`<input.jsonl> [-o <dir>]`。

    刻意**不用** `parser.parse_args(argv)` 的默认行为：它出错就 `sys.exit(2)`，而 `main`
    是要能被单元测试调用的（返回值才是契约）。这里用 `parse_known_args` 让它只在**成功**
    时返回值，剩余部分自己判 —— 出错路径因此全部走返回值。
    """
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description="从 OCR Markdown 里提取列名，逐表写出 jsonl + md 两份产物。",
    )
    parser.add_argument("input", help="输入 jsonl 路径（一行一条记录）")
    parser.add_argument(
        "-o", "--output-dir", default=None, help="产物目录（缺省：当前目录）"
    )
    args, rest = parser.parse_known_args(list(argv) if argv is not None else None)
    if rest:
        raise _UsageError(
            "认不出的参数 {}（用法：{} <input.jsonl> [-o <dir>]）".format(
                " ".join(rest), _PROGRAM
            )
        )
    return args


def _read_lines(path: str) -> list[str]:
    """把输入文件读成**原始行**（含换行），空文件算文件级故障。抛 `_InputError`，原因见消息。

    `encoding="utf-8-sig"`：伙伴的文件头可能带 BOM，它不该让第 1 条记录变成一条坏记录
    （而报错里还看不到 BOM 这件事）。`UnicodeDecodeError` 是 `ValueError` 的子类、
    **不是** `OSError`，所以单独捕一次 —— 它是「整个文件读不出来」，不是「某条记录坏了」。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        raise _InputError("输入文件不存在：{}".format(path)) from None
    except IsADirectoryError:
        raise _InputError("输入是个目录，不是 jsonl 文件：{}".format(path)) from None
    except UnicodeDecodeError as exc:
        raise _InputError("输入文件整体解不开（只认 UTF-8）：{}：{}".format(path, exc)) from None
    except OSError as exc:
        raise _InputError("读不了输入文件：{}：{}".format(path, exc)) from None

    if not any(line.strip() for line in lines):
        raise _InputError("输入文件里没有一条记录（空文件或全是空行）：{}".format(path))
    return lines


def _client_timeout(seconds: float) -> float | None:
    """把 `Config.timeout` 变成 `LangChainClient` 的 `timeout` 参数：**0 ⇒ `None`（不设超时）**。

    0 是我们文档里「不设超时」的哨兵（README 的配置表、`config._timeout`），而这个 0 一旦
    原样交给 `ChatOpenAI(timeout=0)`，到了 httpx（OpenAI SDK 的传输层）就是 `Timeout(0)`：
    连接/读/写/连接池**全部零秒**，跟着 README 配的运维会得到「每一次调用都失败」。
    httpx 真正表示不设超时的值是 `None`，所以在这里映射。

    映射只发生在这里：`Config.timeout` 的含义不变（`0` 还是 `0`），也**不**偷偷换成某个大
    数字 —— 那是另一个承诺，而且长调用照样会翻车。
    """
    return None if seconds == 0 else seconds


def _make_client(
    config: Config, client_factory: Callable[[Config], ModelClient] | None
) -> ModelClient:
    """真实客户端或注入的假 client。`LangChainClient` 的 import 是惰性的（在 `__init__` 里）。"""
    factory = client_factory or (
        lambda cfg: LangChainClient(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=_client_timeout(cfg.timeout),
        )
    )
    return factory(config)


# ------------------------------------------------------------------ 产物


def _product_paths(input_path: str, output_dir: str | None) -> tuple[str, str]:
    """`<name>.out.jsonl` + `<name>.out.md`（规格 §8 的两份产物）。

    `<name>` 是输入文件名的 **stem**（去掉最后一个扩展名）：`batch.jsonl` ⇒
    `batch.out.jsonl` / `batch.out.md`；没有扩展名的 `ocr/batch` 一样是 `batch.out.*`。
    取 stem 的理由就是规格 §8 的写法：两份产物同属一次运行、**共享同一个 `<name>`**。
    输入文件自己的名字与位置不变。`-o` 缺省是当前目录。
    """
    directory = output_dir if output_dir else os.curdir
    name = os.path.splitext(os.path.basename(input_path))[0]
    return (
        os.path.join(directory, name + ".out.jsonl"),
        os.path.join(directory, name + ".out.md"),
    )


def _write_products(
    products: Sequence[dict], meta: dict, jsonl_path: str, md_path: str
) -> None:
    """写两份产物：jsonl 逐行一个 JSON 对象，md 由**代码**从同一批记录渲染（规格 §11.3）。

    两份都用 `newline="\\n"`：Windows 上的默认换行是 `\\r\\n`，会让产物在不同平台上不一致
    （下游按行切分时要处理多余的 `\\r`）。md 的头部由 `render_markdown(records, meta)` 逐键
    渲染，**这里不另拼一套头部**（Task 1 裁决）。
    """
    directory = os.path.dirname(os.path.abspath(jsonl_path))
    os.makedirs(directory, exist_ok=True)

    with open(jsonl_path, "w", encoding="utf-8", newline="\n") as stream:
        for product in products:
            stream.write(json.dumps(product, ensure_ascii=False))
            stream.write("\n")

    with open(md_path, "w", encoding="utf-8", newline="\n") as stream:
        # 渲染结果以空行收尾（render.py 的既有形状），补一个换行让 md 文件以 \n 结束
        stream.write(render_markdown(products, meta) + "\n")


# ------------------------------------------------------------------ 诊断与摘要


def _diagnose(products: Sequence[dict], failures: Sequence[tuple]) -> list[str]:
    """把**每一处**失败写到 stderr（诊断不上 stdout），返回诊断行的列表。

    失败与否**只看 `error`**（Task 6 裁决：不设第二个会漂移的状态位）：记录级看产物记录里的
    `error`、表级看 `tables[i]["error"]`。

    坏记录只打**一条**诊断，取产物里那条完整的 —— 它已经带位置、哨兵与截断后的原文；
    `failures` 只用来兜住「有失败却没有对应产物」这种不该发生的形状（那时它是唯一线索）。
    同一个事实写两遍，读日志的人会以为有两处问题。
    """
    #: 坏记录的 `record_index`：产物里这些条已经给出完整诊断，`failures` 不再重复一份
    reported = {
        product.get("record_index")
        for product in products
        if product.get("error") is not None
    }

    lines = []
    for _raw, record_index, line_no, message in failures:
        if record_index in reported:
            continue
        lines.append(
            "{}：{}：{}".format(
                _PROGRAM,
                location_text(record_index, line_no),
                strip_location(message, record_index, line_no),
            )
        )

    for product in products:
        record_error = product.get("error")
        if record_error is not None:
            # `record_error` 自己以 `input.location_text` 的措辞开头（`pipeline` 合成的），
            # 所以这里**不再**前置一次位置
            lines.append("{}：{}".format(_PROGRAM, record_error))
        for index, table in enumerate(product.get("tables") or [], start=1):
            if table.get("error") is None:
                continue
            lines.append(
                "{}：ID={} / {}（第 {} 张表）：表范围 {}：{}".format(
                    _PROGRAM,
                    product.get("ID", ""),
                    _who(product),
                    index,
                    table.get("range", ""),
                    table["error"],
                )
            )

    for line in lines:
        print(line, file=sys.stderr)
    return lines


def _who(product: dict) -> str:
    """一条记录的定位：健康记录给 `FileName/sheet`，坏记录只有结构化位置。

    坏记录那份文案引 `input.location_text`（措辞的唯一来源），**不在这里再拼一遍**。
    """
    if product.get("error") is not None:
        return location_text(
            product.get("record_index", "?"), product.get("source_line", "?")
        )
    return "{}/{}".format(product.get("FileName", ""), product.get("sheet", ""))


def _summary(
    products: Sequence[dict],
    failures: Sequence[tuple],
    jsonl_path: str,
    md_path: str,
) -> None:
    """stdout 的**唯一**输出：一行摘要，调用方可以直接管道下去。

    诊断不在这里（它们在 stderr）。摘要里给出两份产物的路径，人/脚本才知道东西去哪了。
    """
    bad_records = len(failures)
    bad_tables = sum(
        1
        for product in products
        for table in product.get("tables") or []
        if table.get("error") is not None
    )
    note = ""
    if bad_records or bad_tables:
        note = "（{} 条记录、{} 张表失败，诊断见 stderr）".format(bad_records, bad_tables)
    print(
        "写出 {} 条记录{}：{}、{}".format(
            len(products), note, os.path.abspath(jsonl_path), os.path.abspath(md_path)
        )
    )


def _fail(message, code: int) -> int:
    """一条诊断 ⇒ stderr，返回退出码。**stdout 一个字都不写**。

    程序名前缀只在这里加一次：调用方给**原因**，不给整行。
    """
    print("{}：{}".format(_PROGRAM, message), file=sys.stderr)
    return code
