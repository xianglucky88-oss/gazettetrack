"""零依赖测试运行器：环境无 pytest 时执行 tests/test_attach.py 的全部用例。

用法：PYTHONPATH=. python3 -m tests.attach_runner
有 pytest 时优先使用 `make test`（test_attach 同时兼容 pytest 发现规则）。
"""
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import test_attach  # noqa: E402


class _TmpPath:
    """最小 tmp_path 垫片（test_attach 目前未使用，保留兼容签名）。"""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name)

    def cleanup(self):
        self._td.cleanup()


def main():
    failures = 0
    passed = 0
    funcs = [
        (name, obj) for name, obj in inspect.getmembers(test_attach, inspect.isfunction)
        if name.startswith("test_") and obj.__module__ == test_attach.__name__
    ]
    for name, fn in sorted(funcs):
        params = inspect.signature(fn).parameters
        kwargs = {}
        tmp = None
        if "tmp_path" in params:
            tmp = _TmpPath()
            kwargs["tmp_path"] = tmp.path
        try:
            fn(**kwargs)
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
        finally:
            if tmp is not None:
                tmp.cleanup()
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
