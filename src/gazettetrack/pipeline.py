"""薄编排：loader → normalizer→gazette_parse→dedupe→attach → keyer → store。不含业务判断。"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'gazette_parse', 'dedupe', 'attach']

def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()


def ingest(document, con=None, *, source=None, batch_id=None, contract=None,
           base_url=None, fetcher=None, max_retries=2, retry_backoff=0.0,
           sleep=None, resume_from=None):
    """解析一块异构公示栏并按三路产物落库。

    ``gazette_parse.parse_board`` 负责把条目 / 附件 / 错误拆开；本函数只负责
    按固定顺序把它们交给下游（不在编排层判断业务）：
      条目 → dedupe → attach → keyer → store；
      附件线索 → attach → store；
      错误 / 警告 → store 的批次错误表。

    单条坏行或单页失败只会出现在错误表里，不会阻塞其他条目入库。
    """
    parse = importlib.import_module(".gazette_parse", __package__)
    dedupe = importlib.import_module(".dedupe", __package__)
    attach = importlib.import_module(".attach", __package__)

    result = parse.parse_board(
        document,
        source=source,
        batch_id=batch_id,
        contract=contract,
        base_url=base_url,
        fetcher=fetcher,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        sleep=sleep,
        resume_from=resume_from,
    )

    own = con is None
    con = con or _store.connect()
    try:
        # 恢复时只写本次补抓出来的条目：原批次里已经成功的公告保持原样，
        # 不会被同值覆盖（“恢复不重写其他公告”）。
        previous_keys = (
            {entry.get("entry_key") for entry in resume_from.entries}
            if resume_from is not None
            else set()
        )
        written = 0
        skipped = 0
        for entry in result.to_dedupe():
            if entry.get("entry_key") in previous_keys:
                skipped += 1
                continue
            rec = entry
            rec = dedupe.process(rec)
            rec = attach.process(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            written += 1
        for clue in result.to_attach():
            if resume_from is not None and clue.get("entry_key") in previous_keys:
                continue
            stored = attach.process(dict(clue))
            _store.upsert_attachment(con, stored)
        _store.upsert_batch(con, result.batch_meta(), result.issues)
        _store.commit(con)
        return {
            "fed": len(result.entries),
            "rows": _store.count(con),
            "written": written,
            "skipped": skipped,
            "attachments": len(result.attachments),
            "errors": len(result.errors),
            "warnings": len(result.warnings),
            "batch_id": result.batch_id,
            "source": result.source,
            "result": result,
        }
    finally:
        if own:
            con.close()
