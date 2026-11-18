"""单一职责：公示主体消歧（entity_resolve）。

把每条公示记录解析到规范主体，并接入现有模型（record.data）与稳定键：

- 规范值：规范名称 canonical_name、统一社会信用代码/案号等标识、规范地址，
  全部由实体的观察集合（observations）确定性重算，与输入顺序无关；
- 稳定主体键：ent-u-*（统一社会信用代码锚点）/ ent-n-*（核心名+地区锚点），
  由记录自身内容派生，输入换序、重复导入、进程重启均不改变；
- 同主体异名：同一标识下的历史名称/简称进入 aliases；核心名+地区一致且地址
  兼容时自动合并；有案号 corroborate 的跨地区重复公示也合并；
- 同名不同主体：标识不同或地区不兼容时不合并；同地区同名但标识不同的主体
  互相记 name-clash 备查；
- 标识一致而地址冲突：标识优先，仍归同一主体，但冲突显式写入实体 conflicts
  与记录 adjudication（含取舍 policy 与证据记录 id），不做静默覆盖；
- 人工裁决依据：无法自动确认的记录 status=pending，候选主体、匹配/冲突理由
  写入 record.candidates / record.adjudication，并持久化到注册表 pending 区，
  候选保存后重启仍保持一致。

注册表（JSON，原子写）是事实来源；实体是观察的派生视图。重复导入时先按记录
id 撤回旧观察再重新解析，因此幂等；旧数据（无标识字段的记录）走名称锚点兜底。
"""
import hashlib
import json
import os
import re
import unicodedata

from .config import ENTITY_REGISTRY
from .keyer import key_of

REGISTRY_PATH = ENTITY_REGISTRY
_REG = None  # 进程内缓存；_reset_cache() 模拟重启（测试/运维用）

_USCC_ALPHABET = set("0123456789ABCDEFGHJKLMNPQRTUWXY")  # GB 32100（不含 I/O/S/V/Z）
_CORP_SUFFIXES = (
    "股份有限公司", "有限责任公司", "集团有限公司", "有限公司",
    "分公司", "集团", "公司", "工厂", "厂", "店", "中心",
    "事务所", "合作社", "商行", "经营部",
)
_REGION_RE = re.compile(r"([一-鿿]{2,6}?)(省|市|区|县|州)")
_REGION_RANK = {"市": 0, "区": 1, "县": 1, "州": 2, "省": 3}
_REL_AUTO = {"same", "contained", "empty"}       # 地址兼容：可自动合并
_REL_SCORE = {"same": 10, "contained": 8, "empty": 4, "same-region": -5, "conflict": -15}
_REL_RANK = {"same": 0, "contained": 1, "empty": 2, "same-region": 3, "conflict": 4}
_MIN_ALIAS_LEN = 2   # 进入候选比对的最短核心名长度
_MIN_AUTO_CORE = 3   # 短于此长度的核心名不足以独立建档，一律待审


# ---------- 文本规范化 ----------
def _norm_text(value):
    s = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", s)


def _name_key(name):
    return _norm_text(name).upper()


def _region_key(address):
    """从地址提取地区键：优先市，其次区/县，再次州，最后省；取不到为 ""。"""
    a = _norm_text(address)
    found = []
    for m in _REGION_RE.finditer(a):
        token, kind = m.groups()
        found.append((_REGION_RANK[kind], m.start(), token))
    if not found:
        return ""
    found.sort()
    return found[0][2]


def _core_name(name, region=""):
    """核心名：去地区前缀与行业通用后缀，用于同主体异名（全称/简称/历史名）比对。"""
    s = _name_key(name)
    if region and s.startswith(region):
        s = s[len(region):]
    for _ in range(3):
        for suf in _CORP_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                break
        else:
            break
    return s


def _addr_relation(a, b):
    """两个地址的关系：same / contained / empty / same-region / conflict。"""
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return "empty"
    if a == b:
        return "same"
    if a in b or b in a:
        return "contained"
    ra, rb = _region_key(a), _region_key(b)
    if ra and ra == rb:
        return "same-region"
    return "conflict"


def _best_relation(addr, addrs):
    rels = [_addr_relation(addr, x) for x in addrs]
    if not rels:
        return "empty"
    return min(rels, key=lambda r: _REL_RANK[r])


def _norm_uscc(value):
    s = _norm_text(value).upper()
    if len(s) == 18 and set(s) <= _USCC_ALPHABET:
        return s
    return ""


