"""单一职责：跨地区公示去重与合并判定。

输入是 ``gazette_parse`` + ``entity_resolve`` 处理过的一批记录，输出是「事项组」
（``MatterGroup``）。本模块只回答一个问题：两条记录是不是同一事项？

判定信号（全部可解释、可回放）
------------------------------
* 案号精确键 ``case_no_key`` —— 最强身份，跨地区转载以它为准；
* 主体 ``entity_id`` —— 同一主体名/信用代码归一后稳定；
* 事项 ``matter_key`` —— 标题归一；
* 日期 —— 只用于缺案号候选的时间窗；
* 正文指纹 ``body_fp`` 与正文相似度 —— 区分「后续修订」与「无变化」；
* 附件指纹 ``attachment_fp`` —— 区分「正文相同但附件不同」；
* 案号前缀 + 序号 —— 识别「案号相近但不同」的独立事项。

缺案号时的候选门槛（重要）
--------------------------
没有案号时唯一可靠的强身份是主体，因此门槛更严，宁可漏合也不误合：

1. 候选生成：按 ``entity_id`` / ``matter_key`` / ``body_fp`` 分桶，只有落在
   同一桶的记录才会比较；案号前缀/主体这类粗桶再按公示日期窗剪枝，避免
   全量两两比较（附件指纹只作证据、不开桶，否则共用模板附件会爆炸）；
2. 硬门槛：``entity_id`` 必须相同（两侧都有主体时）；主体缺失只允许
   「``body_fp`` 与 ``matter_key`` 都相同」的完全相同转载合并；
3. 事项门槛：``matter_key`` 明确相同即视为同一事项（标题就是事项身份，
   日期跨度不推翻它）；标题明确不同则直接判为独立事项，正文再像也不合并；
4. 两侧主体都缺失时，只接受「事项标题相同且正文相似度 >= 0.95」；
5. 标题至少一侧缺失时，才退到「正文相似度 >= 0.80 且公示日期间距 <= 90 天」；
6. 同一事项的后续版本判定：
   * ``body_fp`` 相同 → 重复转载（``unchanged``）；
   * ``body_fp`` 不同 → 后续版本（``body_revised``），绝不当作「无变化」；
   * 仅附件指纹不同 → ``attachment_revised``，正文仍记为未变；
   * 一侧正文缺失 → ``content_unknown``，不声称内容相同；
7. 案号相同但主体明确冲突、或同一案号下标题/正文明显不同 → 判为独立事项
   并加日期后缀区分组键，避免把误录串号静默合并。

案号相近（同一法院前缀、不同序号）但不相等的记录，候选阶段会显式比较，
判定为独立事项并留下 ``near_case_no`` 证据。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .normalizer import (
    aggregate_sources,
    date_ordinal,
    normalize_text,
    sort_descriptors,
    source_descriptor,
)

# 缺案号候选门槛（公开常量，便于测试与审计阅读）
MISSING_CASE_BODY_SIM = 0.80          # 事项门槛：正文相似度
MISSING_CASE_DATE_WINDOW_DAYS = 90    # 时间门槛：公示日期间距
SAME_MATTER_SIM_EVIDENCE_MIN = 0.95   # 无主体但完全同文的兜底相似度
NEAR_CASE_MAX_SUFFIX_DELTA = 9999     # 同前缀不同序号视为「相近案号」
NEAR_CASE_EVIDENCE_MIN_SIM = 0.50     # 相近案号写证据时的最低正文相似度

_SEQ_RE = re.compile(r"^(?P<prefix>.*?)(?P<seq>\d+)号?$")


@dataclass
class MatterGroup:
    """一组被判为同一事项的观测（或一个独立事项）。"""

    group_key: str
    records: list[dict[str, Any]]
    decisions: list[dict[str, Any]] = field(default_factory=list)
    # 与组外记录之间的「独立事项」判定（如相近案号），用于解释为何没有合并。
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def anchors(self) -> list[str]:
        return sorted(rec["_anchor"] for rec in self.records)


def _anchor_of(record: dict[str, Any]) -> str:
    if record.get("_anchor"):
        return str(record["_anchor"])
    anchor = str(record.get("id") or "")
    if not anchor:
        basis = {
            "src": record.get("src"),
            "case_no": record.get("case_no_key"),
            "matter": record.get("matter_key"),
            "date": record.get("date"),
            "body": record.get("body_fp"),
            "attachments": record.get("attachment_fp"),
        }
        anchor = "anon-" + hashlib.sha256(
            json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    return anchor


def _case_prefix(case_key: str) -> str:
    """案号前缀：(2026)京01民初123号 → (2026)京01民初 ；序号单独比较。"""
    match = _SEQ_RE.match(case_key or "")
    if not match:
        return case_key or ""
    return match.group("prefix")


def _case_seq(case_key: str) -> int | None:
    match = _SEQ_RE.match(case_key or "")
    return int(match.group("seq")) if match else None


def _bigrams(text: str) -> set[str]:
    text = normalize_text(text)
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i : i + 2] for i in range(len(text) - 1)}


def body_similarity(left: Any, right: Any) -> float | None:
    """字符二元组 Dice 相似度；任一侧为空时返回 ``None``（未知，不是 0）。"""
    a, b = _bigrams(str(left or "")), _bigrams(str(right or ""))
    if not a or not b:
        return None
    return round(2 * len(a & b) / (len(a) + len(b)), 4)


def _date_gap_days(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    a = date_ordinal(left.get("date"))
    b = date_ordinal(right.get("date"))
    if a is None or b is None:
        return None
    return abs(a - b)


def _is_gazette(record: dict[str, Any]) -> bool:
    return bool(
        record.get("is_gazette")
        or record.get("case_no_key")
        or record.get("entity_id")
        or record.get("matter_key")
        or record.get("body_fp")
        or record.get("attachment_fp")
    )


def _block_keys(record: dict[str, Any]) -> set[str]:
    """候选分桶键；只有共享桶的记录才进入比较（控制比较成本）。"""
    keys: set[str] = set()
    if record.get("case_no_key"):
        keys.add("case:" + record["case_no_key"])
        keys.add("caseprefix:" + _case_prefix(record["case_no_key"]))
    if record.get("entity_id"):
        keys.add("entity:" + str(record["entity_id"]))
    if record.get("matter_key"):
        keys.add("matter:" + str(record["matter_key"]))
    if record.get("body_fp"):
        keys.add("body:" + str(record["body_fp"]))
    # 附件指纹只作为裁决证据，不作为候选桶：同一模板附件会被大量无关事项共用，
    # 拿它开桶会把比较成本推回全量两两比较。
    return keys


def _candidate_pairs(records: list[dict[str, Any]]) -> tuple[list[tuple[int, int]], dict[str, int]]:
    """按桶生成候选对；返回 (去重排序后的候选对, 生成成本统计)。

    桶内剪枝（保持可解释）：``entity:`` / ``caseprefix:`` 这类「粗桶」容易随
    记录数平方增长，因此只比较公示日相邻（<= 缺案号时间窗）或日期未知的对；
    精确键桶（case/body/attachment/matter）不受日期剪枝，保证同文转载必被比较。
    """
    buckets: dict[str, list[int]] = {}
    for idx, record in enumerate(records):
        for key in _block_keys(record):
            buckets.setdefault(key, []).append(idx)
    pairs: set[tuple[int, int]] = set()
    stats = {
        "bucket_pairs": 0,
        "pruned_by_date_window": 0,
        "date_unknown_pairs": 0,
        "covered_by_body_bucket": 0,
    }
    body_bucket = "body:"
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        coarse = key.startswith(("entity:", "caseprefix:"))
        for pos, left in enumerate(members):
            for right in members[pos + 1 :]:
                stats["bucket_pairs"] += 1
                pair = (min(left, right), max(left, right))
                left_fp = records[left].get("body_fp")
                if (
                    not key.startswith(body_bucket)
                    and left_fp
                    and left_fp == records[right].get("body_fp")
                ):
                    # 同正文指纹的配对必然出现在 body 桶里，无需在其它桶重复比较。
                    stats["covered_by_body_bucket"] += 1
                    continue
                if coarse:
                    gap = _date_gap_days(records[left], records[right])
                    if gap is None:
                        stats["date_unknown_pairs"] += 1
                    elif gap > MISSING_CASE_DATE_WINDOW_DAYS:
                        stats["pruned_by_date_window"] += 1
                        continue
                pairs.add(pair)
    ordered = sorted(pairs)
    stats["candidates"] = len(ordered)
    return ordered, stats


def _evidence_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    similarity = body_similarity(left.get("body"), right.get("body"))
    left_case, right_case = left.get("case_no_key"), right.get("case_no_key")
    near_case = False
    if left_case and right_case and left_case != right_case:
        same_prefix = _case_prefix(left_case) == _case_prefix(right_case)
        delta = None
        if same_prefix:
            a, b = _case_seq(left_case), _case_seq(right_case)
            if a is not None and b is not None:
                delta = abs(a - b)
        near_case = bool(same_prefix and delta is not None and delta <= NEAR_CASE_MAX_SUFFIX_DELTA)
    return {
        "left": left["_anchor"],
        "right": right["_anchor"],
        "case_no_left": left.get("case_no"),
        "case_no_right": right.get("case_no"),
        "case_no_match": bool(left_case and left_case == right_case),
        "case_no_conflict": bool(left_case and right_case and left_case != right_case),
        "near_case_no": near_case,
        "entity_match": bool(
            left.get("entity_id") and left.get("entity_id") == right.get("entity_id")
        ),
        "entity_known": bool(left.get("entity_id") and right.get("entity_id")),
        "matter_match": bool(
            left.get("matter_key") and left.get("matter_key") == right.get("matter_key")
        ),
        "matter_known": bool(left.get("matter_key") and right.get("matter_key")),
        "body_fp_match": bool(left.get("body_fp") and left.get("body_fp") == right.get("body_fp")),
        "body_similarity": similarity,
        "body_known": bool(left.get("body") and right.get("body")),
        "attachment_fp_match": bool(
            left.get("attachment_fp") and left.get("attachment_fp") == right.get("attachment_fp")
        ),
        "date_gap_days": _date_gap_days(left, right),
    }


def decide(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """判定两条记录是否同一事项，返回可解释的决定与信号。"""
    ev = _evidence_pair(left, right)
    reasons: list[str] = []
    threshold_used: dict[str, Any] = {}

    if ev["case_no_match"]:
        reasons.append("case_no_exact")
        # 案号相同但主体明确冲突：几乎只可能是串号/错录，不能静默合并。
        conflicting_entity = ev["entity_known"] and not ev["entity_match"]
        # 案号相同、事项标题不同且正文差异大：同一案号下的不同公示事项。
        conflicting_matter = (
            bool(left.get("matter_key"))
            and bool(right.get("matter_key"))
            and not ev["matter_match"]
            and (
                ev["body_similarity"] is not None
                and ev["body_similarity"] < MISSING_CASE_BODY_SIM
            )
        )
        if conflicting_entity:
            reasons.append("case_no_match_entity_conflict")
            same = False
        elif conflicting_matter:
            reasons.append("case_no_match_matter_conflict")
            same = False
        else:
            same = True
    elif ev["case_no_conflict"]:
        reasons.append("case_no_conflict")
        if ev["near_case_no"]:
            reasons.append("near_case_no_independent")
        same = False
    else:
        # 至少一侧缺案号：走严门槛，按固定优先级逐条排除。
        similarity = ev["body_similarity"]
        gap = ev["date_gap_days"]
        threshold_used.update(
            {
                "body_similarity": MISSING_CASE_BODY_SIM,
                "date_window_days": MISSING_CASE_DATE_WINDOW_DAYS,
            }
        )
        if ev["entity_known"] and not ev["entity_match"]:
            reasons.append("entity_conflict")
            same = False
        elif ev["matter_known"] and not ev["matter_match"]:
            # 事项标题明确不同：同主体不同事项，正文再像也不合并。
            reasons.append("matter_conflict")
            same = False
        elif ev["body_fp_match"] and (ev["matter_match"] or not ev["matter_known"]):
            # 正文指纹完全一致：同一事项的重复转载。
            reasons.append("body_fp_exact_repost")
            if not ev["entity_known"]:
                reasons.append("entity_missing_requires_exact_body_and_matter")
            if not ev["matter_known"]:
                reasons.append("matter_missing_requires_exact_body")
            same = True
        elif not ev["entity_known"]:
            threshold_used["body_similarity"] = SAME_MATTER_SIM_EVIDENCE_MIN
            if (
                ev["matter_match"]
                and similarity is not None
                and similarity >= SAME_MATTER_SIM_EVIDENCE_MIN
            ):
                reasons.append("no_entity_exact_body_similarity")
                same = True
            else:
                reasons.append("entity_missing_below_gate")
                same = False
        elif ev["matter_match"]:
            # 主体相同 + 事项标题相同：标题本身已是事项身份，日期跨度不推翻它，
            # 正文差异按「后续版本」处理（绝不记作无变化）。
            reasons.append("missing_case_entity_and_matter")
            same = True
        else:
            # 标题至少一侧缺失：只能靠正文相似度 + 时间窗兜底。
            date_ok = gap is None or gap <= MISSING_CASE_DATE_WINDOW_DAYS
            if not date_ok:
                reasons.append("date_window_exceeded")
                same = False
            elif similarity is None or similarity < MISSING_CASE_BODY_SIM:
                reasons.append("body_similarity_below_gate")
                same = False
            else:
                reasons.append("missing_case_body_similarity_within_window")
                same = True

    if same:
        body_state = (
            "unchanged"
            if ev["body_fp_match"]
            else ("unknown" if not ev["body_known"] else "changed")
        )
        attachment_state = (
            "unchanged"
            if ev["attachment_fp_match"]
            else (
                "changed"
                if left.get("attachment_fp") and right.get("attachment_fp")
                else "unknown"
            )
        )
        if body_state == "unknown":
            change = "content_unknown"
            reasons.append("content_incomplete_not_unchanged")
        elif body_state == "unchanged" and attachment_state == "unchanged":
            change = "unchanged"
        elif body_state == "unchanged":
            change = "attachment_revised"
            reasons.append("attachment_revision_detected")
        elif attachment_state == "changed":
            change = "body_and_attachment_revised"
            reasons.append("body_revision_detected")
            reasons.append("attachment_revision_detected")
        else:
            change = "body_revised"
            reasons.append("body_revision_detected")
        if attachment_state == "changed":
            reasons.append("attachment_changed")
    else:
        change = "independent"
        attachment_state = "independent"

    return {
        "same_matter": same,
        "change": change,
        "attachment_state": attachment_state,
        "reasons": sorted(dict.fromkeys(reasons)),
        "signals": ev,
        "thresholds": threshold_used,
    }


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _split_inconsistent(
    components: dict[int, list[int]],
    decisions: list[dict[str, Any]],
    ordered: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[int, list[int]]:
    """把存在「组内但被判独立」配对的组件拆成一致子组。

    只使用已经比较过的判定，因此不增加比较成本；拆分按 (日期, anchor) 稳定排序，
    与输入顺序无关。
    """
    independent: dict[frozenset[int], bool] = {
        frozenset((d["left_index"], d["right_index"])): d["same_matter"] is False
        for d in decisions
    }
    if not any(independent.values()):
        return components

    out: dict[int, list[int]] = {}
    split_count = 0
    for root, members in components.items():
        if len(members) < 3:
            out[root] = members
            continue
        ordered_members = sorted(
            members,
            key=lambda idx: (date_ordinal(ordered[idx].get("date")) or 0, ordered[idx]["_anchor"]),
        )
        subgroup_lists: list[list[int]] = []
        for idx in ordered_members:
            placed = False
            for subgroup in subgroup_lists:
                if not any(
                    independent.get(frozenset((idx, other)), False) for other in subgroup
                ):
                    subgroup.append(idx)
                    placed = True
                    break
            if not placed:
                subgroup_lists.append([idx])
        if len(subgroup_lists) == 1:
            out[root] = members
            continue
        split_count += 1
        for offset, subgroup in enumerate(subgroup_lists):
            out[root * 1000 + offset] = subgroup
    if split_count:
        stats["inconsistent_components_split"] = split_count
    return out


def _group_key(members: list[dict[str, Any]]) -> str:
    if all(not _is_gazette(rec) for rec in members):
        return "plain:" + sorted(_anchor_of(rec) for rec in members)[0]
    case_keys = sorted({rec["case_no_key"] for rec in members if rec.get("case_no_key")})
    if len(case_keys) == 1:
        return "case:" + case_keys[0]
    entity_keys = sorted({rec["entity_id"] for rec in members if rec.get("entity_id")})
    matter_keys = sorted({rec["matter_key"] for rec in members if rec.get("matter_key")})
    if len(entity_keys) == 1 and len(matter_keys) == 1:
        basis = "matter|" + entity_keys[0] + "|" + matter_keys[0]
        return "matter:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    body_fps = sorted({rec["body_fp"] for rec in members if rec.get("body_fp")})
    if len(body_fps) == 1:
        return "body:" + body_fps[0]
    basis = "|".join(sorted(_anchor_of(rec) for rec in members))
    return "group:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def cluster(
    records: Iterable[dict[str, Any]],
    con=None,
) -> tuple[list[MatterGroup], dict[str, Any]]:
    """把记录聚成事项组；返回 (组列表, 候选/比较成本统计)。

    组键与输入顺序无关：候选对排序、并查集取最小值根、成员按 anchor 排序，
    因此换序输入得到相同分组。
    """
    observations: dict[str, dict[str, Any]] = {}
    plain: list[dict[str, Any]] = []
    for record in records:
        rec = dict(record)
        if not _is_gazette(rec):
            plain.append(rec)
            continue
        rec["_anchor"] = _anchor_of(rec)
        # 同 anchor 的重复抓取只保留最后一份观测（幂等）。
        observations[rec["_anchor"]] = rec

    stats = {
        "observations": len(observations) + len(plain),
        "candidates": 0,
        "comparisons": 0,
        "same_decisions": 0,
        "independent_decisions": 0,
        "near_case_independent": 0,
        "suppressed_groups": 0,
    }

    groups: list[MatterGroup] = []

    # 非公示记录保持基线语义：同一 id/key 视为同一条记录。
    plain_groups: dict[str, list[dict[str, Any]]] = {}
    for rec in plain:
        plain_groups.setdefault(_anchor_of(rec), []).append(rec)
    for anchor, members in sorted(plain_groups.items()):
        groups.append(
            MatterGroup(
                group_key="plain:" + anchor,
                records=sorted(members, key=lambda r: json.dumps(r, sort_keys=True, default=str)),
            )
        )

    ordered = sorted(observations.values(), key=lambda r: r["_anchor"])
    pairs, pair_stats = _candidate_pairs(ordered)
    stats.update(pair_stats)
    uf = _UnionFind(len(ordered))
    decisions: list[dict[str, Any]] = []
    for left_idx, right_idx in pairs:
        stats["comparisons"] += 1
        decision = decide(ordered[left_idx], ordered[right_idx])
        decision["left"] = ordered[left_idx]["_anchor"]
        decision["right"] = ordered[right_idx]["_anchor"]
        decision["left_index"], decision["right_index"] = left_idx, right_idx
        decisions.append(decision)
        if decision["same_matter"]:
            stats["same_decisions"] += 1
            uf.union(left_idx, right_idx)
        else:
            stats["independent_decisions"] += 1
            if decision["signals"]["near_case_no"]:
                stats["near_case_independent"] += 1

    components: dict[int, list[int]] = {}
    for idx in range(len(ordered)):
        components.setdefault(uf.find(idx), []).append(idx)
    # 单链合并可能把「A~B~C 但 A 与 C 被判独立」的记录串进同一组；
    # 用已算过的判定做一次一致性拆分（不新增比较）。
    components = _split_inconsistent(
        components,
        decisions,
        ordered,
        stats,
    )

    suppressed: set[str] = set()
    if con is not None:
        try:
            suppressed = {
                row[0]
                for row in con.execute("SELECT group_key FROM merge_suppressed").fetchall()
            }
        except Exception:  # pragma: no cover - 旧库无表时降级
            suppressed = set()

    decisions_by_pair = {
        (d["left_index"], d["right_index"]): d for d in decisions
    }
    independent_index = _index_independent(decisions)
    for root in sorted(components):
        members = [ordered[idx] for idx in components[root]]
        group_key = _group_key(members)
        member_pairs = [
            (min(i, j), max(i, j))
            for pos, i in enumerate(components[root])
            for j in components[root][pos + 1 :]
        ]
        group_decisions = [
            decisions_by_pair[pair] for pair in member_pairs if pair in decisions_by_pair
        ]
        if group_key in suppressed and len(members) > 1:
            stats["suppressed_groups"] += 1
            for rec in sorted(members, key=lambda r: r["_anchor"]):
                marked = dict(rec)
                marked["undo_of_group"] = group_key
                groups.append(
                    MatterGroup(
                        group_key="split:" + rec["_anchor"],
                        records=[marked],
                        decisions=[],
                        conflicts=[],
                    )
                )
            continue
        groups.append(
            MatterGroup(
                group_key=group_key,
                records=members,
                decisions=group_decisions,
                conflicts=_group_conflicts(members, independent_index),
            )
        )

    _disambiguate_group_keys(groups, stats)
    groups.sort(key=lambda g: g.group_key)
    return groups, stats


MAX_CONFLICTS_PER_GROUP = 20


def _index_independent(decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """anchor → 与它被显式判为独立的判定列表（一次性索引，避免逐组全表扫描）。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        if decision["same_matter"]:
            continue
        index.setdefault(decision["left"], []).append(decision)
        index.setdefault(decision["right"], []).append(decision)
    return index


