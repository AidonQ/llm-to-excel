# -*- coding: utf-8 -*-
"""`python -m llm_column_extract <input.jsonl> [-o <dir>]`（规格 §8）。

只做两件事：把 `sys.argv` 交给 `cli.main`，把它的**返回值**变成进程退出码。运行逻辑全在
`cli.main` 里，因为返回值才是可测的契约（`sys.exit` 在单元测试里没法断言，还会顺手杀掉
测试进程）。
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