# ---------- 记录字段提取 ----------
def _first(rec, keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _display(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _extract(rec):
    name = _first(rec, ("name", "entity_name", "subject"))
    address = _first(rec, ("address", "addr", "domicile", "reg_address"))
    region = _region_key(address)
    raw_uscc = _first(rec, ("uscc", "credit_code", "social_credit_code",
                            "unified_social_credit_code"))
    return {
        "name": _display(name),
        "name_key": _name_key(name),
        "core": _core_name(name, region),
        "region": region,
        "address": _display(address),
        "uscc": _norm_uscc(raw_uscc),
        "uscc_raw": raw_uscc,
        "case_no": _norm_text(_first(rec, ("case_no", "case_number", "case"))),
        "ts": _first(rec, ("ts", "date", "publish_date", "published_at", "pub_date")),
        "src": _first(rec, ("src", "source")),
    }


def _record_id(rec):
    rid = rec.get("id")
    return str(rid) if rid not in (None, "") else "k:" + key_of(rec)


def _entity_id(kind, anchor):
    digest = hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:12]
    return "ent-%s-%s" % ({"uscc": "u", "name": "n"}[kind], digest)


# ---------- 注册表持久化 ----------
def _empty_registry():
    return {"version": 1, "entities": {}, "pending": {}}


def _load():
    global _REG
    if _REG is None:
        try:
            with open(REGISTRY_PATH, encoding="utf-8") as f:
                _REG = json.load(f)
        except (OSError, ValueError):
            _REG = _empty_registry()
        for k, v in _empty_registry().items():
            _REG.setdefault(k, v)
    return _REG


def _save(reg):
    path = str(REGISTRY_PATH)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, sort_keys=True, indent=2)
    os.replace(tmp, path)  # 原子替换，避免半截文件


def _reset_cache():
    global _REG
    _REG = None


# ---------- 实体派生视图 ----------
def _derive_conflicts(ent, addrs):
    """地址冲突显式留痕：同一主体下存在互不兼容的地址时生成冲突条目。

    冲突是从观察集合派生的确定性事实（含取舍 policy 与证据记录 id），
    不会随某一次写入被覆盖掉。
    """
    uniq = sorted({_norm_text(a): a for a in addrs}.items())
    divergent = []
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            if _addr_relation(uniq[i][0], uniq[j][0]) in ("same-region", "conflict"):
                divergent.append([uniq[i][1], uniq[j][1]])
    if not divergent:
        return []
    evidence = {}
    for o in ent["observations"]:
        if o["address"]:
            evidence.setdefault(o["address"], []).append(o["record_id"])
    policy = "uscc-anchor-priority" if ent["kind"] == "uscc" else "case-no-corroborated"
    return [{
        "field": "address",
        "values": [d for _, d in uniq],
        "divergent_pairs": divergent,
        "resolution": "kept-single-entity",
        "policy": policy,
        "evidence": {k: sorted(v) for k, v in sorted(evidence.items())},
    }]


def _recompute(ent):
    """由观察集合重算规范值：规范名称/别名/地址/案号/冲突，全部确定性。"""
    obs = sorted(ent["observations"], key=lambda o: o["record_id"])
    ent["observations"] = obs
    names = {}
    for o in obs:
        if o["name"]:
            names[o["name"]] = max(names.get(o["name"], ""), o["ts"])
    if names:
        keys = {n: _name_key(n) for n in names}
        # 被更长名称包含的简称不作规范名；其余按（最新公示时间, 更长, 字典序）取
        pool = [n for n in names
                if not any(n != m and keys[n] in keys[m] for m in names)] or list(names)
        ent["canonical_name"] = max(pool, key=lambda n: (names[n], len(keys[n]), keys[n]))
        ent["aliases"] = sorted(n for n in names if n != ent["canonical_name"])
    else:
        ent["canonical_name"] = ""
        ent["aliases"] = []
    addrs = {}
    for o in obs:
        if o["address"]:
            addrs[o["address"]] = max(addrs.get(o["address"], ""), o["ts"])
    ent["addresses"] = sorted(addrs)
    ent["canonical_address"] = max(addrs, key=lambda a: (addrs[a], _norm_text(a))) if addrs else ""
    ent["identifiers"]["case_nos"] = sorted({o["case_no"] for o in obs if o["case_no"]})
    if ent["kind"] == "uscc":
        region = _region_key(ent["canonical_address"])
        ent["region"] = region
        ent["core_name"] = _core_name(ent["canonical_name"], region)
        ent["regions"] = sorted({r for r in (_region_key(a) for a in addrs) if r})
    else:
        ent["regions"] = [ent["region"]] if ent["region"] else []
    ent["conflicts"] = _derive_conflicts(ent, addrs)


def _observation(info, rid):
    return {"record_id": rid, "src": info["src"], "ts": info["ts"],
            "name": info["name"], "address": info["address"],
            "uscc": info["uscc"], "case_no": info["case_no"]}


