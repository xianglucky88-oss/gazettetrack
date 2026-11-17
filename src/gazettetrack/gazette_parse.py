"""单一职责：对记录做单一职责变换：gazette_parse。

用于记录变换。"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_gazette_parse", "naive")
    return rec
