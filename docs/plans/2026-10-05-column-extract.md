# 从 OCR jsonl 提取列名的 LLM 组件 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 读入一份 jsonl（每条记录 = 一个「文件 + 工作表」），用 LLM 判定每张表的标题行区并产出列名路径，写出 jsonl（给机器）与 md（给人）两份产物。

**Architecture:** 逐记录 → 逐表 → 每表一次调用。**代码只做 I/O、提示词渲染、兜底解析、结构校验、汇总**；判定标题行区这件事完全在提示词里。模型只吐 JSON，md 由代码从 jsonl 渲染，因此两份产物不可能互相矛盾。模型调用被一个**假 client** 隔离，使「逐表隔离、有界重试、部分成功」全部可测。

**Tech Stack:** Python 3.8+（用 `from __future__ import annotations`）、LangChain（`ChatOpenAI` + 自定义 `base_url`）、标准库 `unittest`、无 pytest。

**Spec:** `llm-column-extract/docs/specs/2026-10-05-column-extract-design.md` —— 计划从规格论证而来，**执行者两份都要读**。

## Global Constraints

- **术语**（规格 §3，权威是 `excel-to-md/GLOSSARY.md`）：代码、产物、文档一律用「**标题行区**」「数据区」「表范围」。**不得使用「表头」「标题」**。「列名行」「分组表头」**只允许出现在提示词文本里**，且必须同时给出权威表述「标题行区的最下面一行」。
- **`columns` 的元素必须是字符串**。写成对象会被 `str()` 悄悄毁掉（规格 §11.2）。
- **不依赖网关的结构化输出能力**（`response_format`）。**兜底解析必写**：剥 markdown 围栏、容忍前后杂字、JSON 坏了要报错而不是猜（规格 §8）。
- `columns` 的元素是**路径**，各层用 `" / "` 拼接；单层时即该值本身（规格 §6）。
- 没有标题行区的表：**`columns: []`，字段必须在**（省掉会被判成格式错误）。
- **逐表隔离**：单表失败不中断；该记录照样写出，失败的表带标记。整批跑完**退出码非 0 但产物照写**（规格 §9）。
- **串行执行**，不做并发（等有实测数据再考虑）。
- **模型调用不进单元测试**；一律经假 client（规格 §10）。
- 运行测试：`PYTHONPATH=src python -m unittest discover -s tests -v`，**必须在项目根目录 `llm-column-extract/` 下运行**。

## Review Focus

规格暗示、但没有哪个任务的测试能覆盖的失效模式。**每条都在其归属任务里有对应测试**（第 5 条除外，它测不了，见说明）。

1. **伙伴的 jsonl 里某列整列为空** ⇒ 该表应标记失败，而不是产出一个空路径 —— Task 4。
2. **模型的 `header_rows` 覆盖整个表**（等于没有数据区）⇒ 应判非法并重问，不产出 —— Task 4。
3. **模型返回合法 JSON 但不是对象**（数组、字符串、数字）⇒ 应判失败，不许崩 —— Task 4。
4. **输入 jsonl 里某列比表范围声明的高度短**（缺行）⇒ 应明确报错，不静默按短的算 —— Task 2。
5. **模型把标题行当成标题行区的最下面一行** ⇒ 路径里会混进标题行的文字。**我们无法自动发现**（规格 §12.1 已接受的代价）。本条**不写测试**，只保证 `reason` 必填并可断言 —— Task 4 的 `test_reason_is_preserved`。

---

### Task 1: 输出契约与 md 渲染

**Files:**
- Create: `src/llm_column_extract/__init__.py`（空）
- Create: `src/llm_column_extract/output.py`
- Create: `src/llm_column_extract/render.py`
- Test: `tests/test_output.py`, `tests/test_render.py`

