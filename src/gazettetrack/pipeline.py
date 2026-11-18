"""薄编排：loader → normalizer → 业务阶段 → keyer → store。

不含业务判断。阶段若提供 ``process_con(con, record)`` 则复用同一连接/事务，
否则调用单参 ``process(record)``。收尾阶段若提供 ``finish(con)`` 则执行一次。
"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'gazette_parse', 'dedupe', 'attach', 'versioning']

def _stages():
    stages = []
    for name in CHAIN_NAMES:
        mod = importlib.import_module("." + name, __package__)
        stages.append(mod)
    return stages

def _apply(mod, con, rec):
    fn = getattr(mod, "process_con", None)
    if fn is not None:
        return fn(con, rec)
    return mod.process(rec)

def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        stages = _stages()
        fed = 0
        for raw in _loader.load():
            rec = raw
            for mod in stages:
                rec = _apply(mod, con, rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        for mod in stages:
            hook = getattr(mod, "finish", None)
            if hook is not None:
                hook(con)
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()
