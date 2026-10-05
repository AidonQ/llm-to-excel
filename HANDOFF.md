# 交接：llm-column-extract（给新会话）

> 本文件是**跨会话交接**，不是规格也不是契约。工作真正开始后即可删除。
> 权威顺序：规格 > 计划 > 本文件。

## 一句话状态

**设计已批准、计划已写好、实施一行未动。** 卡在一个决定上：分支怎么处理。

## 三个要读的文件（权威顺序）

| 角色 | 路径 |
|---|---|
| **规格（权威）** | `llm-column-extract/docs/specs/2026-10-05-column-extract-design.md`（249 行，已批准） |
| **实施计划** | `llm-column-extract/docs/plans/2026-10-05-column-extract.md`（543 行，7 任务 / 35 步） |
| **执行 ledger** | `.superpowers/sdd/2026-10-05-column-extract/progress.md` |
| 设计过程记录 | `llm-column-extract/README.md` |

## 待人类伙伴决定的两件事（都还没有答案）

1. **分支**：当前在 `master`，工作区干净。`executing-plans` 规定**不得在 master 上开工，除非明确同意**。
   建议开 `feat/llm-column-extract`（沿用本项目既有做法）。
2. **执行方式**：Native（内联，最后一份终审）还是 Subagent-driven（每任务一份全新上下文 + 独立复核）。
   **上一个会话推荐 Subagent-driven**，理由是计划有 7 个任务、链式接口，内联会让后面的任务拿到
   被压缩过的上下文。

## 这个组件是什么

读入一份 jsonl（每条记录 = 一个「文件 + 工作表」），用 LLM 从 OCR 产出的文本里**判定标题行区**并
产出**列名路径**，写出 **jsonl（给机器）+ md（给人）** 两份产物。

**职责边界（这条最重要）**：

| 谁 | 做什么 |
|---|---|
| 上游（伙伴的 OCR） | 框定**表范围** + **全量读取**每列的单元格内容。**不判标题行区** |
| 本组件代码 | 读 jsonl → 渲染提示词 → 调模型 → 兜底解析 → **结构校验** → 写 jsonl + 渲染 md |
| 模型 | **判定标题行区**（存在与否、起止行号），产出每列的**路径**，并给 `reason` |

## 必须遵守的裁决（违反即为偏离规格）

- **术语**：权威是 `excel-to-md/GLOSSARY.md`。代码、产物、文档一律用「**标题行区**」「数据区」
  「表范围」。**不得使用「表头」「标题」**。「列名行」「分组表头」**只允许出现在提示词文本里**，
  且必须同时给出权威表述「标题行区的最下面一行」。
- **`columns` 的元素是「路径」**（如 `2024 年度 / Q1`，各层用 `" / "` 拼接），不是最下层的短名。
  单层时两者相同。
- **`columns` 的元素必须是字符串。** 写成对象会被 `excel-to-md` 的 `load_claimed` 用 `str()` 悄悄
  毁成一坨，**而且不报错**。
- **模型只吐 JSON，md 由代码从 jsonl 渲染** —— 两份不可能互相矛盾。
- **列名由模型写出**（「纯提示词」，人类伙伴明确选择）⇒ 本组件**无法自动判断列名是不是编的**。
  这是**明确接受的取舍**，不是疏漏。
- **不依赖网关的结构化输出能力**；兜底解析必写。模型名 / `base_url` / 密钥 / 超时 / 重试上限
  全是配置项。**继续使用 LangChain**（审计建议换成标准库 `urllib.request`，人类伙伴未采纳）。
- 没有标题行区的表：**`columns: []`，字段必须在**。
- **逐表隔离**：单表失败不中断；该记录照样写出并带 `error`。整批跑完**退出码非 0 但产物照写**。
- **串行执行**，不做并发。
- **模型调用不进单元测试**；用 `FakeClient` 与 `client_factory` 注入。

## 本项目已完成的另一部分（**勿动**）

`excel-to-md/`：列名验证子系统已 6 任务完成、合并、推送。