def _retract(reg, rid):
    """重复导入/数据订正：先撤回该记录的旧观察，使重解析幂等。"""
    emptied = []
    for eid, ent in reg["entities"].items():
        before = len(ent["observations"])
        if not before:
            continue
        ent["observations"] = [o for o in ent["observations"] if o["record_id"] != rid]
        if len(ent["observations"]) != before:
            if ent["observations"]:
                _recompute(ent)
            else:
                emptied.append(eid)
    for eid in emptied:
        del reg["entities"][eid]
    reg["pending"].pop(rid, None)


def _regions_compatible(a_regions, b_regions):
    if not a_regions or not b_regions:
        return True
    return bool(set(a_regions) & set(b_regions))


def _refresh_notes(reg):
    """同地区同名但身份证据不同的主体互相留 name-clash 备查（派生、确定性）。"""
    ents = sorted(reg["entities"].values(), key=lambda e: e["entity_id"])
    for ent in ents:
        ent["notes"] = []
    for i in range(len(ents)):
        for j in range(i + 1, len(ents)):
            a, b = ents[i], ents[j]
            if (a["core_name"] and a["core_name"] == b["core_name"]
                    and _regions_compatible(a["regions"], b["regions"])):
                a["notes"].append("name-clash:" + b["entity_id"])
                b["notes"].append("name-clash:" + a["entity_id"])


# ---------- 候选与匹配 ----------
def _name_relation(info, ent):
    matched, score = [], 0
    if info["name_key"]:
        entity_names = {ent["canonical_name"]} | set(ent["aliases"])
        if info["name_key"] in {_name_key(n) for n in entity_names if n}:
            matched.append("name-exact")
            score += 40
    core_e, core_r = ent["core_name"], info["core"]
    if core_e and core_r:
        if core_e == core_r:
            matched.append("core-equal")
            score += 30
        elif (len(core_e) >= _MIN_ALIAS_LEN and len(core_r) >= _MIN_ALIAS_LEN
              and (core_e in core_r or core_r in core_e)):
            matched.append("core-alias")
            score += 20
    if matched and info["region"] and info["region"] in ent["regions"]:
        matched.append("region-match")
        score += 10
    return matched, score


def _candidate_of(ent, info, rel):
    matched, score = _name_relation(info, ent)
    score += _REL_SCORE[rel]
    if info["case_no"] and info["case_no"] in ent["identifiers"]["case_nos"]:
        matched.append("case-no")
        score += 25
    return {
        "entity_id": ent["entity_id"],
        "kind": ent["kind"],
        "canonical_name": ent["canonical_name"],
        "score": score,
        "matched": matched,
        "conflicts": ["address-" + rel] if rel in ("same-region", "conflict") else [],
        "addr_relation": rel,
    }


def _scan_candidates(reg, info, exclude_eid=None):
    out = []
    info_regions = [info["region"]] if info["region"] else []
    for ent in reg["entities"].values():
        if ent["entity_id"] == exclude_eid:
            continue
        case_hit = info["case_no"] and info["case_no"] in ent["identifiers"]["case_nos"]
        if not _regions_compatible(info_regions, ent["regions"]) and not case_hit:
            continue
        matched, _ = _name_relation(info, ent)
        if not matched:
            continue
        rel = _best_relation(info["address"], ent["addresses"])
        out.append(_candidate_of(ent, info, rel))
    out.sort(key=lambda c: (-c["score"], c["entity_id"]))
    return out


