"""离线 CLI：跑流水线，可选输出解释报告。"""
from __future__ import annotations

import argparse
import json
import sys

from . import pipeline
from . import recovery
from . import report
from . import store as _store


def _summarize(result):
    """撤销/恢复结果里的 records/record 体积较大，CLI 只输出摘要。"""
    return {
        key: value
        for key, value in result.items()
        if key not in {"records", "record"}
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gazettetrack")
    parser.add_argument("--db", default=None, help="sqlite 路径（默认 gazettetrack.db）")
    parser.add_argument("--explain", default=None, help="输出某个 key 的解释报告")
    parser.add_argument("--undo-group", default=None, help="撤销一次错误合并（group_key）")
    parser.add_argument("--restore-group", default=None, help="恢复一次被撤销的合并（group_key）")
    parser.add_argument("--summary", action="store_true", help="输出落库摘要")
    parser.add_argument("--indent", action="store_true", help="格式化 JSON")
    args = parser.parse_args(argv)

    con = _store.connect(args.db)
    try:
        payload = {}
        if args.undo_group:
            payload["undo"] = _summarize(recovery.undo_merge(con, args.undo_group))
        if args.restore_group:
            payload["restore"] = _summarize(recovery.restore_merge(con, args.restore_group))
        if not args.undo_group and not args.restore_group:
            payload.update(pipeline.run(con))
            _store.commit(con)
        if args.summary:
            payload["summary"] = report.summary(con)
        if args.explain:
            payload["explain"] = report.explain(con, args.explain)
    finally:
        con.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.indent else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
