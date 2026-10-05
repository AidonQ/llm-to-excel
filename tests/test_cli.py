# -*- coding: utf-8 -*-
"""CLI 与配置（规格 §8、§9、§11）：端到端，**无网络、无模型**。

`main(argv, client_factory=..., env=...)` 的两个注入口就是本文件全部测试的基础：

- `client_factory` 让整条 CLI 路径跑在 `tests/_fake_client.py` 的 `FakeClient` 上 ——
  本运行时**没有装 `langchain_openai`**，所以任何测试都不得构造 `LangChainClient`
  （规格 §10「模型调用不进单元测试」）。
- `env` 让配置从**传进来的映射**读，测试因此不必改进程环境（`os.environ` 不被污染，
  也不会串到别的测试模块）。生产路径的默认值是 `os.environ`。

这里盯住七件事：

1. **两份产物** —— `<name>.out.jsonl` + `<name>.out.md`（`<name>` = 输入文件名的 stem），
   UTF-8、`newline="\\n"`，且 md 是**代码从 jsonl 渲染**的（规格 §8）：表范围与列路径必须
   真在 md 里。
2. **退出码 0 只在什么都没失败时**；有失败（记录级或表级）仍**照写产物**、退出码非 0
   （规格 §9 最后一行，与 `excel-to-md` 刻意不同）。
3. **文件级故障**（文件不可读、空文件、整体无法解码）⇒ stderr 报错、非 0 退出、
   **不写任何产物**。
4. **逐记录隔离**真的在 CLI 里生效 —— 用的是 `run_batch` + `number_lines` + `parse_record`，
   **不是**对坏记录原子抛错的 `load_records`（Task 6 裁决）。
5. **BOM**：用 `encoding="utf-8-sig"` 读，带 BOM 的第 1 条记录不该变成一条坏记录。
6. **诊断只走 stderr**；stdout 只有调用方会拿去管道的那一行摘要。
7. **缺配置**在 stderr 上报、非 0 退出，stdout 一个字都不许有。

术语一律用权威表述：**表范围**、**标题行区**、**数据区**。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from _fake_client import FakeClient

from llm_column_extract import cli
from llm_column_extract.cli import _product_paths, main
from llm_column_extract.config import Config, ConfigError, load_config
from llm_column_extract.prompt import PROMPT_VERSION

#: 项目根（`tests/` 的上一级）：子进程要以它为 `cwd`，`-m llm_column_extract` 才认得到包
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
_TESTS_DIR = os.path.join(_ROOT, "tests")

#: 子进程的宽限：沙箱里 `subprocess.run` 用**管道**会永久挂住（本文件全部改成文件重定向），
#: 万一别的环境里真的挂住，让测试**失败**，而不是陪着一起挂。
_SUBPROCESS_TIMEOUT = 120

#: 合规回答（`header_rows` 是**表内 1-based**，见规格 §6.1）
GOOD_A = json.dumps(
    {
        "header_rows": [1, 1],
        "columns": ["产品", "数量", "单价"],
        "reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据",
    },
    ensure_ascii=False,
)
#: 另一张表（2 列）的回答 —— 列数要与表对齐，否则校验四会拒收
GOOD_B = json.dumps(
    {
        "header_rows": [1, 1],
        "columns": ["地区", "金额"],
        "reason": "行1 是列名行（标题行区的最下面一行），行2 起是数据",
    },
    ensure_ascii=False,
)
#: 坏 JSON：`ask_table` 有界重试（两次）后该表标记失败
BAD_JSON = "这不是 JSON，模型跑题了"

#: 一条**完整**的配置：CLI 必需项都在
ENV = {
    "LLM_MODEL": "deepseek-v4",
    "LLM_BASE_URL": "http://gateway.internal/v1",
    "LLM_API_KEY": "sk-test",
    "LLM_TIMEOUT": "30",
    "LLM_MAX_ATTEMPTS": "1",
}

#: 子进程那一组的最小配置：`main` **先**读配置、**再**读输入，所以哪怕只是「输入文件不存在」
#: 也要给两个必需项（否则它先以退出码 2 停在缺 `LLM_MODEL` 上，测不到想测的东西）。
E2E_ENV = {
    "LLM_MODEL": "deepseek-v4",
    "LLM_BASE_URL": "http://gateway.internal/v1",
    "LLM_MAX_ATTEMPTS": "1",
}


def table_json(range_str, **columns):
    """一个按列组织的表对象（与 `input.py` 的识别规则同形）。"""
    table = {"range": range_str}
    table.update(columns)
    return table


def record_json(record_id, tables, file_name="book.xlsx", sheet="Sheet1"):
    """一条合法记录的 jsonl 原文。"""
    return json.dumps(
        {
            "ID": record_id,
            "FileName": file_name,
            "sheet": sheet,
            "tables": tables,
        },
        ensure_ascii=False,
    )


#: 一条记录 + 两张表（3 列 4 行、2 列 4 行）：对应 GOOD_A / GOOD_B
GOOD_RECORD = record_json(
    "7",
    [
        table_json(
            "A2:C5",
            产品=["产品", "甲", "乙", "丙"],
            数量=["数量", "1", "2", "3"],
            单价=["单价", "10", "20", "30"],
        ),
        table_json(
            "E2:F5",
            地区=["地区", "东部", "西部", "北部"],
            金额=["金额", "1", "2", "3"],
        ),
    ],
)


class _CliTestCase(unittest.TestCase):
    """夹具：输入/产物都放在**已存在的临时目录**里用 `mkstemp` 造输入（不新建目录）。

    产物的名字**算出来、不预先创建**（见 `_products`）：预先创建会让「产物存在」这类断言在
    CLI 什么都没写时也成立 —— 本文件踩过这个坑。清理一律 `addCleanup`，`%TEMP%` 里不留东西。
    """

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _temp_path(self, name, suffix, directory=None):
        """一个已存在的空文件 ⇒ 路径。

        `suffix` 必须自带点（`mkstemp` 会原样拼接，不会补点）：写 `"jsonl"` 得到的是
        `….outjsonl`，与 CLI 拼出来的名字对不上，测试就会对着一个 CLI 从没碰过的路径断言。
        """
        handle, path = tempfile.mkstemp(prefix=name + ".", suffix=suffix, dir=directory)
        os.close(handle)
        self.addCleanup(self._remove, path)
        return path

    def _input(self, lines, encoding="utf-8", bom=False):
        """写一份输入 jsonl，返回路径。`lines` 里是 str（原始行，可为空行）。"""
        path = self._temp_path("cli.in", ".jsonl")
        text = "".join(line + "\n" for line in lines)
        with open(path, "w", encoding=encoding, newline="\n") as stream:
            stream.write(("\ufeff" if bom else "") + text)
        return path

    def _bytes_input(self, payload):
        """写一份**字节**输入（用来造整体无法解码的文件）。"""
        path = self._temp_path("cli.bin", ".jsonl")
        with open(path, "wb") as stream:
            stream.write(payload)
        return path

    def _missing_input(self):
        """一个**不存在**的输入路径（`mkstemp` 造出来立刻删掉，名字还在）。"""
        path = self._temp_path("cli.missing", ".jsonl")
        os.remove(path)
        return path

    def _out_dir(self, input_path):
        """`-o` 的值：输入文件所在的那个**已存在**目录。"""
        return os.path.dirname(os.path.abspath(input_path))

    def _products(self, input_path):
        """两份产物**该有的**路径（CLI 负责写出来，这里**不**预先创建）。

        为什么名字要算：`mkstemp` 的随机段在 `suffix` **之前**，`<name>.out.jsonl` 这种
        「点后面还有点」的名字拼不出来。所以名字由 `cli._product_paths` 定（规则：输入文件名
        去掉最后一个扩展名 + `.out.jsonl` / `.out.md`）。

        为什么**不**预先创建：预创建会让「两份产物都在」在 CLI 什么都没写时也成立
        （`ArgumentTest.test_long_flag_means_the_same_as_short_one` 就因此形同虚设）。
        清理一并登记 —— 断言「不该有产物」的测试也用它：真被写出来了也不会漏在 `%TEMP%` 里。
        """
        jsonl_path, md_path = _product_paths(input_path, self._out_dir(input_path))
        for path in (jsonl_path, md_path):
            self.addCleanup(self._remove, path)
        return jsonl_path, md_path

    def _new_output_dir(self):
        """一个**还不存在**的产物目录（`-o` 的普通用法：CLI 自己 `os.makedirs`）。

        位置选在仓库根目录下：本沙箱里**已存在的临时目录可以写文件，新建的子目录不行**
        （实测 `mkdtemp()` 里再 `open(..., "w")` 报 WinError 5），而工作区下的新建目录可以。
        上一次跑崩留下的同名目录先清掉，保证这次真的走 `os.makedirs`。
        """
        path = os.path.join(_ROOT, ".cli-new-out-dir")
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def _run(self, args, responses=(), factory=None, env=None):
        """调 `main`，返回退出码、stdout/stderr、用过的 client 与它收到的 `Config`。

        `responses` 是喂给 `FakeClient` 的预置响应；`factory` 覆盖默认注入点。
        """
        captured = {}
        client = FakeClient(responses)

        def default_factory(config):
            captured["config"] = config
            return client

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                list(args),
                client_factory=factory or default_factory,
                env=dict(ENV if env is None else env),
            )
        return {
            "code": code,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "client": client,
            "config": captured.get("config"),
        }


class CliTest(_CliTestCase):
    def test_writes_both_jsonl_and_markdown(self):
        # 端到端：一条记录、两张表 ⇒ 两份产物，jsonl 逐表不拍平，md 由代码渲染
        input_path = self._input([GOOD_RECORD])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        result = self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B])

        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertTrue(os.path.exists(jsonl_path), "没写出 jsonl 产物")
        self.assertTrue(os.path.exists(md_path), "没写出 md 产物")

        with open(jsonl_path, "r", encoding="utf-8", newline="") as stream:
            raw = stream.read()
        self.assertNotIn("\r", raw, "jsonl 必须用 newline='\\n' 写")
        self.assertTrue(raw.endswith("\n"), "jsonl 每条记录自占一行")

        records = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["ID"], "7")
        self.assertEqual([t["range"] for t in record["tables"]], ["A2:C5", "E2:F5"])
        self.assertEqual(record["tables"][0]["columns"], ["产品", "数量", "单价"])
        self.assertNotIn("error", record)
        self.assertEqual(record["prompt_version"], PROMPT_VERSION)

        with open(md_path, "r", encoding="utf-8", newline="") as stream:
            md = stream.read()
        self.assertNotIn("\r", md, "md 必须用 newline='\\n' 写")
        # 人的那份不能是空壳：表范围与列路径都要在
        self.assertIn("A2:C5", md)
        self.assertIn("E2:F5", md)
        self.assertIn("`产品`", md)

        # 配置真的从 env 映射来（不是从 os.environ）
        self.assertEqual(
            result["config"],
            Config(
                model="deepseek-v4",
                base_url="http://gateway.internal/v1",
                api_key="sk-test",
                timeout=30.0,
                max_attempts=1,
            ),
        )

    def test_exit_code_is_zero_when_everything_succeeds(self):
        input_path = self._input([GOOD_RECORD, GOOD_RECORD])
        self._products(input_path)
        out_dir = self._out_dir(input_path)

        result = self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B, GOOD_A, GOOD_B])

        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(result["stderr"], "", "全好时 stderr 不该有诊断")
        self.assertEqual(len(result["stdout"].splitlines()), 1, result["stdout"])

    def test_exit_code_is_nonzero_when_a_table_failed_but_products_are_still_written(self):
        # 规格 §9 最后一行：整批跑完 ⇒ 退出码非 0，但**产物照写**
        input_path = self._input([GOOD_RECORD])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        # `FakeClient` 的响应数就是「每表几次调用」的账本：配置里 `LLM_MAX_ATTEMPTS=1`
        # ⇒ 第一张表一次坏 JSON 就判失败，第二张表照常出结果（下面另有 `max_attempts=2`
        # 的一条，专门测「带具体错误重问一次」）。
        result = self._run([input_path, "-o", out_dir], [BAD_JSON, GOOD_B])

        self.assertNotEqual(result["code"], 0, "有表失败就必须非 0 退出")
        self.assertTrue(os.path.exists(jsonl_path), "表级失败也必须留下产物")
        self.assertTrue(os.path.exists(md_path))

        with open(jsonl_path, "r", encoding="utf-8") as stream:
            record = json.loads(stream.read().splitlines()[0])
        self.assertEqual(record["ID"], "7")
        self.assertIn("error", record["tables"][0])
        self.assertNotIn("error", record["tables"][1])
        self.assertEqual(record["tables"][1]["columns"], ["地区", "金额"])

        # 诊断去 stderr：哪张表、哪条记录
        self.assertIn("A2:C5", result["stderr"])
        self.assertNotIn("A2:C5", result["stdout"], "诊断不许出现在 stdout")

    def test_retry_budget_comes_from_config(self):
        # `LLM_MAX_ATTEMPTS=2` ⇒ 第一次坏 JSON 会被**带具体错误重问一次**，
        # 第二次合规 ⇒ 该表成功、整批退出码 0（有界重试真的接在配置上）
        input_path = self._input([GOOD_RECORD])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)
        env = dict(ENV, LLM_MAX_ATTEMPTS="2")

        result = self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B], env=env)

        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(len(result["client"].calls), 2, "两张表各问一次，没有多余重试")
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            record = json.loads(stream.read().splitlines()[0])
        self.assertNotIn("error", record["tables"][0])
        self.assertNotIn("error", record["tables"][1])

        # 反过来：第一次坏 JSON、重问一次还是坏 ⇒ 该表失败、产地照写、退出码非 0
        again = self._input([GOOD_RECORD])
        jsonl_path, md_path = self._products(again)
        out_dir = self._out_dir(again)

        second = self._run([again, "-o", out_dir], [BAD_JSON, BAD_JSON, GOOD_B], env=env)

        self.assertNotEqual(second["code"], 0)
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            record = json.loads(stream.read().splitlines()[0])
        self.assertIn("error", record["tables"][0])
        self.assertNotIn("error", record["tables"][1])

    def test_input_format_error_exits_nonzero_without_writing_products(self):
        """文件级故障（裁决：这条测试名指的是**文件**读不了，不是某条记录坏了）。

        所以判据是**一个产物都不许出现**：`_products` 只算路径、**不**预先创建，跑完再确认
        它们仍然不存在（逐记录隔离走的是另一条路 —— 见 `RecordIsolationTest`，那边产物照写）。
        """
        for label, build in (
            ("文件不存在", self._missing_input),
            ("整体无法解码", lambda: self._bytes_input(b'{"ID": "\xff\xfe\xff"}\n')),
            ("空文件", lambda: self._input([])),
            ("全是空行", lambda: self._input(["", "   ", ""])),
        ):
            with self.subTest(label):
                input_path = build()
                out_dir = self._out_dir(input_path)
                jsonl_path, md_path = self._products(input_path)

                result = self._run([input_path, "-o", out_dir], [GOOD_A])

                self.assertNotEqual(result["code"], 0, label)
                self.assertFalse(os.path.exists(jsonl_path), "文件级故障不许留产物：" + label)
                self.assertFalse(os.path.exists(md_path), label)
                self.assertNotEqual(result["stderr"], "", label)
                self.assertEqual(result["stdout"], "", label)

    def test_missing_config_is_reported_on_stderr_not_stdout(self):
        input_path = self._input([GOOD_RECORD])
        out_dir = self._out_dir(input_path)
        jsonl_path, md_path = self._products(input_path)
        env = {key: value for key, value in ENV.items() if key != "LLM_MODEL"}

        result = self._run([input_path, "-o", out_dir], [GOOD_A], env=env)

        self.assertNotEqual(result["code"], 0)
        self.assertIn("LLM_MODEL", result["stderr"])
        self.assertEqual(result["stdout"], "", "缺配置时 stdout 必须是空的")
        self.assertFalse(os.path.exists(jsonl_path), "配置不全不该有产物")
        self.assertFalse(os.path.exists(md_path))

    def test_diagnostics_go_to_stderr_not_stdout(self):
        # 一条坏记录 + 一张失败的表：两种诊断都在，而且**一条都不许走 stdout**
        bad_line = '{"ID": "9", "FileName": "b.xlsx"'  # 截断的 JSON：解析不了
        input_path = self._input([GOOD_RECORD, "", bad_line])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        result = self._run([input_path, "-o", out_dir], [BAD_JSON, BAD_JSON, GOOD_B])

        # 坏记录留在产物里（原位，不静默丢弃），身份是空串 + 结构化定位
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream.read().splitlines()]
        self.assertEqual(len(records), 2, "坏记录必须留在产物里")
        self.assertEqual(records[0]["ID"], "7")
        self.assertEqual(records[1]["ID"], "")
        self.assertEqual(records[1]["FileName"], "")
        self.assertEqual(records[1]["sheet"], "")
        self.assertEqual(records[1]["tables"], [])
        self.assertEqual(records[1]["source_line"], 3)
        self.assertEqual(records[1]["record_index"], 2)
        self.assertIn("error", records[1])

        # 诊断只在 stderr：坏记录与失败表各有一条线索
        self.assertIn("jsonl 第 3 行", result["stderr"])
        self.assertIn("A2:C5", result["stderr"])
        self.assertGreaterEqual(result["stderr"].count("\n"), 2, result["stderr"])
        self.assertNotIn("jsonl 第 3 行", result["stdout"])

        # 记录级那条诊断里位置**只说一遍**（产物里的 error 自带位置前缀，不再前置一次）。
        # 表级诊断不带记录位置 —— 只认「第 N 条记录（jsonl 第 M 行）」这个短语。
        record_lines = [line for line in result["stderr"].splitlines() if "记录（jsonl" in line]
        self.assertEqual(len(record_lines), 1, result["stderr"])
        self.assertEqual(
            record_lines[0].count("jsonl 第 3 行"), 1, "诊断里位置写了不止一遍：" + record_lines[0]
        )
        # stdout 只有一行摘要
        self.assertLessEqual(len(result["stdout"].splitlines()), 1, result["stdout"])

        # 而且「有坏东西」就是退出码非 0 的理由（不设第二个状态位）
        self.assertNotEqual(result["code"], 0)

        # md 里也要看得见 —— 人看的那份不得静默少一条
        with open(md_path, "r", encoding="utf-8") as stream:
            md = stream.read()
        self.assertIn("整条记录跳过", md)


class RecordIsolationTest(_CliTestCase):
    """Task 6 裁决的接线方式：CLI 自己逐行读、逐条隔离，**不靠** `load_records`。"""

    def test_unparseable_record_still_writes_both_products_and_exits_nonzero(self):
        bad_line = "这压根不是 JSON"
        input_path = self._input([GOOD_RECORD, bad_line, GOOD_RECORD])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        # 两条好记录各问两张表（坏记录不消耗模型调用）
        result = self._run(
            [input_path, "-o", out_dir],
            [GOOD_A, GOOD_B, GOOD_A, GOOD_B],
        )

        self.assertNotEqual(result["code"], 0, "有坏记录就必须非 0 退出")
        self.assertTrue(os.path.exists(jsonl_path), "逐记录隔离 ⇒ 产物照写")
        self.assertTrue(os.path.exists(md_path))

        with open(jsonl_path, "r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream.read().splitlines()]
        self.assertEqual([record["ID"] for record in records], ["7", "", "7"])
        self.assertEqual(records[1]["record_index"], 2)
        self.assertEqual(records[1]["source_line"], 2)
        self.assertEqual(len(result["client"].calls), 4, "坏记录不该消耗模型调用")

        with open(md_path, "r", encoding="utf-8") as stream:
            md = stream.read()
        self.assertIn("A2:C5", md)
        self.assertIn("整条记录跳过", md)

    def test_blank_lines_do_not_advance_the_record_index(self):
        input_path = self._input(["", GOOD_RECORD, "", "", "坏行"])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B])

        with open(jsonl_path, "r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream.read().splitlines()]
        self.assertEqual(records[0]["ID"], "7")
        self.assertEqual(records[1]["record_index"], 2, "空行不推进记录序号")
        self.assertEqual(records[1]["source_line"], 5, "行号是文件里的物理行号")

    def test_bom_is_not_a_broken_first_record(self):
        # `utf-8-sig`（Task 6 裁决）：带 BOM 的第 1 条不该变成坏记录
        input_path = self._input([GOOD_RECORD], bom=True)
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        result = self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B])

        self.assertEqual(result["code"], 0, result["stderr"])
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream.read().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ID"], "7")
        self.assertNotIn("error", records[0])


class ArgumentTest(_CliTestCase):
    """命令行外形与「`main` 只返回退出码、绝不 `SystemExit`」这两条契约。"""

    def test_unknown_argument_is_a_return_value_not_a_system_exit(self):
        # argparse 出错会 `sys.exit(2)`：那对「把 main 当库调」的调用方（冒烟脚本、调度器）
        # 是致命的，所以 main 必须把它的退出码变成返回值
        result = self._run(["whatever.jsonl", "--bogus"], [])

        self.assertNotEqual(result["code"], 0)
        self.assertIn("--bogus", result["stderr"])
        self.assertEqual(result["stdout"], "")

    def test_missing_input_argument_is_a_return_value_too(self):
        result = self._run([], [])

        self.assertNotEqual(result["code"], 0)
        self.assertIn("input", result["stderr"])

    def test_help_still_works_and_writes_usage_to_stdout(self):
        # `-h` 的正文是 argparse 自己印的（英文 `usage:`），走 stdout；这里只钉住
        # 「它不再打死调用方」这一条
        result = self._run(["--help"], [])

        self.assertEqual(result["code"], 0)
        self.assertIn("usage:", result["stdout"])
        self.assertIn("--output-dir", result["stdout"])
        self.assertEqual(result["stderr"], "")

    def test_long_flag_means_the_same_as_short_one(self):
        input_path = self._input([GOOD_RECORD])
        jsonl_path, md_path = self._products(input_path)
        out_dir = self._out_dir(input_path)

        result = self._run([input_path, "--output-dir", out_dir], [GOOD_A, GOOD_B])

        self.assertEqual(result["code"], 0, result["stderr"])
        # 判据是**内容**：`_products` 不预先创建产物，所以「存在」这件事只能由 CLI 做出来；
        # 就算哪天又有人把路径预建出来，下面两条内容断言也仍然要求 CLI 真的写了东西。
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            record = json.loads(stream.read().splitlines()[0])
        self.assertEqual(record["ID"], "7")
        self.assertEqual([t["range"] for t in record["tables"]], ["A2:C5", "E2:F5"])
        with open(md_path, "r", encoding="utf-8") as stream:
            md = stream.read()
        self.assertIn("A2:C5", md)
        # 摘要是 stdout 的**唯一**输出，并且点名两份产物：`--output-dir` 真的被当成 `-o`
        self.assertIn(os.path.abspath(jsonl_path), result["stdout"])
        self.assertIn(os.path.abspath(md_path), result["stdout"])


class NewOutputDirTest(_CliTestCase):
    """`-o` 指向**还不存在**的目录：`cli._write_products` 会 `os.makedirs`。

    README 说 `-o` 是产物目录，没要求调用方先建好它 —— 这是普通用法，之前所有测试都只传
    已存在的目录，`os.makedirs`（`cli.py` 里那一行）因此从没被执行过。
    """

    def test_creates_the_directory_it_was_given_and_writes_both_products_there(self):
        input_path = self._input([GOOD_RECORD])
        out_dir = self._new_output_dir()
        self.assertFalse(os.path.isdir(out_dir), "夹具没给出一个不存在的目录")
        stem = os.path.splitext(os.path.basename(input_path))[0]
        jsonl_path = os.path.join(out_dir, stem + ".out.jsonl")
        md_path = os.path.join(out_dir, stem + ".out.md")
        for path in (jsonl_path, md_path):
            self.addCleanup(self._remove, path)

        result = self._run([input_path, "-o", out_dir], [GOOD_A, GOOD_B])

        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertTrue(os.path.isdir(out_dir), "-o 给的新目录该被建出来")
        self.assertTrue(os.path.exists(jsonl_path), "jsonl 没落到新目录里")
        self.assertTrue(os.path.exists(md_path), "md 没落到新目录里")
        with open(jsonl_path, "r", encoding="utf-8") as stream:
            record = json.loads(stream.read().splitlines()[0])
        self.assertEqual(record["ID"], "7")
        self.assertEqual(record["tables"][0]["columns"], ["产品", "数量", "单价"])


class SubprocessEndToEndTest(_CliTestCase):
    """真子进程：`python -m llm_column_extract`（规格 §8 的运行形态）。

    本文件其余端到端测试都是**同进程**调 `cli.main`，`__main__.py` 那两行与「进程退出码」
    因此没人验过。这里补上，用的还是那条唯一可行的手段：**stdio 重定向到文件**
    （本沙箱里 `PIPE` / `capture_output` 会永久挂住，文件重定向不会），`-X utf8` 让子进程
    的 stdio 是 UTF-8。

    两半合起来才是整条真路径：`-m` 那半走 `__main__.py`（缺输入 ⇒ 退出码 3）；完整成功那半
    用 `-c` 直接调 `cli.main` 并注入假 client —— 本机没装 `langchain-openai`，
    **默认 `client_factory` 造不出真客户端**，所以「导入包 + 跑完整批 + 落盘」这半只能这样走。
    """

    def _run_python(self, args, env_extra=None):
        """起一个真子进程；stdout/stderr 都写进**文件**（绝不用管道）。返回 `(退出码, out, err)`。"""
        stdout_path = self._temp_path("proc.stdout", ".txt")
        stderr_path = self._temp_path("proc.stderr", ".txt")
        env = dict(os.environ)
        # 子进程不继承父进程的 `sys.path`：`src` 让 `-m llm_column_extract` 认得到包，
        # `tests` 让 `-c` 程序认得到 `_fake_client`
        env["PYTHONPATH"] = os.pathsep.join([_SRC, _TESTS_DIR])
        env.update(env_extra or {})
        with open(stdout_path, "w", encoding="utf-8") as out, open(
            stderr_path, "w", encoding="utf-8"
        ) as err:
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", *args],
                cwd=_ROOT,
                env=env,
                stdout=out,
                stderr=err,
                # 别的环境里真挂住 ⇒ 让测试失败，而不是陪着一起挂
                timeout=_SUBPROCESS_TIMEOUT,
            )
        # 读回时用默认的 universal newlines：子进程在 Windows 上写的 `\r\n` 在这里就是 `\n`
        with open(stdout_path, "r", encoding="utf-8") as stream:
            stdout = stream.read()
        with open(stderr_path, "r", encoding="utf-8") as stream:
            stderr = stream.read()
        return completed.returncode, stdout, stderr

    def test_module_entry_point_exits_three_on_a_missing_input(self):
        # 真 `-m`：`__main__.py` 把 `cli.main` 的返回值交给 `sys.exit`
        missing = self._temp_path("cli.e2e.missing", ".jsonl")
        os.remove(missing)

        code, stdout, stderr = self._run_python(
            ["-m", "llm_column_extract", missing], E2E_ENV
        )

        self.assertEqual(code, 3, stderr)
        self.assertEqual(stdout, "", "文件级故障时 stdout 一个字都没有")
        self.assertEqual(
            stderr,
            "llm-column-extract：输入文件不存在：{}\n".format(missing),
            "文件级故障只该有一条 stderr 诊断",
        )

    def test_a_full_run_writes_both_products_named_after_the_input_stem(self):
        input_path = self._input([GOOD_RECORD])
        out_dir = self._out_dir(input_path)
        base = os.path.basename(input_path)
        name = os.path.splitext(base)[0]
        jsonl_path = os.path.join(out_dir, name + ".out.jsonl")
        md_path = os.path.join(out_dir, name + ".out.md")
        # 旧拼法（保留输入的扩展名）的路径：跑完必须**不存在**
        stale = (
            os.path.join(out_dir, base + ".out.jsonl"),
            os.path.join(out_dir, base + ".out.md"),
        )
        for path in (jsonl_path, md_path, *stale):
            self.addCleanup(self._remove, path)

        program = (
            "import sys\n"
            "from llm_column_extract.cli import main\n"
            "from _fake_client import FakeClient\n"
            "sys.exit(main({argv!r}, client_factory=lambda cfg: FakeClient({responses!r})))\n"
        ).format(argv=[input_path, "-o", out_dir], responses=[GOOD_A, GOOD_B])

        code, stdout, stderr = self._run_python(["-c", program], E2E_ENV)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "", "全好时 stderr 必须干净")
        self.assertEqual(os.path.basename(jsonl_path), name + ".out.jsonl")
        self.assertEqual(os.path.basename(md_path), name + ".out.md")
        self.assertTrue(os.path.exists(jsonl_path), "子进程没写出 jsonl")
        self.assertTrue(os.path.exists(md_path), "子进程没写出 md")
        self.assertFalse(os.path.exists(stale[0]), "jsonl 的名字还带着输入的扩展名")
        self.assertFalse(os.path.exists(stale[1]), "md 的名字还带着输入的扩展名")

        # 逐字节读：UTF-8 解得开（中文列名没被转义成 \\uXXXX），且没有 `\r`
        with open(jsonl_path, "rb") as stream:
            raw = stream.read()
        self.assertNotIn(b"\r", raw, "jsonl 必须用 newline='\\n' 写")
        text = raw.decode("utf-8")
        self.assertIn("产品", text)
        self.assertEqual(json.loads(text.splitlines()[0])["ID"], "7")
        with open(md_path, "rb") as stream:
            md_raw = stream.read()
        self.assertNotIn(b"\r", md_raw, "md 必须用 newline='\\n' 写")
        self.assertIn("A2:C5", md_raw.decode("utf-8"))

        # stdout 只有摘要那一行，点名两份产物
        self.assertEqual(len(stdout.splitlines()), 1, stdout)
        self.assertIn(os.path.abspath(jsonl_path), stdout)


class ProductNamingTest(unittest.TestCase):
    """两份产物的**名字**（规格 §8）：`<name>.out.jsonl` + `<name>.out.md`。

    `<name>` 是输入文件名的 **stem**（去掉最后一个扩展名）：`batch.jsonl` ⇒
    `batch.out.jsonl` / `batch.out.md`。两份产物同属一次运行、共享同一个 `<name>`。

    这条单独测，因为它是本文件其余测试的夹具前提：其余测试用 `mkstemp` 造输入、再按
    `_product_paths` 算产物路径，一旦拼法变了，它们会静默地对着一个 CLI 从没碰过的路径断言。
    """

    def test_name_is_the_input_stem_and_default_dir_is_cwd(self):
        self.assertEqual(
            _product_paths("/data/batch.jsonl", None),
            (os.path.join(os.curdir, "batch.out.jsonl"),
             os.path.join(os.curdir, "batch.out.md")),
        )

    def test_explicit_output_dir(self):
        self.assertEqual(
            _product_paths("/data/batch.jsonl", "/out"),
            (os.path.join("/out", "batch.out.jsonl"),
             os.path.join("/out", "batch.out.md")),
        )

    def test_input_without_extension_keeps_its_name(self):
        # 没有扩展名的输入（`ocr/batch`）与 `batch.jsonl` 落到同一个 `<name>`
        self.assertEqual(
            _product_paths("/data/ocr/batch", "/out"),
            (os.path.join("/out", "batch.out.jsonl"),
             os.path.join("/out", "batch.out.md")),
        )


class ConfigTest(unittest.TestCase):
    """`load_config(env) -> Config`（规格 §8 的配置项）。纯函数，不碰进程环境。"""

    def test_reads_every_item_from_the_mapping(self):
        config = load_config(ENV)
        self.assertEqual(config.model, "deepseek-v4")
        self.assertEqual(config.base_url, "http://gateway.internal/v1")
        self.assertEqual(config.api_key, "sk-test")
        self.assertEqual(config.timeout, 30.0)
        self.assertEqual(config.max_attempts, 1)

    def test_optional_items_have_defaults(self):
        config = load_config({"LLM_MODEL": "m", "LLM_BASE_URL": "http://x/v1"})
        # 缺省值是文档的一部分（README 的配置表、`config.DEFAULT_*`）：钉具体数字，
        # 只写「> 0」的话有人把缺省改成 1 秒或 1 次也照样绿
        self.assertEqual(config.timeout, 60.0, "缺省超时 = 60 秒")
        self.assertEqual(config.max_attempts, 2, "缺省重试上限 = 2（第一次 + 重问一次）")
        self.assertIsInstance(config.timeout, float)
        self.assertIsInstance(config.max_attempts, int)

    def test_missing_required_items_name_the_variable(self):
        # 异常类型也要钉：`assertRaises(Exception)` 对**任何**异常都通过（包括 AttributeError
        # 这种「实现其实写错了」的形态），下一点名变量的断言才是有效证据
        with self.assertRaises(ConfigError) as caught:
            load_config({"LLM_BASE_URL": "http://x/v1"})
        self.assertIn("LLM_MODEL", str(caught.exception))

        with self.assertRaises(ConfigError) as caught:
            load_config({"LLM_MODEL": "m"})
        self.assertIn("LLM_BASE_URL", str(caught.exception))

    def test_bad_numbers_are_reported_by_name(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(dict(ENV, LLM_TIMEOUT="半分钟"))
        self.assertIn("LLM_TIMEOUT", str(caught.exception))

        with self.assertRaises(ConfigError) as caught:
            load_config(dict(ENV, LLM_MAX_ATTEMPTS="0"))
        self.assertIn("LLM_MAX_ATTEMPTS", str(caught.exception))

        # 不是有限数的超时值：`float("nan")` 与任何数比较都是 False，只查下界会放它过去
        for bad in ("nan", "inf"):
            with self.subTest(bad):
                with self.assertRaises(ConfigError) as caught:
                    load_config(dict(ENV, LLM_TIMEOUT=bad))
                self.assertIn("LLM_TIMEOUT", str(caught.exception))

        # 次数是循环次数：小数在这里是写错，不接受
        with self.assertRaises(ConfigError) as caught:
            load_config(dict(ENV, LLM_MAX_ATTEMPTS="2.5"))
        self.assertIn("LLM_MAX_ATTEMPTS", str(caught.exception))

        # 超时可以给小数
        self.assertEqual(load_config(dict(ENV, LLM_TIMEOUT="30.5")).timeout, 30.5)

    def test_zero_timeout_is_accepted_and_stays_zero_in_config(self):
        # `Config.timeout` 的**含义**不变：0 是文档里「不设超时」的哨兵，不是别的数
        self.assertEqual(load_config(dict(ENV, LLM_TIMEOUT="0")).timeout, 0.0)


class ClientTimeoutTest(unittest.TestCase):
    """`LLM_TIMEOUT=0` 的承诺：**不设超时**（README 的配置表、`config._timeout`）。

    映射只发生在**造客户端那一刻**：`0` ⇒ `None`。`timeout=0` 交给 httpx（OpenAI SDK 的
    传输层）是**零秒**超时 —— 连接/读/写/连接池全部立刻失败，跟着 README 配的运维会得到
    「500 条里每一次调用都失败」。httpx 真正表示不设超时的是 `None`。

    本机没装 `langchain-openai`，真 `LangChainClient.__init__` 在 import 那一步就抛 ——
    所以这里把 `cli.LangChainClient` 换成记账的替身，看**默认 factory** 到底递进去了什么。
    """

    class _Recorder:
        """冒充 `cli.LangChainClient`：只记下被传进来的关键字参数。"""

        seen: dict | None = None

        def __init__(self, **kwargs) -> None:
            type(self).seen = kwargs

    def _handed_to_the_client(self, raw_timeout):
        """走**默认** `client_factory`（`client_factory=None`）造一次客户端，返回记账字典。"""
        config = load_config(dict(ENV, LLM_TIMEOUT=raw_timeout))
        with mock.patch.object(cli, "LangChainClient", self._Recorder):
            client = cli._make_client(config, None)
        self.assertIsInstance(client, self._Recorder, "默认 factory 没走到 LangChainClient")
        return self._Recorder.seen

    def test_zero_reaches_the_client_as_none(self):
        seen = self._handed_to_the_client("0")

        self.assertIsNone(
            seen["timeout"],
            "0 必须映射成 None（不设超时）；0 秒、或某个大数字都不是文档的承诺",
        )
        # 其余参数照原样转发（顺带钉住默认 factory 的接线）
        self.assertEqual(seen["model"], "deepseek-v4")
        self.assertEqual(seen["base_url"], "http://gateway.internal/v1")
        self.assertEqual(seen["api_key"], "sk-test")

    def test_a_positive_timeout_reaches_the_client_as_the_number(self):
        self.assertEqual(self._handed_to_the_client("30.5")["timeout"], 30.5)

    def test_the_mapping_helper_is_directly_testable(self):
        # 映射是**具名**的，所以可以单独钉；0 之外的每个值原样通过
        self.assertIsNone(cli._client_timeout(0))
        self.assertEqual(cli._client_timeout(0.5), 0.5)
        self.assertEqual(cli._client_timeout(60.0), 60.0)


if __name__ == "__main__":
    unittest.main()