# ---------- 解析决策 ----------
def _resolve(reg, info):
    """返回决策：attach / create / pending。只读注册表，不写。"""
    # 1) 强标识锚点：统一社会信用代码
    if info["uscc"]:
        anchor = "uscc:" + info["uscc"]
        ent = reg["entities"].get(_entity_id("uscc", anchor))
        if ent is not None:
            return {"action": "attach", "entity": ent, "matched_via": ["uscc-anchor"],
                    "policy": "uscc-anchor-priority",
                    "addr_relation": _best_relation(info["address"], ent["addresses"])}
        return {"action": "create", "kind": "uscc", "anchor": anchor,
                "matched_via": ["new-uscc-entity"], "policy": "new-entity-no-candidates"}
    # 2) 名称锚点（缺标识兜底）
    if not info["core"]:
        return {"action": "pending", "reasons": ["insufficient-identity"], "candidates": []}
    if len(info["core"]) < _MIN_AUTO_CORE:
        return {"action": "pending", "reasons": ["weak-identity"],
                "candidates": _scan_candidates(reg, info)}
    anchor = "name:%s|%s" % (info["core"], info["region"])
    anchor_ent = reg["entities"].get(_entity_id("name", anchor))
    cands = _scan_candidates(
        reg, info, exclude_eid=anchor_ent["entity_id"] if anchor_ent else None)
    identified = [c for c in cands if c["kind"] == "uscc" and "core-equal" in c["matched"]]
    # 2a) 唯一的持标识同名主体：缺标识记录归入已识别主体
    if len(identified) == 1 and len(cands) == 1:
        c = identified[0]
        ent = reg["entities"][c["entity_id"]]
        corrob = info["case_no"] and info["case_no"] in ent["identifiers"]["case_nos"]
        if c["addr_relation"] in _REL_AUTO or corrob:
            return {"action": "attach", "entity": ent,
                    "matched_via": ["uscc-entity-name-match"],
                    "policy": "unique-identified-candidate", "addr_relation": c["addr_relation"]}
        return {"action": "pending", "reasons": ["address-divergent"], "candidates": cands}
    if identified:
        return {"action": "pending", "reasons": ["ambiguous-identified"],
                "candidates": cands + ([_candidate_of(anchor_ent, info,
                                        _best_relation(info["address"], anchor_ent["addresses"]))]
                                       if anchor_ent else [])}
    # 2b) 名称锚点命中
    if anchor_ent is not None:
        rel = _best_relation(info["address"], anchor_ent["addresses"])
        corrob = info["case_no"] and info["case_no"] in anchor_ent["identifiers"]["case_nos"]
        if rel in _REL_AUTO or corrob:
            via = ["name-anchor"]
            if corrob and rel not in _REL_AUTO:
                via.append("case-no-corroborated")
            return {"action": "attach", "entity": anchor_ent, "matched_via": via,
                    "policy": "name-core-region-anchor", "addr_relation": rel,
                    "alternatives": cands}
        own = _candidate_of(anchor_ent, info, rel)
        return {"action": "pending", "reasons": ["address-divergent"],
                "candidates": [own] + cands}
    # 2c) 锚点未命中：无候选则建档，唯一兼容候选则挂靠，否则待审
    if not cands:
        return {"action": "create", "kind": "name", "anchor": anchor,
                "matched_via": ["new-name-entity"], "policy": "new-entity-no-candidates"}
    if len(cands) == 1:
        c = cands[0]
        corrob = "case-no" in c["matched"]
        if c["addr_relation"] in _REL_AUTO or corrob:
            via = ["alias-attach"] + c["matched"]
            if corrob and c["addr_relation"] not in _REL_AUTO:
                via.append("case-no-corroborated")
            return {"action": "attach", "entity": reg["entities"][c["entity_id"]],
                    "matched_via": via, "policy": "unique-compatible-candidate",
                    "addr_relation": c["addr_relation"]}
    return {"action": "pending", "reasons": ["ambiguous-alias"], "candidates": cands}


# ---------- 主入口 ----------
def process(record):
    """对记录做单一职责变换：附加主体解析结果（实体引用/规范值/候选/裁决依据）。"""
    rec = dict(record)
    reg = _load()
    info = _extract(rec)
    rid = _record_id(rec)
    _retract(reg, rid)
    decision = _resolve(reg, info)
    notes = []
    if info["uscc_raw"] and not info["uscc"]:
        notes.append("invalid-uscc-ignored:" + info["uscc_raw"])
    if decision["action"] == "pending":
        candidates = decision["candidates"]
        reg["pending"][rid] = {
            "record_id": rid, "reasons": decision["reasons"], "candidates": candidates,
            "name": info["name"], "src": info["src"], "ts": info["ts"],
        }
        rec.update({
            "entity_id": None,
            "entity_status": "pending",
            "canonical_name": None,
            "entity_aliases": [],
            "matched_via": [],
            "candidates": candidates,
            "adjudication": {
                "status": "pending",
                "decided_by": "manual-required",
                "reasons": decision["reasons"],
                "candidate_count": len(candidates),
                "notes": notes,
            },
        })
    else:
        if decision["action"] == "create":
            ent = {
                "entity_id": _entity_id(decision["kind"], decision["anchor"]),
                "kind": decision["kind"],
                "anchor": decision["anchor"],
                "core_name": info["core"],
                "region": info["region"],
                "regions": [],
                "canonical_name": "",
                "canonical_address": "",
                "aliases": [],
                "addresses": [],
                "identifiers": {"uscc": info["uscc"] if decision["kind"] == "uscc" else "",
                                "case_nos": []},
                "conflicts": [],
                "notes": [],
                "observations": [],
            }
            reg["entities"][ent["entity_id"]] = ent
        else:
            ent = decision["entity"]
        ent["observations"].append(_observation(info, rid))
        _recompute(ent)
        rec.update({
            "entity_id": ent["entity_id"],
            "entity_status": "auto",
            "canonical_name": ent["canonical_name"],
            "entity_aliases": list(ent["aliases"]),
            "matched_via": decision["matched_via"],
            "candidates": [],
            "adjudication": {
                "status": "auto",
                "decided_by": "policy",
                "policy": decision["policy"],
                "rules": decision["matched_via"],
                "alternatives": decision.get("alternatives", []),
                "conflicts": ent["conflicts"],
                "notes": notes,
            },
        })
    _refresh_notes(reg)
    _save(reg)
    return rec