**Interfaces:**
- Produces:
  - `dataclass TableResult(range: str, columns: tuple[str, ...], header_rows: tuple[int, int] | None, reason: str, error: str | None = None)` —— **没有 `failed` 字段**：失败与否由 `error is not None` 决定，不设第二个会与它漂移的状态位
  - `build_record(record_id: str, file_name: str, sheet: str, tables: Sequence[TableResult], *, model: str, prompt_version: str, attempts: int) -> dict`
  - `render_markdown(records: Sequence[dict], meta: dict) -> str`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_output.py
class BuildRecordTest(unittest.TestCase):
    def test_tables_are_nested_not_flattened(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("产品", "数量"), (2, 2), "第 2 行是标题行区"),
            TableResult("E2:F4", ("地区", "金额"), (2, 2), "第 2 行是标题行区"),
        ], model="m", prompt_version="v1", attempts=1)
        self.assertEqual([t["range"] for t in doc["tables"]], ["A2:C4", "E2:F4"])

    def test_no_header_table_keeps_empty_columns_field(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "本表没有标题行区")],
            model="m", prompt_version="v1", attempts=1)
        self.assertIn("columns", doc["tables"][0])          # 字段必须在
        self.assertEqual(doc["tables"][0]["columns"], [])   # 必须是空数组

    def test_failed_table_is_written_with_its_error(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "", error="列数不符")],
            model="m", prompt_version="v1", attempts=2)
        self.assertNotIn("failed", doc["tables"][0])   # 不设第二个状态位
        self.assertEqual(doc["tables"][0]["error"], "列数不符")

    def test_paths_are_plain_strings(self):
        doc = build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("2024 年度 / Q1",), (2, 3), "r")],
            model="m", prompt_version="v1", attempts=1)
        self.assertIsInstance(doc["tables"][0]["columns"][0], str)
