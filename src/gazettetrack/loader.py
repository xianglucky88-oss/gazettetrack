"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。

默认读 ``fixtures/input.json``（基线夹具）。可通过 ``path`` 参数或环境变量
``GAZETTETRACK_FIXTURE`` 指向其它离线样本（例如 ``fixtures/gazette.json``）。
"""
import json
import os
from pathlib import Path

from .config import FIXTURES


def load(path=None):
    target = path or os.environ.get("GAZETTETRACK_FIXTURE") or FIXTURES
    with open(Path(target), encoding="utf-8") as f:
        return json.load(f)