- **146 个测试全绿**（原 86 + 新增 60），`master` 上有全部历史
- 已通过 `git subtree split --prefix=excel-to-md` + 邮箱重写 + **快进**推送发布到
  GitHub `AidonQ/excel-to-md` 的 `main`（当前 `b310e76`）
- 独立终审查出 **1 Critical + 6 Important + 5 Minor**，已修 F1/F6/F2/F7/F9；
  **仍未修 7 条**（F3 `tables[]` 形式下 `hallucinated` 不可达；F4 覆盖率分母；F5 缺逐表异常隔离；
  F8 失败运行留旧产物；F10 工作表名大小写；F11 记录序号 0-based；F12 `normalized` 过度上报）。
  复现证据在 `excel-to-md/_review/`（已 gitignore），决策记录在
  `.superpowers/sdd/2026-10-05-column-name-validation/progress.md`。
- **`llm-column-extract/` 与它客观解耦**：放在工作区根目录、与 `excel-to-md/` 平级，
  因此**不会**被发布子树带上 GitHub。每次提交都验过这一点。

## 环境事实（踩过的坑，别再踩）

- **Python**：`C:\Users\huawei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
- **测试**：`excel-to-md` 用 `PYTHONPATH=src python -m unittest discover -s tests -v`，
  **必须在项目根目录下跑**（测试用相对路径 fixtures）。**没有 pytest。**
  `llm-column-extract` 同理，CWD 为 `llm-column-extract/`。
- **沙箱**：受限模式下 MSYS shell 被挡（`git subtree`、`filter-branch`、`scripts/sdd-workspace`
  都会 `Server execution failed`），**需要 `sandbox_permissions: "danger-full-access"`**。
  TLS 出网同样需要它。
- `excel-to-md/tests/_cli_tmp`、`_validate_tmp` 里的 `mkdtemp` 目录是 0o700，
  对它做 `Get-ChildItem -Recurse` 会刷一屏权限错误，**与审计无关**。
- **PowerShell 5.1**：写中文内容时**不要**在双引号字符串里放 `"` 或 `$`
  （`$A` 会被当成变量、`\"` 不是转义）——我已因此让整条命令失败过两次，什么都没提交。
- `Get-Content` 把 UTF-8 当 GBK 解，**中文显示是乱码**。要读文本用 `read` 工具，不要用 shell。

## 这个项目已经形成的纪律（请沿用）

1. **量了再说。** 任何行数、比例、增删量，**先测再说**。上一个会话里我报了**四次没量过的数**
   （v1 计划「1.3 倍」实为 2.07；「846 行」写完那句话就变 850；审计「-40 行」实为 +1；
   计划「约 330 行、1.3 倍」实为 536 行、2.15 倍）。**量不到就不写数字。**
2. **TDD 每个任务**：先写测试、**看着它失败**、再实现。没看着失败的测试不证明任何事。
3. **计划里的错要裁决并记录**，不要偷偷改。ledger 里 `Ruling:` 一行一件事、附「错了的代价」。
4. **收到审查意见不要照单全收**——逐条对照代码核验。上个会话的 12 条终审发现核验后**全部成立**，
   但我自己修的 F1 第一版**把既有测试打破了**（把「字段认不出来」静默当成「没有列名」），
   是门控拦住了提交。**审查者也会漏，你自己也会错。**
5. **粗粒度的字符串检查会给出看起来像结论的噪声**：`grep 标题` 会命中权威词 `标题行区`，
   `grep TBD` 会命中「无 TBD 这类行」。用否定环视或逐条看。

## 下一步（新会话从这里开始）

1. 读规格 + 计划 + ledger（ledger 里有预检结果：**4 条共享接口行，未发现冲突**）
2. 取得人类伙伴对**分支**与**执行方式**的决定
3. 按 `executing-plans` 或 `subagent-driven-development` 从 **Task 1** 开始：
   `输出契约与 md 渲染`（Create `src/llm_column_extract/output.py`、`render.py`）
4. 到 **Task 5** 时裁决一件事：`FakeClient` 现在被排在 `src/llm_column_extract/client.py` 里，
   即**测试替身进生产代码**。选项 (a) 留在 src 便于两个测试模块 import；
   (b) 挪到 `tests/_fake_client.py`。**记 `Ruling:`。**
