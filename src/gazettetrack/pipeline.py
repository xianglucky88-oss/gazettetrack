"""薄编排：loader → normalizer→gazette_parse→dedupe→attach → keyer → store。不含业务判断。"""
import importlib
from contextlib import nullcontext as _nullcontext
from . import store as _store
from . import loader as _loader
from . import attach as _attach
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'gazette_parse', 'dedupe', 'attach']

def run(con=None, attach_context=None):
    own = con is None
    con = con or _store.connect()
    # attach 阶段使用独立的附件证据库（只追加）；调用方可注入上下文（测试/定制下载器），
    # 缺省时自建默认上下文（离线 OfflineFetcher），无附件声明的记录自动透传。
    if attach_context is not None:
        _attach.set_default_context(attach_context)
        cm = _nullcontext(attach_context)
    else:
        cm = _attach.use_context()
    with cm:
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