```

```python
# tests/test_render.py
class RenderTest(unittest.TestCase):
    def test_markdown_lists_every_table_with_its_range(self):
        md = render_markdown([build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", ("产品", "数量"), (2, 2), "第 2 行")],
            model="m", prompt_version="v1", attempts=1)], {"input": "in.jsonl"})
        self.assertIn("A2:C4", md)
        self.assertIn("产品", md)

    def test_markdown_shows_failed_tables_as_failed(self):
        md = render_markdown([build_record("1", "x.xlsx", "S", [
            TableResult("A2:C4", (), None, "", error="列数不符")],
            model="m", prompt_version="v1", attempts=1)], {"input": "in.jsonl"})
        self.assertIn("列数不符", md)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_output.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'llm_column_extract.output'`

- [ ] **Step 3: 实现 `output.py` 与 `render.py`**

`output.py`：`TableResult` 是 frozen dataclass。`build_record` 返回
`{"ID", "FileName", "sheet", "tables": [{"range", "columns": list, "header_rows": list|None, "reason", "error"}], "model", "prompt_version", "attempts"}`。
`columns` 与 `header_rows` 从 tuple 转 list（JSON 友好）。**`error` 为 `None` 时不写该键**（成功就不带噪声）。

`render.py`：`render_markdown` 逐记录输出 `## <FileName> / <sheet>`，
每张表一段含 `range`、`columns` 列表、`reason`；失败的标出 `error`。**纯字符串拼接，不引依赖。**

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_output.py" -v` 与 `-p "test_render.py"`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract tests/test_output.py tests/test_render.py
git commit -m "feat(llm-column-extract): 输出契约与 md 渲染"
```

---

### Task 2: 输入读取

**Files:**
- Create: `src/llm_column_extract/input.py`
- Test: `tests/test_input.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class InputFormatError(Exception)`
  - `dataclass InputTable(range: str, columns: tuple[tuple[str, ...], ...])` —— `columns` 是**每列的单元格内容**，按行序
  - `dataclass InputRecord(record_id: str, file_name: str, sheet: str, tables: tuple[InputTable, ...])`
  - `load_records(path: str) -> list[InputRecord]`

**说明：字段是「袋子」。** 规格 §5.1：契约未冻结，**不得假设字段名**。识别规则：
- `ID` / `id` ⇒ `record_id`；`FileName` / `filename` / `file` ⇒ `file_name`；`sheet` / `Sheet` ⇒ `sheet`
- 表：任何含**范围样式的字符串**（正则 `^\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+$`）的键，或含表对象的数组
- 每列单元格：该表对象下的数组字段（元素是字符串或 `{"value": ...}`）
- **认不出来 ⇒ 抛 `InputFormatError`，指出第几条记录、看到了哪些键。不猜、不静默跳过。**

- [ ] **Step 1: 写失败的测试**

```python
class LoadRecordsTest(unittest.TestCase):
    def test_reads_columns_as_per_column_cell_lists(self):      # 主路径
        # 写一份最小 jsonl，断言 tables[0].columns == (("2024 年度","产品","北区"), ("...","Q1","10"))
    def test_missing_sheet_field_raises_with_record_index(self):
        # 缺 sheet ⇒ InputFormatError，消息含记录序号与看到的键
    def test_unrecognized_shape_raises_instead_of_silently_skipping(self):
    def test_column_shorter_than_declared_range_height_raises(self):
        # Review Focus #4：某列比表范围声明的高度短 ⇒ 明确报错
    def test_missing_file_raises(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_input.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'llm_column_extract.input'`

- [ ] **Step 3: 实现 `input.py`**

用 `range` 字符串算出**声明高度**（`A2:C4` ⇒ 3 行），与每列 len 比对；不齐 ⇒ `InputFormatError`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_input.py" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract/input.py tests/test_input.py
git commit -m "feat(llm-column-extract): 输入 jsonl 读取，按字段不定的袋子处理"
```

---

### Task 3: 提示词渲染

**Files:**
- Create: `src/llm_column_extract/prompt.py`
- Test: `tests/test_prompt.py`

**Interfaces:**
- Consumes: `InputTable`
- Produces:
  - `PROMPT_VERSION = "v1"`
  - `SYSTEM_PROMPT: str`
  - `render_user_message(table: InputTable) -> str`
  - `build_messages(table: InputTable) -> list[dict]` —— `[{"role": "system", ...}, {"role": "user", ...}]`

**这是全组件唯一由规格逐字钉死文本的地方，因此本任务带文案。** 规格 §7。

- [ ] **Step 1: 写失败的测试**

```python
class PromptTest(unittest.TestCase):
    def test_row_numbers_start_at_one_and_are_labelled_as_in_table(self):
        msg = render_user_message(InputTable("A2:C4", (("产品","A","B"),)))
        self.assertIn("行1", msg)          # 1-based，且明确是「表内」
        self.assertNotIn("行0", msg)

    def test_authoritative_term_is_defined_inline(self):
        self.assertIn("标题行区", SYSTEM_PROMPT)
        # 权威表述必须与局部说明同时出现（规格 §7.1）
        self.assertIn("最下面", SYSTEM_PROMPT)

    def test_forbidden_terms_never_appear_alone(self):
        # 「表头」不得出现在 SYSTEM_PROMPT 里（「分组表头」允许）
        self.assertNotIn("表头", SYSTEM_PROMPT.replace("分组表头", ""))

    def test_no_header_exit_is_offered(self):
        self.assertIn("header_rows", SYSTEM_PROMPT)
        self.assertIn("null", SYSTEM_PROMPT)

    def test_reason_is_required(self):
        self.assertIn("reason", SYSTEM_PROMPT)

    def test_all_four_few_shots_present(self):
        for marker in ("单层", "分组表头", "标题行", "没有标题行区"):
            self.assertIn(marker, SYSTEM_PROMPT)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_prompt.py" -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `prompt.py`**

`SYSTEM_PROMPT` 必须按序包含规格 §7 的六块内容：

1. **术语**（四个词，可判定的定义，不是形容词）：
   - **标题行**：只出现一次、横跨整表宽度、描述整张表的文字。**不属于标题行区，要排除**。
   - **标题行区**：描述各列含义、位于数据之上的**连续行块**（一行或多行）。
   - **列名行**：标题行区里**最下面**那一行。`columns` 的取值来自它，`path` 来自标题行区各层。
   - **分组表头**：标题行区里列名行之上的行。**拼进 `path`**。
2. **硬约束**（规格 §7.2，逐条抄）：只输出 JSON、不要解释、不要 markdown 围栏；`columns`
   每项是路径，各层用 `" / "` 拼接；路径每段**必须照抄原文单元格**，不许改写/翻译/补全/纠错/去空格；
   空单元格跳过；没有标题行区时输出 `{"header_rows": null, "columns": [], "reason": "..."}`；**必须给 reason**。
3. **「没有标题行区」这条路**：既给规则又给负例（规格 §7.3）。
4. **四个 few-shot**：单层／两层含分组表头（横幅**重复取值**）／标题行+列名行／没有标题行区。
5. **不给的东西**（在代码里体现，不在提示词里）：md、我们的推断、伙伴的其它字段。

`render_user_message` 渲染表范围与逐行网格，**行号 1-based 并写明「表内」**：
```
表范围 A2:C4（由上游框定）
下面按表内行号列出；「行1」是本表的第一行

行1 | 产品 | 数量 | 单价
行2 | A    | 1    | 0.25
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_prompt.py" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract/prompt.py tests/test_prompt.py
git commit -m "feat(llm-column-extract): 提示词渲染（术语、硬约束、四个 few-shot）"
```

---

### Task 4: 模型输出解析与校验 ★ 测试主力

**Files:**
- Create: `src/llm_column_extract/model_out.py`
- Test: `tests/test_model_out.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ModelOutputError(Exception)`
  - `dataclass ModelAnswer(header_rows: tuple[int, int] | None, columns: tuple[str, ...], reason: str)`
  - `parse_model_output(raw: str, *, n_columns: int, n_rows: int) -> ModelAnswer`

**规格 §6.1 的四条校验全在这里。** 这是唯一能抓住弱模型的地方，测试要密。

- [ ] **Step 1: 写失败的测试**

```python
class ParseModelOutputTest(unittest.TestCase):
    # --- 正常路径 ---
    def test_plain_json(self):
    def test_strips_markdown_fence(self):
    def test_tolerates_leading_and_trailing_prose(self):
    def test_reason_is_preserved(self):        # Review Focus #5 的替代断言

    # --- 校验：规格 §6.1 ---
    def test_header_rows_out_of_range_raises(self):        # a<1 或 b>n_rows
    def test_header_rows_not_a_pair_raises(self):          # [2] / [1,2,3]
    def test_header_rows_covering_every_row_raises(self):  # Review Focus #2
    def test_null_header_rows_requires_empty_columns(self):# 自洽性
    def test_empty_columns_requires_null_header_rows(self):
    def test_column_count_must_match_input(self):          # 漏列/多列
    def test_empty_string_column_raises(self):
    def test_all_empty_column_path_raises(self):           # Review Focus #1

    # --- 坏输入不许崩 ---
    def test_non_object_json_raises(self):                 # Review Focus #3
    def test_broken_json_raises(self):
    def test_missing_reason_raises(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_model_out.py" -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `model_out.py`**

解析顺序：① 剥 markdown 围栏（```json ... ```）② 取第一个 `{` 到最后一个 `}` ③ `json.loads`
④ 形状校验，**任一条不合规就抛 `ModelOutputError`，消息里带上具体原因**（供重问时回传）。

`empty_string_column` 与 `all_empty_column_path` 都指**某一列的路径为空字符串** —— 那说明
模型没判出这一列，**宁可失败也不写空列名**。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_model_out.py" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract/model_out.py tests/test_model_out.py
git commit -m "feat(llm-column-extract): 模型输出兜底解析与结构校验"
```

---

### Task 5: 调用与有界重试

**Files:**
- Create: `src/llm_column_extract/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `parse_model_output`、`ModelOutputError`、`build_messages`
- Produces:
  - `class ModelClient(Protocol): def complete(self, messages: list[dict]) -> str: ...`
  - `class LangChainClient: def __init__(self, *, model: str, base_url: str, api_key: str, timeout: float) -> None`
  - `class FakeClient: def __init__(self, responses: Sequence[str | Exception]) -> None`
  - `ask_table(client: ModelClient, table: InputTable, *, max_attempts: int = 2) -> ModelAnswer`

- [ ] **Step 1: 写失败的测试**

```python
class AskTableTest(unittest.TestCase):
    def test_returns_answer_on_first_success(self):
    def test_retries_once_on_bad_output_and_passes_the_error_back(self):
        # 第二次的 messages 里必须出现第一次的错误说明（否则重问没意义）
    def test_gives_up_after_max_attempts(self):   # 抛 ModelOutputError
    def test_transport_error_is_retried(self):    # FakeClient 抛 TimeoutError
    def test_transport_error_gives_up_bounded(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_client.py" -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `client.py`**

`LangChainClient.complete` 用 `langchain_openai.ChatOpenAI(model=..., base_url=..., api_key=..., timeout=...)`
并 `invoke(messages)`，返回 `.content`。**不传 `response_format`**（规格 §8：不依赖网关的结构化输出能力）。

`ask_table` 的循环：失败时把 `f"上一次的回答不合规：{exc}。请只输出 JSON。"` 追加进 messages。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_client.py" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract/client.py tests/test_client.py
git commit -m "feat(llm-column-extract): 模型调用与有界重试，含测试替身"
```

---

### Task 6: 编排（逐记录、逐表隔离、部分成功）

**Files:**
- Create: `src/llm_column_extract/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `InputRecord`、`ask_table`、`TableResult`、`build_record`
- Produces: `run_pipeline(records: Sequence[InputRecord], client: ModelClient, *, model: str, max_attempts: int = 2) -> list[dict]`

**规格 §9 的全部行为都在这里。**

- [ ] **Step 1: 写失败的测试**

```python
class PipelineTest(unittest.TestCase):
    def test_happy_path_one_record_two_tables(self):
    def test_one_failed_table_does_not_lose_the_other(self):
        # 第一张表永远返回坏 JSON ⇒ 该表 failed；第二张表照常 match
    def test_record_is_still_written_when_a_table_fails(self):
    def test_whole_batch_continues_after_a_record_fails(self):
        # 3 条记录，第 2 条的输入不可解析 ⇒ 第 1、3 条仍有结果
    def test_failed_table_carries_its_error_text(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_pipeline.py" -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `pipeline.py`**

逐表 `try/except`（捕获 `ModelOutputError` 与传输异常），失败时构造
`TableResult(range, (), None, "", error=str(exc))` 并**继续**。**不写空列名。**

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_pipeline.py" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract/pipeline.py tests/test_pipeline.py
git commit -m "feat(llm-column-extract): 编排与逐表隔离，部分成功仍写产物"
```

---

### Task 7: CLI、配置与端到端

**Files:**
- Create: `src/llm_column_extract/config.py`
- Create: `src/llm_column_extract/cli.py`
- Create: `src/llm_column_extract/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_records`、`run_pipeline`、`build_record`、`render_markdown`、`LangChainClient`
- Produces:
  - `dataclass Config(model: str, base_url: str, api_key: str, timeout: float, max_attempts: int)`
  - `load_config(env: Mapping[str, str]) -> Config`
  - `main(argv: list[str] | None = None, *, client_factory: Callable[[Config], ModelClient] | None = None) -> int`

**`client_factory` 是端到端测试的注入点** —— 有了它，整条 CLI 路径可以在**无网络、无模型**下测通。

- [ ] **Step 1: 写失败的测试**

```python
class CliTest(unittest.TestCase):
    def _run(self, args, responses):
        # 用 client_factory=lambda cfg: FakeClient(responses) 调 main
    def test_writes_both_jsonl_and_markdown(self):
    def test_exit_code_is_zero_when_everything_succeeds(self):
    def test_exit_code_is_nonzero_when_a_table_failed_but_products_are_still_written(self):
        # 规格 §9 最后一行：部分成功要留下
    def test_input_format_error_exits_nonzero_without_writing_products(self):
    def test_missing_config_is_reported_on_stderr_not_stdout(self):
    def test_diagnostics_go_to_stderr_not_stdout(self):
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_cli.py" -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现 `config.py`、`cli.py`、`__main__.py`**

CLI 形状：`python -m llm_column_extract <input.jsonl> [-o <dir>]`。
产物：`<basename>.out.jsonl` + `<basename>.out.md`，**UTF-8、`newline="\n"`**。
配置从环境变量读（`LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_TIMEOUT`、
`LLM_MAX_ATTEMPTS`），**缺必需项时在 stderr 报错并返回非 0**。

- [ ] **Step 4: 跑测试确认通过，并跑全量**

Run: `PYTHONPATH=src python -m unittest discover -s tests -p "test_cli.py" -v`
Expected: PASS

Run: `PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: PASS（全量）

- [ ] **Step 5: 提交**

```bash
git add src/llm_column_extract tests/test_cli.py
git commit -m "feat(llm-column-extract): CLI、配置与端到端（假 client 注入）"
```

---

## 计划自检

**1. 规格覆盖**

| 规格节 | 落在哪个任务 |
|---|---|
| §3 术语 | Task 3（提示词）+ Global Constraints |
| §4 职责边界 | Task 1/3/6 合起来 |
| §5 输入契约（袋子） | Task 2 |
| §6 模型输出契约 | Task 3（要求）+ Task 4（校验） |
| §6.1 四条校验 | Task 4 |
| §7 提示词设计 | Task 3 |
| §8 运行形态与配置 | Task 7 |
| §9 错误处理 | Task 6（隔离）+ Task 7（退出码） |
| §10 测试策略 | Global Constraints + Task 5（假 client） |
| §11 产出契约 | Task 1 |
| §12.1 已知取舍 | Review Focus #5 记录在案 |

**2. 步骤扫描**：每个「代码」步骤只给签名、文件与规格钉死的值，函数体留给实现者；
唯一的例外是 Task 3 的 `SYSTEM_PROMPT` —— 那是**规格逐字钉死的文案**，不是我能决定得更好的东西。
无「TBD」「处理边界情况」这类不决定任何事的行。

**3. 类型一致性**：`InputTable`/`InputRecord`（Task 2）→ `build_messages`（Task 3）→
`ModelAnswer`（Task 4）→ `ask_table`（Task 5）→ `TableResult`/`build_record`（Task 1）→
`run_pipeline`（Task 6）→ `main`（Task 7）。**`TableResult` 在 Task 1 定义、Task 6 使用** —— 已核对字段名一致。

**4. Review Focus**：五条，其中四条在 Task 4 / Task 2 有测试；第五条（标题行被判成列名）
**测不了**（规格 §12.1 明确接受），改为断言 `reason` 必填并被保留。

**5. 比例**：**实测**本计划 **536 行**，规格 249 行 ⇒ **2.15 倍**。

> 我原先在这里写的是「约 330 行、1.3 倍」——**那是估的，量出来是错的**（本会话第四次我报了
> 没量过的数）。已按「先量再说」的规矩换成实测值。
>
> 2.15 倍**没有**退化成逐行抄写：全文绝大多数行是**测试名与断言**、**接口签名**与**规格钉死的值**；
> 函数体只出现在 Task 3（`SYSTEM_PROMPT`，规格逐字钉死的文案）。
>
> 但这个倍数**高于健康区间**。实施时若发现某一步仍不够明确，**优先拆细步骤，而不是往计划里补代码**。