def _group_conflicts(
    members: list[dict[str, Any]],
    independent_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """组内记录与组外记录被显式判为独立事项的证据（有上限，控制输出体积）。"""
    member_anchors = {rec["_anchor"] for rec in members}
    conflicts = []
    seen: set[tuple[str, str]] = set()
    for member in sorted(member_anchors):
        for decision in independent_index.get(member, []):
            pair = (decision["left"], decision["right"])
            if pair in seen:
                continue
            seen.add(pair)
            inside = [
                decision["left"] in member_anchors,
                decision["right"] in member_anchors,
            ]
            if sum(inside) != 1:
                continue
            signals = decision["signals"]
            near_case_explains = signals["near_case_no"] and (
                signals["body_similarity"] is not None
                and signals["body_similarity"] >= NEAR_CASE_EVIDENCE_MIN_SIM
            )
            if not (near_case_explains or signals["entity_match"] or signals["matter_match"]):
                # 只是案号前缀相同、正文毫不相干的配对没有解释价值，不写入证据。
                continue
            conflicts.append(
                {
                    "left": decision["left"],
                    "right": decision["right"],
                    "reasons": decision["reasons"],
                    "signals": {
                        key: signals[key]
                        for key in (
                            "case_no_left",
                            "case_no_right",
                            "near_case_no",
                            "entity_match",
                            "matter_match",
                            "body_similarity",
                            "date_gap_days",
                        )
                    },
                }
            )
            if len(conflicts) >= MAX_CONFLICTS_PER_GROUP:
                return conflicts
    return conflicts


def _disambiguate_group_keys(groups: list[MatterGroup], stats: dict[str, Any]) -> None:
    """同一事项键被多个组件复用时加日期后缀，避免落库键冲突。

    这种情况只会在「主体+事项标题相同但被显式判为独立」时出现（例如案号冲突）。
    后缀取组件内最早公示日，再退化为 anchor 哈希，二者都与输入顺序无关。
    """
    by_key: dict[str, list[MatterGroup]] = {}
    for group in groups:
        by_key.setdefault(group.group_key, []).append(group)
    collisions = 0
    for key, bucket in by_key.items():
        if len(bucket) < 2:
            continue
        collisions += len(bucket) - 1
        for group in bucket:
            dates = sorted(
                date_ordinal(rec.get("date")) or 0 for rec in group.records if rec.get("date")
            )
            if dates:
                suffix = _dt.date.fromordinal(dates[0]).isoformat()
            else:
                suffix = hashlib.sha256(
                    "|".join(sorted(rec["_anchor"] for rec in group.records)).encode("utf-8")
                ).hexdigest()[:8]
            group.group_key = f"{key}@{suffix}"
    if collisions:
        stats["group_key_collisions"] = collisions


def group_from_records(
    records: list[dict[str, Any]], group_key: str | None = None
) -> MatterGroup:
    """把已知属于同一事项的观测直接组成组，并补齐可解释的两两判定。

    供撤销/恢复回放使用：不重新做候选筛选，但证据仍逐对生成。
    """
    members = []
    for record in records:
        rec = dict(record)
        rec["_anchor"] = _anchor_of(rec)
        rec.setdefault("is_gazette", True)
        members.append(rec)
    members.sort(key=lambda rec: rec["_anchor"])
    decisions = []
    for idx, left in enumerate(members):
        for right in members[idx + 1 :]:
            decision = decide(left, right)
            decision["left"], decision["right"] = left["_anchor"], right["_anchor"]
            decisions.append(decision)
    return MatterGroup(
        group_key=group_key or _group_key(members),
        records=members,
        decisions=decisions,
    )


def _pick_base(members: list[dict[str, Any]]) -> dict[str, Any]:
    """基准观测：最早公示日；同日按 anchor，保证换序结果一致。"""
    return sorted(
        members,
        key=lambda r: (date_ordinal(r.get("date")) or 0, r["_anchor"]),
    )[0]


def _attachment_diff(members: list[dict[str, Any]]) -> dict[str, Any]:
    """每个附件出现在哪些 anchor、缺在哪些 anchor（附件差异证据）。"""
    per_anchor = {}
    for rec in members:
        per_anchor[rec["_anchor"]] = {
            att.get("sha256") or att.get("name"): att
            for att in (rec.get("attachments") or [])
        }
    all_ids = set().union(*[set(v) for v in per_anchor.values()]) if per_anchor else set()
    missing: dict[str, dict[str, Any]] = {}
    for att_id in sorted(str(a) for a in all_ids):
        present = sorted(
            a for a, atts in per_anchor.items() if any(str(k) == att_id for k in atts)
        )
        if len(present) != len(per_anchor):
            missing[att_id] = {
                "present_in": present,
                "missing_in": sorted(set(per_anchor) - set(present)),
            }
    return missing


def _revision_of(rec: dict[str, Any]) -> dict[str, Any]:
    """把一条观测展开成版本输入：内容 + 自己的一份来源。"""
    return {
        "body": rec.get("body"),
        "body_fp": rec.get("body_fp"),
        "attachments": rec.get("attachments") or [],
        "attachment_fp": rec.get("attachment_fp"),
        "date": rec.get("date"),
        "sources": [source_descriptor(rec)],
    }


def merge(group: MatterGroup) -> dict[str, Any]:
    """把同一事项组聚合成规范记录（含来源、差异证据、可撤销合并信息）。

    输出刻意携带 ``revisions``（每条观测的内容与其来源）与 ``evidence``：
    版本阶段据此识别「后续版本」，attach 阶段据此输出来源集合，store 的
    合并历史据此可完整撤销一次错误合并。
    """
    members = sorted(group.records, key=lambda r: r["_anchor"])
    base = _pick_base(members)
    sources = sort_descriptors([source_descriptor(rec) for rec in members])
    aggregate = aggregate_sources(sources)

    changes = sorted({d["change"] for d in group.decisions if d["same_matter"]})
    attachment_states = sorted(
        {d["attachment_state"] for d in group.decisions if d["same_matter"]}
    )
    if not changes:
        content_state = "single"
    elif "content_unknown" in changes:
        content_state = "unknown"
    elif "body_revised" in changes:
        content_state = "revised"
    else:
        content_state = "unchanged"

    body_variants: dict[str, dict[str, Any]] = {}
    for rec in members:
        if rec.get("body_fp"):
            entry = body_variants.setdefault(
                rec["body_fp"], {"body_fp": rec["body_fp"], "anchors": [], "date": rec.get("date")}
            )
            entry["anchors"].append(rec["_anchor"])
            if rec.get("date") and (not entry.get("date") or rec["date"] < entry["date"]):
                entry["date"] = rec["date"]
    variants = sorted(body_variants.values(), key=lambda v: (v.get("date") or "", v["body_fp"]))

    decision_evidence = [
        {
            "left": d["left"],
            "right": d["right"],
            "same_matter": d["same_matter"],
            "change": d["change"],
            "attachment_state": d["attachment_state"],
            "reasons": d["reasons"],
            "signals": d["signals"],
            "thresholds": d["thresholds"],
        }
        for d in group.decisions
    ]

    canonical = dict(base)
    canonical.pop("_anchor", None)
    canonical["is_gazette"] = True
    canonical["group_key"] = group.group_key
    canonical["case_no"] = base.get("case_no")
    canonical["case_no_key"] = base.get("case_no_key")
    canonical["entity_id"] = base.get("entity_id")
    canonical["entity_name"] = base.get("entity_name")
    canonical["matter"] = base.get("matter")
    canonical["matter_key"] = base.get("matter_key")
    canonical["date"] = aggregate["date"]
    canonical["sources"] = aggregate["sources"]
    canonical["source_anchors"] = aggregate["source_anchors"]
    canonical["srcs"] = aggregate["srcs"]
    canonical["regions"] = aggregate["regions"]
    canonical["attachments"] = aggregate["attachments"]
    canonical["attachment_fp"] = aggregate["attachment_fp"]
    canonical["attachment_variants"] = aggregate["attachment_variants"]
    canonical["body"] = base.get("body")
    canonical["body_fp"] = base.get("body_fp")
    canonical["body_variants"] = variants
    canonical["revisions"] = [_revision_of(rec) for rec in members]
    canonical["content_state"] = content_state
    canonical["attachment_state"] = (
        "changed"
        if "changed" in attachment_states
        else (attachment_states[0] if attachment_states else "single")
    )
    canonical["duplicate_count"] = max(1, len(members) - 1)
    canonical["evidence"] = {
        "decision": "same_matter" if len(members) > 1 else "singleton",
        "content_state": content_state,
        "attachment_state": canonical["attachment_state"],
        "body_variants": len(variants),
        "attachment_variants": len(canonical["attachment_variants"]),
        "missing_attachments": _attachment_diff(members),
        "thresholds": {
            "missing_case_body_sim": MISSING_CASE_BODY_SIM,
            "missing_case_date_window_days": MISSING_CASE_DATE_WINDOW_DAYS,
        },
        "pairs": decision_evidence,
        "non_merges": [
            {
                "left": conflict["left"],
                "right": conflict["right"],
                "reasons": conflict["reasons"],
            }
            for conflict in group.conflicts
        ],
    }
    canonical["merge"] = {
        "group_key": group.group_key,
        "members": [rec["_anchor"] for rec in members],
        "decision": canonical["evidence"]["decision"],
    }
    canonical["_stage_dedupe"] = "ok"
    return canonical


def process(record: dict[str, Any]) -> dict[str, Any]:
    """单记录形态：基线与既有调用方使用；非公示记录保持基线标记。"""
    if not _is_gazette(record):
        rec = dict(record)
        rec.setdefault("_stage_dedupe", "naive")
        return rec
    rec = dict(record)
    rec["_anchor"] = _anchor_of(rec)
    return merge(MatterGroup(group_key=_group_key([rec]), records=[rec]))


last_stats: dict[str, Any] = {}


def process_batch(records: list[dict[str, Any]], con=None) -> list[dict[str, Any]]:
    """批处理入口：聚类 → 合并 → 记录可撤销的合并历史。"""
    global last_stats
    groups, stats = cluster(records, con)
    merged = []
    for group in groups:
        if group.group_key.startswith("plain:"):
            # 基线非公示记录：同一 anchor 只保留一份（等价基线幂等）。
            plain = dict(group.records[0])
            plain.setdefault("_stage_dedupe", "naive")
            merged.append(plain)
            continue
        canonical = merge(group)
        if len(group.records) > 1 and con is not None:
            from . import store as _store
            from .versioning import observations_of

            _store.record_merge(
                con,
                group.group_key,
                "same_matter",
                [rec["_anchor"] for rec in group.records],
                observations_of(canonical),
                action="merge",
            )
        merged.append(canonical)
    last_stats = dict(stats)
    return merged
