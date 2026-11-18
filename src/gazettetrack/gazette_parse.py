"""异构公示栏解析：把页面/行拆成结构化条目、附件线索和局部错误。

设计要点
--------
* **一行坏数据只影响一行**：行级校验失败会形成带 ``source/batch_id/page/row``
  的 :class:`ParseIssue`，该行被跳过，同一页面的其余行继续解析。
* **页面失败只影响一页**：分页拉取失败会按 ``max_retries`` 局部重试；重试耗尽
  后记录 ``page_fetch_failed`` 并保留已解析页面，不会让整批公告回滚。
* **字段契约决定降级边界**：:class:`FieldContract` 明确 required / nullable /
  block_on_invalid。必填字段缺失只阻断当前条目；可选字段非法则降级为 ``None``
  并记录 warning。``DEFAULT_CONTRACT`` 只把 ``title`` 设为必填，调用方可按业务
  收紧（例如把 ``publish_date`` 也设为必填）。
* **条目、附件、错误分开返回**：:func:`parse_board` 返回 :class:`ParseResult`，
  其中 ``entries`` 交给 dedupe，``attachments`` 交给 attach，``errors`` 交给
  store/告警通道，互不混写。``result.routes()`` / ``to_dedupe()`` /
  ``to_attach()`` / ``to_store()`` 提供显式三路交付。
* **来源与批次随条目保留**：每个条目都带 ``source``、``batch_id``、``page``、
  ``row``、``locator`` 与 ``extraction`` 元信息。
* **重复抓取可幂等**：条目主键 ``id`` / ``entry_key`` 只由来源与公告内容派生，
  与本次 ``batch_id``、分页顺序无关；重复抓取得到相同主键，可在 store 幂等
  落库，不会重复写入。

公开入口
--------
``process(record)``          兼容原有单记录流水线阶段（保持输入字段不变）。
``parse_board(document, ...)`` 解析整块公示栏（可含多页、分页回调）。
``parse_pages(pages, ...)``  显式分页列表。
``parse_rows(rows, ...)``    单页行列表。
``parse_record(record, ...)`` 单条已结构化/扁平记录。
``download_attachments(...)`` 对附件线索做局部重试下载，失败逐附件隔离。
``parse_document = parse_board``、``parse = parse_board`` 为别名。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urljoin, urlparse

# ---------------------------------------------------------------------------
# 字段与表头契约
# ---------------------------------------------------------------------------

#: 结构化条目保证存在的规范字段；缺失或降级时为 None。
CANONICAL_FIELDS: tuple[str, ...] = (
    "entry_id",
    "title",
    "publish_date",
    "court",
    "case_no",
    "announcement_type",
    "subject",
    "amount",
    "detail_url",
)

#: 除规范字段外，解析器还会识别并单独输出的列。
PARSED_FIELDS: tuple[str, ...] = CANONICAL_FIELDS + ("attachments",)

NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "—",
        "——",
        "–",
        "null",
        "none",
        "n/a",
        "na",
        "nan",
        "无",
        "暂无",
        "不适用",
        "未提供",
        "未填写",
        "空",
        "nil",
    }
)

#: 表头别名。匹配时会先做 NFKC、去空白/标点/后缀单位，再做包含与模糊匹配。
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "entry_id": (
        "序号",
        "编号",
        "公告编号",
        "公示编号",
        "公告序号",
        "条目编号",
        "流水号",
        "id",
        "entry id",
        "record id",
        "no",
        "no.",
    ),
    "title": (
        "标题",
        "公告标题",
        "公示标题",
        "公告名称",
        "公示名称",
        "公告题目",
        "项目名称",
        "名称",
        "题目",
        "标的名称",
        "title",
        "headline",
        "name",
        "subject title",
    ),
    "publish_date": (
        "发布日期",
        "公示日期",
        "公告日期",
        "发布时间",
        "公示时间",
        "公告时间",
        "日期",
        "时间",
        "publish date",
        "published date",
        "release date",
        "date",
        "ts",
        "timestamp",
    ),
    "court": (
        "法院",
        "承办法院",
        "执行法院",
        "发布法院",
        "法院名称",
        "发布机构",
        "公示机关",
        "发布单位",
        "机构",
        "单位",
        "court",
        "issuer",
        "institution",
        "organization",
    ),
    "case_no": (
        "案号",
        "案件编号",
        "案件号",
        "案号文书号",
        "案号/文书号",
        "文书号",
        "执行案号",
        "case no",
        "case number",
        "case id",
        "docket",
    ),
    "announcement_type": (
        "公告类型",
        "公示类型",
        "公告类别",
        "类型",
        "类别",
        "type",
        "category",
        "notice type",
    ),
    "subject": (
        "主体",
        "主体名称",
        "被执行人",
        "当事人",
        "当事人名称",
        "债务人",
        "受让人",
        "申请人",
        "被申请人",
        "债权人",
        "申请执行人",
        "权利人",
        "subject",
        "debtor",
        "party",
        "entity",
        "applicant",
        "claimant",
    ),
    "amount": (
        "金额",
        "标的额",
        "标的金额",
        "涉案金额",
        "执行标的",
        "执行金额",
        "金额元",
        "amount",
        "value",
        "sum",
    ),
    "detail_url": (
        "链接",
        "详情",
        "详情链接",
        "原文链接",
        "查看详情",
        "公告链接",
        "url",
        "link",
        "detail url",
        "source url",
        "href",
    ),
    "attachments": (
        "附件",
        "附件下载",
        "附件列表",
        "相关附件",
        "下载",
        "文件",
        "attachments",
        "files",
        "downloads",
        "attachment",
    ),
}

#: 无表头旧格式的位置兜底（仅当无法从表头解析出 title 时使用）。
LEGACY_POSITIONS: tuple[tuple[str, int], ...] = (
    ("entry_id", 0),
    ("title", 1),
    ("publish_date", 2),
    ("court", 3),
    ("case_no", 4),
    ("subject", 5),
    ("amount", 6),
    ("detail_url", 7),
    ("attachments", 8),
)

_ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".rar",
        ".7z",
        ".txt",
        ".csv",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".ofd",
    }
)

_CASE_NO_RE = re.compile(r"[（(]?\d{4}[）)]?[^\s，,。；;]{0,24}?号")
_DATE_TEXT_RE = re.compile(
    r"(?P<y>\d{4})\s*[年./\-]\s*(?P<m>\d{1,2})\s*[月./\-]\s*(?P<d>\d{1,2})\s*日?"
)
_MONEY_RE = re.compile(r"^[¥￥]?\s*-?[\d,，]+(?:\.\d+)?\s*(?:亿元|亿|万元|万|元)?$")
_URL_RE = re.compile(r"^(?:https?:)?//|^(?:https?|ftp|file):", re.I)
_AMOUNT_HAS_UNIT_RE = re.compile(r"(?:亿元|万元|亿|万|元|RMB|rmb|¥|￥)\s*$")
_PUNCT_RE = re.compile(
    r"[\s　_\-—–－·．.、,，:：;；/\\|()（）\[\]【】<>《》“”\"'‘’]+"
)
_NUMBER_SUFFIX_RE = re.compile(r"(?:人民币|元|万元|亿元|单位)?$")
_WS_RE = re.compile(r"\s+")


class GazetteParseError(ValueError):
    """整块文档形态无法识别时抛出；页面/行级问题不会抛此异常。"""


class PageFetchError(GazetteParseError):
    """单页抓取在重试耗尽后仍失败；携带尝试次数供批次报告。"""

    def __init__(self, message: str, *, attempts: int, url: str | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.url = url


class _InvalidValue(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 通用清洗工具
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    """NFKC、去零宽字符、折叠空白。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("​", "").replace("‌", "").replace("‍", "")
    text = text.replace("﻿", "").replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    text = _clean_text(value).lower()
    return text in NULL_TOKENS


def _norm_header(value: Any) -> str:
    text = _clean_text(value).lower()
    text = _PUNCT_RE.sub("", text)
    # 「金额（元）」「标的金额（万元）」的括号已被去掉，这里再去掉尾部单位。
    text = _NUMBER_SUFFIX_RE.sub("", text)
    return text


def _slug(value: Any) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^0-9a-z一-鿿._-]+", "-", text)
    return text.strip("-._") or "source"


def _truncate(value: Any, limit: int = 300) -> str:
    text = _clean_text(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return _truncate(value)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_date(raw: Any) -> str:
    """解析常见日期/时间格式，返回 ``YYYY-MM-DD``；失败抛 _InvalidValue。"""
    text = _clean_text(raw)
    if not text:
        raise _InvalidValue("missing_value", "日期为空")
    match = _DATE_TEXT_RE.search(text)
    if match:
        try:
            parsed = date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        except ValueError as exc:
            raise _InvalidValue("invalid_date", f"非法日期：{text}") from exc
        return parsed.isoformat()
    # ISO / RFC3339 变体
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%Y %m %d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise _InvalidValue("invalid_date", f"无法识别日期：{text}")


def _parse_amount(raw: Any) -> int | float:
    text = _clean_text(raw)
    if not text:
        raise _InvalidValue("missing_value", "金额为空")
    text = text.replace("人民币", "").replace("RMB", "").replace("rmb", "")
    text = text.replace("¥", "").replace("￥", "").replace(" ", "")
    text = text.replace("，", ",").replace(",", "")
    multiplier = Decimal(1)
    for suffix, factor in (("亿元", Decimal(100000000)), ("亿", Decimal(100000000)),
                           ("万元", Decimal(10000)), ("万", Decimal(10000)), ("元", Decimal(1))):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    if not text or not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        raise _InvalidValue("invalid_amount", f"无法识别金额：{_clean_text(raw)}")
    try:
        value = Decimal(text) * multiplier
    except InvalidOperation as exc:
        raise _InvalidValue("invalid_amount", f"无法识别金额：{_clean_text(raw)}") from exc
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _resolve_url(raw: Any, base_url: str | None) -> tuple[str, bool]:
    text = _clean_text(raw)
    if not text:
        raise _InvalidValue("missing_value", "链接为空")
    lowered = text.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
        raise _InvalidValue("invalid_url", f"非 http 链接：{text}")
    absolute = urljoin(base_url, text) if base_url else text
    parsed = urlparse(absolute)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "ftp", "file"}:
        raise _InvalidValue("invalid_url", f"不支持的链接协议：{text}")
    resolved = bool(parsed.scheme)
    return absolute, resolved


def _looks_like_date(value: Any) -> bool:
    try:
        _parse_date(value)
        return True
    except _InvalidValue:
        return False


def _looks_like_amount(value: Any) -> bool:
    text = _clean_text(value)
    if not _MONEY_RE.match(text):
        return False
    try:
        _parse_amount(text)
        return True
    except _InvalidValue:
        return False


def _looks_like_url(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    if _URL_RE.match(text):
        return True
    return any(text.lower().split("?")[0].endswith(ext) for ext in _ATTACHMENT_EXTENSIONS)


def _guess_filename(url: str, fallback: str = "") -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name if path else ""
    if not name:
        name = _clean_text(fallback) or "attachment"
    name = name.replace("\\", "_").replace("/", "_").strip().strip(".")
    return name[:180] or "attachment"


def _extension_of(name_or_url: str) -> str:
    path = urlparse(name_or_url).path if "://" in name_or_url else name_or_url
    return Path(unquote(path)).suffix.lower()


def _kind_of(extension: str) -> str:
    if extension == ".pdf":
        return "pdf"
    if extension in {".doc", ".docx"}:
        return "document"
    if extension in {".xls", ".xlsx", ".csv"}:
        return "spreadsheet"
    if extension in {".ppt", ".pptx"}:
        return "presentation"
    if extension in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}:
        return "image"
    if extension in {".zip", ".rar", ".7z"}:
        return "archive"
    return "file"


def _safe_filename(name: str, fallback: str = "attachment") -> str:
    name = _clean_text(name).replace("\\", "_").replace("/", "_").strip()
    name = re.sub(r"[\x00-\x1f<>:\"|?*]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    return name[:180]


# ---------------------------------------------------------------------------
# 字段契约：决定哪些错误必须阻断、哪些字段可以降级
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldContract:
    """条目字段契约。

    ``required``
        必填字段；缺失或非法会阻断**当前条目**（不影响同页其他条目）。
    ``block_on_invalid``
        额外要求“只要出现且非法就阻断”的字段（默认等于 ``required``）。
    ``nullable``
        允许为空的字段；这里的字段缺失时安静降级为 ``None``。非 nullable 的
        可选字段缺失会记一条 warning，但仍产出条目。
    ``guaranteed``
        无论是否成功抽取都保证存在于条目中的字段，默认全部规范字段。

    注意：契约只决定“单条记录是否收录”，从不决定“整页/整批是否失败”。
    """

    required: frozenset[str] = field(default_factory=lambda: frozenset({"title"}))
    block_on_invalid: frozenset[str] | None = None
    nullable: frozenset[str] = field(default_factory=lambda: frozenset(CANONICAL_FIELDS))
    guaranteed: frozenset[str] = field(default_factory=lambda: frozenset(CANONICAL_FIELDS))

    def __post_init__(self) -> None:
        for name in ("required", "nullable", "guaranteed"):
            value = getattr(self, name)
            if isinstance(value, str):
                value = frozenset({value})
            else:
                value = frozenset(value)
            unknown = set(value) - set(CANONICAL_FIELDS)
            if unknown:
                raise ValueError(f"字段契约包含未知字段：{sorted(unknown)}")
            object.__setattr__(self, name, value)
        if self.block_on_invalid is None:
            object.__setattr__(self, "block_on_invalid", self.required)
        else:
            value = self.block_on_invalid
            if isinstance(value, str):
                value = frozenset({value})
            else:
                value = frozenset(value)
            unknown = set(value) - set(CANONICAL_FIELDS)
            if unknown:
                raise ValueError(f"字段契约包含未知字段：{sorted(unknown)}")
            object.__setattr__(self, "block_on_invalid", value)

    @property
    def blockers(self) -> frozenset[str]:
        """缺失或非法时必须阻断当前条目的字段。"""
        return frozenset(self.required) | frozenset(self.block_on_invalid or ())

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "required": sorted(self.required),
            "block_on_invalid": sorted(self.block_on_invalid or ()),
            "nullable": sorted(self.nullable),
            "guaranteed": sorted(self.guaranteed),
        }


DEFAULT_CONTRACT = FieldContract()

def _coerce_contract(contract: FieldContract | Mapping[str, Any] | None) -> FieldContract:
    if contract is None:
        return DEFAULT_CONTRACT
    if isinstance(contract, FieldContract):
        return contract
    if isinstance(contract, Mapping):
        return FieldContract(
            required=frozenset(contract.get("required", ("title",))),
            block_on_invalid=(
                None
                if contract.get("block_on_invalid") is None
                else frozenset(contract["block_on_invalid"])
            ),
            nullable=frozenset(contract.get("nullable", CANONICAL_FIELDS)),
            guaranteed=frozenset(contract.get("guaranteed", CANONICAL_FIELDS)),
        )
    raise TypeError("contract 必须是 FieldContract 或 mapping")


# ---------------------------------------------------------------------------
# 表头映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderMap:
    """一次表头映射的结果。"""

    mapping: dict[str, int]
    headers: tuple[str, ...]
    unmatched: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    matched_by: dict[str, str] = field(default_factory=dict)
    #: 由表头声明的数值单位，如「标的金额（万元）」→ amount: 10000。
    scales: dict[str, int] = field(default_factory=dict)

    def get(self, field: str, default: int | None = None) -> int | None:
        return self.mapping.get(field, default)

    def __getitem__(self, field: str) -> int:
        return self.mapping[field]

    def __contains__(self, field: str) -> bool:
        return field in self.mapping

    def __iter__(self):
        return iter(self.mapping)

    def __len__(self) -> int:
        return len(self.mapping)

    def items(self):
        return self.mapping.items()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mapping": dict(self.mapping),
            "headers": list(self.headers),
            "unmatched": list(self.unmatched),
            "duplicates": list(self.duplicates),
            "matched_by": dict(self.matched_by),
            "scales": dict(self.scales),
        }


_ALIAS_LOOKUP: dict[str, str] = {}
for _field_name, _aliases in HEADER_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_norm_header(_alias)] = _field_name
    _ALIAS_LOOKUP[_norm_header(_field_name)] = _field_name
# 明确区分容易混淆的别名（“编号”优先视为 entry_id 而非 case_no）。
_ALIAS_LOOKUP.update(
    {
        "编号": "entry_id",
        "序号": "entry_id",
        "标题": "title",
        "名称": "title",
        "日期": "publish_date",
        "时间": "publish_date",
        "法院": "court",
        "单位": "court",
        "案号": "case_no",
        "主体": "subject",
        "金额": "amount",
        "链接": "detail_url",
        "附件": "attachments",
    }
)


# OCR / 人工改字造成的常见表头变体（定向容错，不扩大模糊匹配范围）。
_HEADER_VARIANTS = {
    "公告标提": "title",
    "标提": "title",
    "案件号": "case_no",
    "发布曰期": "publish_date",
    "公告曰期": "publish_date",
    "公示曰期": "publish_date",
}
for _variant, _field_name in _HEADER_VARIANTS.items():
    _ALIAS_LOOKUP.setdefault(_norm_header(_variant), _field_name)


def canonical_header(name: Any) -> str | None:
    """把单个表头名映射到规范字段名；无法识别返回 ``None``。

    兼容：全半角、大小写、空白/标点、括号单位、中英文别名、模糊拼写。
    """
    text = _norm_header(name)
    if not text:
        return None
    if text in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[text]
    # 包含匹配：表头在别名之外多带了修饰/单位，如「公告标题（必填）」
    # 「标的金额（万元）」。只允许「别名 ⊆ 表头」，避免把短表头（source）
    # 误配到长别名（source url）。
    candidates: list[tuple[int, str]] = []
    for alias, field_name in _ALIAS_LOOKUP.items():
        if len(alias) >= 2 and alias in text and len(text) > len(alias):
            candidates.append((len(alias), field_name))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1]
    # 模糊匹配：容忍 OCR / 人工改字造成的轻微差异（短字符串不参与，避免误配）。
    best_field: str | None = None
    best_ratio = 0.0
    for alias, field_name in _ALIAS_LOOKUP.items():
        if len(alias) < 3 or len(text) < 3:
            continue
        # 长度差距过大时不做模糊匹配，避免短词吸附到长别名。
        if abs(len(text) - len(alias)) > max(1, len(alias) // 4):
            continue
        ratio = difflib.SequenceMatcher(None, text, alias).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_field = field_name
    if best_ratio >= 0.78:
        return best_field
    return None


_HEADER_SCALE_RE = re.compile(r"(亿元|万元|亿|万|元)")


def _header_scale(label: Any) -> int:
    """从表头文字里读取金额单位（亿元/万元/...），默认 1。"""
    match = _HEADER_SCALE_RE.search(_clean_text(label))
    if not match:
        return 1
    return {
        "亿元": 100000000,
        "亿": 100000000,
        "万元": 10000,
        "万": 10000,
        "元": 1,
    }[match.group(1)]


def map_header(headers: Sequence[Any]) -> HeaderMap:
    """把一行表头映射成 ``{规范字段: 列下标}``。

    同一规范字段出现多列时保留第一列并把后续列记为 duplicates；未知列记入
    unmatched，不阻断整张表。空表头会被忽略。
    """
    mapping: dict[str, int] = {}
    unmatched: list[str] = []
    duplicates: list[str] = []
    matched_by: dict[str, str] = {}
    scales: dict[str, int] = {}
    clean_headers: list[str] = []
    for index, value in enumerate(headers):
        label = _clean_text(value)
        clean_headers.append(label)
        field_name = canonical_header(value)
        if field_name is None:
            if label:
                unmatched.append(label)
            continue
        if field_name in mapping:
            duplicates.append(label)
            continue
        mapping[field_name] = index
        matched_by[field_name] = label
        if field_name == "amount":
            scales[field_name] = _header_scale(label)
    return HeaderMap(
        mapping=mapping,
        headers=tuple(clean_headers),
        unmatched=tuple(unmatched),
        duplicates=tuple(duplicates),
        matched_by=matched_by,
        scales=scales,
    )


def _looks_like_header(cells: Sequence[Any]) -> bool:
    """判断一行是否“看起来像表头”。

    比单纯的别名命中更严格，避免把数据行（如「乙法院」「not-money」）误判为
    表头：要求同时满足
      * 至少命中一个强别名（精确别名，而非包含/模糊匹配）；
      * 且命中 title，或命中 ≥2 个规范字段；
      * 且该行没有明显的“数据特征”（日期、金额、URL……）。
    """
    texts = [_clean_text(cell) for cell in cells]
    fields = {canonical_header(text) for text in texts if text}
    fields.discard(None)
    if not fields:
        return False
    strong = sum(1 for text in texts if text and _norm_header(text) in _ALIAS_LOOKUP)
    if strong == 0:
        return False
    if "title" not in fields and len(fields) < 2:
        return False
    data_like = sum(
        1
        for text in texts
        if text and (_looks_like_date(text) or _looks_like_url(text) or _looks_like_amount(text) or _CASE_NO_RE.search(text))
    )
    if data_like > 0:
        return False
    return True


def _cells_are_header(cells: Sequence[Any]) -> bool:
    texts = [_cell_text(cell) for cell in cells]
    if not any(_norm_header(t) for t in texts):
        return False
    return _looks_like_header(texts)


# ---------------------------------------------------------------------------
# 结果对象与错误
# ---------------------------------------------------------------------------


def _issue(
    code: str,
    message: str,
    *,
    severity: str,
    blocking: bool,
    source: str,
    batch_id: str,
    page: int | None = None,
    row: int | None = None,
    table: int | None = None,
    field_name: str | None = None,
    entry_key: str | None = None,
    raw: Any = None,
    attempts: int | None = None,
    stage: str = "gazette_parse",
    locator: Mapping[str, Any] | None = None,
    fatal: bool = False,
) -> dict[str, Any]:
    loc: dict[str, Any] = {
        "source": source,
        "batch_id": batch_id,
        "page": page,
        "row": row,
        "table": table,
    }
    if locator:
        loc.update({k: v for k, v in locator.items() if v is not None})
    issue = {
        "stage": stage,
        "code": code,
        "message": message,
        "severity": severity,
        "blocking": bool(blocking),
        "fatal": bool(fatal),
        "source": source,
        "batch_id": batch_id,
        "page": page,
        "row": row,
        "table": table,
        "field": field_name,
        "entry_key": entry_key,
        "attempts": attempts,
        "locator": loc,
    }
    if raw is not None:
        issue["raw"] = _truncate(raw, 300)
    return issue


@dataclass
class ParseResult:
    """一次公示栏解析的完整产物；三路数据彼此分离。

    * ``entries`` → 交给 dedupe / keyer / store
    * ``attachments`` → 交给 attach / store（附件线索；下载失败不阻断条目）
    * ``errors`` / ``warnings`` → 交给 store 的错误表或告警通道

    结果对象可像条目列表一样迭代/索引（``list(result)``、``result[0]``），
    也可用 ``result["entries"]`` 等键访问，便于不同调用方使用。
    """

    source: str
    batch_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    contract: FieldContract = DEFAULT_CONTRACT

    # -- 常用视图 ---------------------------------------------------------
    @property
    def issues(self) -> list[dict[str, Any]]:
        return [*self.errors, *self.warnings]

    @property
    def success_count(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        """批次级成功：没有 fatal 错误。

        局部坏行、坏页、坏表的 ``blocking`` 只作用于该行/该表，不算整批失败；
        只有 ``document_unrecognized`` / ``batch_all_pages_failed`` 这类 fatal
        错误才会让 ``ok`` 为 False。
        """
        return not any(issue.get("fatal") for issue in self.errors)

    @property
    def complete(self) -> bool:
        """整批没有任何错误（含局部坏行）。"""
        return not self.errors

    @property
    def failed_locations(self) -> list[dict[str, Any]]:
        """所有失败位置（含 fatal），供核对成功条目数与失败位置。"""
        return [
            {
                "code": issue.get("code"),
                "source": issue.get("source"),
                "batch_id": issue.get("batch_id"),
                "page": issue.get("page"),
                "row": issue.get("row"),
                "table": issue.get("table"),
                "field": issue.get("field"),
                "blocking": issue.get("blocking"),
                "fatal": issue.get("fatal"),
            }
            for issue in self.errors
        ]

    # -- 三路交付 ---------------------------------------------------------
    def to_dedupe(self) -> list[dict[str, Any]]:
        return self.entries

    def to_attach(self) -> list[dict[str, Any]]:
        return self.attachments

    def to_store(self) -> dict[str, Any]:
        return {
            "batch": self.batch_meta(),
            "entries": self.entries,
            "attachments": self.attachments,
            "errors": self.errors,
            "warnings": self.warnings,
            "pages": self.pages,
            "stats": self.stats,
        }

    def routes(self) -> dict[str, Any]:
        """显式的下游交付契约，避免条目/附件/错误混写。"""
        return {
            "dedupe": self.to_dedupe(),
            "attach": self.to_attach(),
            "store": self.to_store(),
        }

    handoff = routes

    def batch_meta(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "batch_id": self.batch_id,
            "parser_version": PARSER_VERSION,
            "contract": self.contract.as_dict(),
            "stats": dict(self.stats),
            "metadata": _jsonable(self.metadata),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "batch_id": self.batch_id,
            "parser_version": PARSER_VERSION,
            "entries": self.entries,
            "attachments": self.attachments,
            "errors": self.errors,
            "warnings": self.warnings,
            "pages": self.pages,
            "stats": self.stats,
            "metadata": _jsonable(self.metadata),
            "contract": self.contract.as_dict(),
        }

    # -- 容器协议 ---------------------------------------------------------
    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.as_dict()[item]
        return self.entries[item]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __bool__(self) -> bool:
        return bool(self.entries)


PARSER_VERSION = "gazette_parse/2"


# ---------------------------------------------------------------------------
# 单元格 / 行规范化
# ---------------------------------------------------------------------------


@dataclass
class _Cell:
    text: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    attachment_hints: list[dict[str, Any]] = field(default_factory=list)
    is_header: bool = False
    raw: Any = None


def _cell_text(cell: Any) -> str:
    if isinstance(cell, _Cell):
        return cell.text
    if isinstance(cell, Mapping):
        for key in ("text", "value", "label", "content"):
            if key in cell:
                return _clean_text(cell[key])
        return ""
    if isinstance(cell, (list, tuple)):
        return " ".join(_cell_text(part) for part in cell if not isinstance(part, Mapping))
    return _clean_text(cell)


def _as_cell(value: Any) -> _Cell:
    if isinstance(value, _Cell):
        return value
    if isinstance(value, Mapping):
        text = ""
        for key in ("text", "value", "label", "content"):
            if key in value:
                text = _clean_text(value[key])
                break
        links: list[dict[str, str]] = []
        raw_links = value.get("links")
        if isinstance(raw_links, (list, tuple)):
            for link in raw_links:
                if isinstance(link, Mapping) and link.get("url"):
                    links.append(
                        {
                            "url": _clean_text(link["url"]),
                            "text": _clean_text(link.get("text") or link.get("label")),
                            "download": str(link.get("download", "")),
                        }
                    )
                elif isinstance(link, str):
                    links.append({"url": _clean_text(link), "text": ""})
        if value.get("href"):
            links.append(
                {
                    "url": _clean_text(value["href"]),
                    "text": _clean_text(value.get("link_text") or value.get("text") or ""),
                    "download": str(value.get("download", "")),
                }
            )
        hints = value.get("attachments")
        attachment_hints = _normalise_attachment_hints(hints)
        if not text and not links and not attachment_hints:
            text = _clean_text(value)
        return _Cell(text=text, links=links, attachment_hints=attachment_hints, raw=value)
    if isinstance(value, (list, tuple)):
        parts = [_as_cell(part) for part in value]
        text = " ".join(part.text for part in parts if part.text)
        links = [link for part in parts for link in part.links]
        hints = [hint for part in parts for hint in part.attachment_hints]
        return _Cell(text=_clean_text(text), links=links, attachment_hints=hints, raw=value)
    return _Cell(text=_clean_text(value), raw=value)


def _normalise_attachment_hints(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    elif isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    hints: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            url = _clean_text(item.get("url") or item.get("href") or item.get("src"))
            name = _clean_text(item.get("filename") or item.get("name") or item.get("text"))
            if url or name:
                hints.append(
                    {
                        "url": url,
                        "filename": name,
                        "label": _clean_text(item.get("label") or name),
                        "download": str(item.get("download", "")),
                    }
                )
        elif isinstance(item, str) and _clean_text(item):
            hints.append(
                {"url": _clean_text(item), "filename": "", "label": _clean_text(item), "download": ""}
            )
    return hints


# ---------------------------------------------------------------------------
# HTML 表格抽取（无第三方依赖，容忍坏标记）
# ---------------------------------------------------------------------------


class _TableHTMLParser(HTMLParser):
    """宽容的 HTML 表格抽取器。

    只关心 ``table / tr / td / th / a / caption``；嵌套表格独立成表，不污染
    外层单元格。遇到不闭合标签由 ``HTMLParser`` 自身容错。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []

    # -- 表格 -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            table = {
                "headers": None,
                "rows": [],
                "caption": "",
                "attrs": attrs_dict,
                "row": None,
                "cell": None,
                "cell_tag": None,
                "pending": [],  # rowspan 占位：(剩余行数, 列, cell)
                "in_caption": False,
            }
            self.tables.append(table)
            self._open.append(table)
            return
        if not self._open:
            return
        table = self._open[-1]
        if tag == "caption":
            table["in_caption"] = True
            return
        if tag == "tr":
            self._start_row(table)
        elif tag in {"td", "th"}:
            self._start_cell(table, tag, attrs_dict)
        elif tag == "a" and table["cell"] is not None:
            href = _clean_text(attrs_dict.get("href"))
            if href:
                table["cell"].links.append(
                    {
                        "url": href,
                        "text": "",
                        "download": attrs_dict.get("download", ""),
                    }
                )
        elif tag == "br" and table["cell"] is not None:
            table["cell"].text += " "

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self._open:
            return
        table = self._open[-1]
        if tag == "table":
            self._close_table(table)
            self._open.pop()
            return
        if tag == "caption":
            table["in_caption"] = False
        elif tag in {"td", "th"}:
            self._close_cell(table)
        elif tag == "tr":
            self._close_row(table)

    def handle_data(self, data: str) -> None:
        if not self._open:
            return
        table = self._open[-1]
        if table["in_caption"]:
            table["caption"] += data
            return
        if table["cell"] is not None:
            raw = _clean_text(data)
            if raw:
                table["cell"].text = (table["cell"].text + " " + raw).strip()
            if table["cell"].links:
                # 链接文字归属于最近一个未命名链接。
                for link in reversed(table["cell"].links):
                    if not link["text"]:
                        link["text"] = raw
                        break

    # -- 行/单元格 ---------------------------------------------------------
    def _start_row(self, table: dict[str, Any]) -> None:
        if table["row"] is not None:
            self._close_row(table)
        table["row"] = []
        table["row_tag"] = None
        table["cell"] = None
        # 填入上一个 rowspan 遗留的占位（按列号升序，保持表格结构）。
        remaining: list[tuple[int, int, _Cell]] = []
        for count, col, cell in sorted(table["pending"], key=lambda item: item[1]):
            while len(table["row"]) < col:
                table["row"].append(_Cell())
            if len(table["row"]) == col:
                table["row"].append(cell)
            if count - 1 > 0:
                remaining.append((count - 1, col, cell))
        table["pending"] = remaining

    def _start_cell(
        self, table: dict[str, Any], tag: str, attrs: dict[str, str]
    ) -> None:
        if table["row"] is None:
            self._start_row(table)
        self._close_cell(table)
        cell = _Cell()
        if attrs.get("data-field"):
            cell.text = ""  # data-field 只是提示，不当作单元格文本
            cell.__dict__["field_hint"] = _clean_text(attrs["data-field"])
        if attrs.get("colspan"):
            cell.__dict__["colspan"] = attrs["colspan"]
        if attrs.get("rowspan"):
            cell.__dict__["rowspan"] = attrs["rowspan"]
        table["cell"] = cell
        table["cell_tag"] = tag
        table["row"].append(cell)
        if tag == "th" and table["row_tag"] is None:
            table["row_tag"] = "th"

    def _close_cell(self, table: dict[str, Any]) -> None:
        cell = table["cell"]
        if cell is None:
            return
        table["cell"] = None
        colspan_raw = cell.__dict__.pop("colspan", "")
        rowspan_raw = cell.__dict__.pop("rowspan", "")
        cell.__dict__.pop("field_hint", None)
        try:
            colspan = max(1, int(str(colspan_raw).strip() or 1))
        except ValueError:
            colspan = 1
        try:
            rowspan = max(1, int(str(rowspan_raw).strip() or 1))
        except ValueError:
            rowspan = 1
        col = len(table["row"]) - 1
        for _ in range(colspan - 1):
            table["row"].append(_Cell())
        if rowspan > 1:
            # 占位符把该单元格延续到后续行（colspan 已展开的列都算同一逻辑列）。
            table["pending"].append((rowspan - 1, col, cell))

    def _close_row(self, table: dict[str, Any]) -> None:
        self._close_cell(table)
        row = table["row"]
        if row is None:
            return
        table["row"] = None
        if any(cell.text or cell.links or cell.attachment_hints for cell in row):
            if table["row_tag"] == "th" and table["headers"] is None:
                table["headers"] = row
            else:
                table["rows"].append(row)
        table["row_tag"] = None

    def _close_table(self, table: dict[str, Any]) -> None:
        if table["row"] is not None or table["cell"] is not None:
            # 文档在这里截断：最后一行没有正常闭合。
            table["truncated"] = True
        if table["row"] is not None:
            self._close_row(table)
        table["caption"] = _clean_text(table["caption"])


def extract_html_tables(html: Any, base_url: str | None = None) -> list[dict[str, Any]]:
    """从 HTML 文本抽取所有表格，返回 ``[{headers, rows, caption, attrs}]``。"""
    text = str(html or "")
    parser = _TableHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser 极少抛出，保底不中断批次
        pass
    # 截断的 HTML（缺少 </table> / </tr>）不会触发 endtag：在这里收尾，
    # 保证坏表的最后一行（哪怕只有半行）也进入行级校验，而不是静默丢失。
    while parser._open:
        table = parser._open.pop()
        try:
            parser._close_table(table)
        except Exception:  # pragma: no cover
            pass
    tables: list[dict[str, Any]] = []
    for raw in parser.tables:
        headers = raw["headers"]
        rows = raw["rows"]
        if headers is None and rows:
            # 无 th 时，若首行像表头则提升为表头。
            first = rows[0]
            if _cells_are_header(first):
                headers = first
                rows = rows[1:]
        if not headers and not rows:
            continue
        tables.append(
            {
                "headers": headers,
                "rows": rows,
                "caption": raw["caption"],
                "attrs": raw["attrs"],
                "base_url": base_url,
                "truncated": bool(raw.get("truncated")),
            }
        )
    return tables


# ---------------------------------------------------------------------------
# 行 -> 条目
# ---------------------------------------------------------------------------


@dataclass
class _ParseContext:
    source: str
    batch_id: str
    page: int | None
    table: int | None
    base_url: str | None
    contract: FieldContract
    method: str
    header_map: HeaderMap | None = None
    legacy: bool = False


def _attachment_from_link(
    link: Mapping[str, Any],
    ctx: _ParseContext,
    entry_key: str | None,
    row: int | None,
) -> dict[str, Any] | None:
    url_text = _clean_text(link.get("url"))
    if not url_text:
        return None
    try:
        absolute, resolved = _resolve_url(url_text, ctx.base_url)
    except _InvalidValue:
        return None
    label = _clean_text(link.get("text") or link.get("label"))
    filename = _guess_filename(absolute, label)
    extension = _extension_of(filename) or _extension_of(absolute)
    return {
        "entry_key": entry_key,
        "source": ctx.source,
        "batch_id": ctx.batch_id,
        "page": ctx.page,
        "row": row,
        "table": ctx.table,
        "url": absolute,
        "resolved": resolved,
        "label": label or filename,
        "filename": filename,
        "extension": extension,
        "kind": _kind_of(extension),
        "download_attr": bool(_clean_text(link.get("download"))),
        "status": "pending",
        "locator": {
            "source": ctx.source,
            "batch_id": ctx.batch_id,
            "page": ctx.page,
            "row": row,
            "table": ctx.table,
        },
    }


def _collect_attachments(
    attachments_cell: _Cell | None,
    row_cells: Sequence[_Cell],
    ctx: _ParseContext,
    entry_key: str | None,
    row: int | None,
) -> list[dict[str, Any]]:
    """收集附件线索：附件列文本/链接 + 任意单元格中的下载链接。"""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, label: str, download: str = "") -> None:
        clue = _attachment_from_link(
            {"url": url, "text": label, "download": download}, ctx, entry_key, row
        )
        if clue and clue["url"] not in seen:
            seen.add(clue["url"])
            found.append(clue)

    if attachments_cell is not None:
        for hint in attachments_cell.attachment_hints:
            if hint.get("url"):
                add(hint["url"], hint.get("filename") or hint.get("label", ""), hint.get("download", ""))
            elif hint.get("filename"):
                # 只有文件名没有链接：保留线索，交给 attach 阶段补链。
                filename = _safe_filename(hint["filename"])
                found.append(
                    {
                        "entry_key": entry_key,
                        "source": ctx.source,
                        "batch_id": ctx.batch_id,
                        "page": ctx.page,
                        "row": row,
                        "table": ctx.table,
                        "url": None,
                        "resolved": False,
                        "label": hint.get("label") or filename,
                        "filename": filename,
                        "extension": _extension_of(filename),
                        "kind": _kind_of(_extension_of(filename)),
                        "download_attr": False,
                        "status": "unresolved",
                        "locator": {
                            "source": ctx.source,
                            "batch_id": ctx.batch_id,
                            "page": ctx.page,
                            "row": row,
                            "table": ctx.table,
                        },
                    }
                )
        for link in attachments_cell.links:
            add(link.get("url", ""), link.get("text", ""), link.get("download", ""))
        # 文本列中的裸链接 / 文件链接。
        for token in re.findall(r"(?:https?://|//|/)[^\s，,；;）)】\]]+", attachments_cell.text):
            if _looks_like_url(token) or _extension_of(token) in _ATTACHMENT_EXTENSIONS:
                add(token, "")
        # 附件列的纯文件名（每行一个/顿号分隔）。
        if not found:
            for piece in re.split(r"[、,，;；\n|]+", attachments_cell.text):
                piece = _clean_text(piece)
                if piece and _extension_of(piece) in _ATTACHMENT_EXTENSIONS:
                    filename = _safe_filename(piece)
                    found.append(
                        {
                            "entry_key": entry_key,
                            "source": ctx.source,
                            "batch_id": ctx.batch_id,
                            "page": ctx.page,
                            "row": row,
                            "table": ctx.table,
                            "url": None,
                            "resolved": False,
                            "label": filename,
                            "filename": filename,
                            "extension": _extension_of(filename),
                            "kind": _kind_of(_extension_of(filename)),
                            "download_attr": False,
                            "status": "unresolved",
                            "locator": {
                                "source": ctx.source,
                                "batch_id": ctx.batch_id,
                                "page": ctx.page,
                                "row": row,
                                "table": ctx.table,
                            },
                        }
                    )

    # 其他单元格里的下载链接（带 download 属性或指向附件扩展名）。
    for cell in row_cells:
        if cell is attachments_cell:
            continue  # 附件列已在上面的分支里处理，避免同一链接重复入列。
        for link in cell.links:
            url = _clean_text(link.get("url"))
            if not url:
                continue
            extension = _extension_of(url)
            if _clean_text(link.get("download")) or extension in _ATTACHMENT_EXTENSIONS:
                add(url, link.get("text", ""), link.get("download", ""))

    return found


def _cell_for(field_name: str, cells: Sequence[_Cell], ctx: _ParseContext) -> _Cell:
    if ctx.header_map is not None:
        index = ctx.header_map.get(field_name)
        if index is not None and index < len(cells):
            return cells[index]
        return _Cell()
    # 旧格式：位置兜底。
    for name, index in LEGACY_POSITIONS:
        if name == field_name:
            if index < len(cells):
                return cells[index]
            return _Cell()
    return _Cell()


def _ok_entry_key(source: str, fields: Mapping[str, Any]) -> str:
    """稳定内容键：与分页顺序、抓取批次、行号无关，重复抓取得到相同键。

    只用公告内容（标题/日期/案号/法院/主体/金额/详情链接）派生，不用页码、
    行号或批次，因此同一公告无论重复抓取还是跨页重复都得到同一键。链接只去掉
    ``#fragment``，保留查询参数（查询参数常用于区分类似公告）。
    """
    detail_url = (fields.get("detail_url") or "").split("#", 1)[0]
    payload = {
        "source": source,
        "title": fields.get("title") or "",
        "publish_date": fields.get("publish_date") or "",
        "case_no": fields.get("case_no") or "",
        "court": fields.get("court") or "",
        "announcement_type": fields.get("announcement_type") or "",
        "subject": fields.get("subject") or "",
        "amount": fields.get("amount"),
        "detail_url": detail_url,
    }
    return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True))[:20]


def _validate_field(
    field_name: str,
    raw_text: str,
    ctx: _ParseContext,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 ``(值, blocking_errors, warnings)``。

    契约决定：必填/必须校验字段失败 → blocking error（仅该条目）；其余失败 →
    warning + ``None``（降级）。结构性问题永远阻断当前条目。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    contract = ctx.contract
    is_blank = _is_null(raw_text)
    if is_blank:
        # 空白只对 ``required`` 字段阻断；``block_on_invalid`` 针对的是“有值但非法”。
        blocking = field_name in contract.required
        if blocking:
            code = "required_field_missing"
        elif field_name in contract.nullable:
            return None, errors, warnings
        else:
            code = "field_value_missing"
        issue = _issue(
            code,
            f"字段 {field_name} 为空"
            + ("（必填，阻断该条目）" if blocking else "（按契约降级为空）"),
            severity="error" if blocking else "warning",
            blocking=blocking,
            source=ctx.source,
            batch_id=ctx.batch_id,
            page=ctx.page,
            row=None,  # 由调用方填充具体行号
            table=ctx.table,
            field_name=field_name,
        )
        if issue["blocking"]:
            errors.append(issue)
        else:
            warnings.append(issue)
        return None, errors, warnings

    value: Any = raw_text
    try:
        if field_name == "publish_date":
            value = _parse_date(raw_text)
        elif field_name == "amount":
            value = _parse_amount(raw_text)
            scale = 1
            if ctx.header_map is not None:
                scale = ctx.header_map.scales.get("amount", 1)
            if scale != 1 and not _AMOUNT_HAS_UNIT_RE.search(raw_text):
                # 表头声明单位（如「金额（万元）」）而单元格只有数字时换算；
                # 用 Decimal 避免浮点误差。
                scaled = Decimal(str(value)) * scale
                value = int(scaled) if scaled == scaled.to_integral_value() else float(scaled)
        elif field_name == "detail_url":
            value, _ = _resolve_url(raw_text, ctx.base_url)
        elif field_name in {"title", "court", "subject", "announcement_type"}:
            value = _clean_text(raw_text)
        elif field_name == "case_no":
            value = _clean_text(raw_text)
        elif field_name == "entry_id":
            value = _clean_text(raw_text)
        else:
            value = _clean_text(raw_text)
    except _InvalidValue as exc:
        blocking = field_name in contract.blockers
        issue = _issue(
            exc.code,
            f"字段 {field_name} 非法：{exc.message}",
            severity="error" if blocking else "warning",
            blocking=blocking,
            source=ctx.source,
            batch_id=ctx.batch_id,
            page=ctx.page,
            row=None,
            table=ctx.table,
            field_name=field_name,
            raw=raw_text,
        )
        if blocking:
            errors.append(issue)
        else:
            warnings.append(issue)
        return None, errors, warnings

    if field_name == "amount" and value is None:
        value = None
    return value, errors, warnings


def _build_entry(
    cells: Sequence[_Cell],
    ctx: _ParseContext,
    *,
    row_number: int,
    raw_row: Any,
    row_notes: Sequence[dict[str, Any]] = (),
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """从一行单元格构建条目；错误只针对该行。"""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    degraded: list[str] = []
    missing: list[str] = []
    attachments_cell = _cell_for("attachments", cells, ctx)
    attachments = _collect_attachments(attachments_cell, cells, ctx, None, row_number)

    if ctx.header_map is not None:
        available = set(ctx.header_map.mapping)
    else:
        available = {name for name, _ in LEGACY_POSITIONS}

    for field_name in CANONICAL_FIELDS:
        if field_name not in available:
            if field_name in ctx.contract.required:
                errors.append(
                    _issue(
                        "required_field_missing",
                        f"表头缺少必填字段 {field_name}",
                        severity="error",
                        blocking=True,
                        source=ctx.source,
                        batch_id=ctx.batch_id,
                        page=ctx.page,
                        row=row_number,
                        table=ctx.table,
                        field_name=field_name,
                    )
                )
            elif field_name in ctx.contract.nullable:
                values[field_name] = None
            else:
                values[field_name] = None
                warnings.append(
                    _issue(
                        "field_not_in_layout",
                        f"版式中没有字段 {field_name}，已降级为空",
                        severity="warning",
                        blocking=False,
                        source=ctx.source,
                        batch_id=ctx.batch_id,
                        page=ctx.page,
                        row=row_number,
                        table=ctx.table,
                        field_name=field_name,
                    )
                )
            continue
        cell = _cell_for(field_name, cells, ctx)
        value, field_errors, field_warnings = _validate_field(field_name, cell.text, ctx)
        for issue in field_errors:
            issue["row"] = row_number
            issue["entry_key"] = None
            issue["locator"]["row"] = row_number
            errors.append(issue)
        for issue in field_warnings:
            issue["row"] = row_number
            issue["locator"]["row"] = row_number
            warnings.append(issue)
        values[field_name] = value
        if value is None and field_name not in ctx.contract.required:
            degraded.append(field_name)
        if _is_null(cell.text):
            missing.append(field_name)

    for note in row_notes:
        warnings.append(note)

    if errors:
        return None, errors, warnings, attachments

    entry_key = _ok_entry_key(ctx.source, values)
    entry_id = values.get("entry_id")
    for clue in attachments:
        clue["entry_key"] = entry_key
    source_id = f"{ctx.source}:{entry_id}" if entry_id else entry_key
    entry: dict[str, Any] = {
        "id": entry_key,
        "entry_key": entry_key,
        "record_id": entry_key,
        "source_id": source_id,
        "source": ctx.source,
        "batch_id": ctx.batch_id,
        "page": ctx.page,
        "row": row_number,
        "table": ctx.table,
        "locator": {
            "source": ctx.source,
            "batch_id": ctx.batch_id,
            "page": ctx.page,
            "row": row_number,
            "table": ctx.table,
        },
        "extraction": {
            "method": ctx.method,
            "legacy": ctx.legacy,
            "truncated_row": any(
                note.get("code") == "row_truncated" for note in row_notes
            ),
            "header_matched": (
                dict(ctx.header_map.matched_by) if ctx.header_map is not None else {}
            ),
            "available_fields": sorted(available),
            "degraded_fields": sorted(set(degraded)),
            "blank_fields": sorted(set(missing)),
            "parser_version": PARSER_VERSION,
        },
        "attachments": attachments,
        "raw": _truncate(_row_snapshot(raw_row), 500),
    }
    for field_name in CANONICAL_FIELDS:
        entry[field_name] = values.get(field_name)
    if entry.get("detail_url"):
        entry["detail_url"], entry["detail_url_resolved"] = _resolve_url(
            entry["detail_url"], None
        )
    # 契约保证字段一定存在（即便降级为 None）。
    for field_name in ctx.contract.guaranteed:
        entry.setdefault(field_name, None)
    return entry, errors, warnings, attachments


def _row_snapshot(row: Any) -> str:
    if isinstance(row, Mapping):
        return json.dumps(_jsonable(row), ensure_ascii=False)
    if isinstance(row, (list, tuple)):
        return " | ".join(_cell_text(cell) for cell in row)
    return str(row)


def _parse_table(
    table: Mapping[str, Any],
    ctx: _ParseContext,
    result: ParseResult,
    *,
    page_number: int | None,
    first_row_number: int = 1,
) -> None:
    raw_headers = table.get("headers")
    header_cells: list[_Cell] | None = None
    if raw_headers:
        header_cells = [_as_cell(cell) for cell in raw_headers]
        hm = map_header([cell.text for cell in header_cells])
        if not hm.mapping:
            result.errors.append(
                _issue(
                    "table_header_unrecognized",
                    "表头无法识别任何规范字段，整表跳过",
                    severity="error",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                    raw=" | ".join(cell.text for cell in header_cells),
                )
            )
            return
        missing_required = sorted(set(ctx.contract.required) - set(hm.mapping))
        if missing_required:
            # 缺必填列 → 整表不可用；这是“必须阻断”的版式错误，但只影响本表。
            result.errors.append(
                _issue(
                    "table_missing_required_columns",
                    f"表头缺少必填列：{'、'.join(missing_required)}，整表跳过",
                    severity="error",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                    field_name=missing_required[0],
                    raw=" | ".join(cell.text for cell in header_cells),
                )
            )
            return
        ctx.header_map = hm
        ctx.method = "header"
        ctx.legacy = False
        if hm.unmatched:
            result.warnings.append(
                _issue(
                    "header_columns_unmatched",
                    f"表头有 {len(hm.unmatched)} 列未识别：{'、'.join(hm.unmatched[:6])}",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                )
            )
        if hm.duplicates:
            result.warnings.append(
                _issue(
                    "header_duplicate_columns",
                    f"表头重复列已忽略：{'、'.join(hm.duplicates[:6])}",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                )
            )

    rows = list(table.get("rows") or [])
    if header_cells is None:
        # 无显式表头：尝试把第一行当表头，否则走旧格式位置解析。
        if not rows:
            result.warnings.append(
                _issue(
                    "table_empty",
                    "表格没有数据行",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                )
            )
            return
        first_cells = [_as_cell(cell) for cell in rows[0]]
        if _cells_are_header([cell.text for cell in first_cells]):
            hm = map_header([cell.text for cell in first_cells])
            ctx.header_map = hm
            ctx.method = "header"
            ctx.legacy = False
            rows = rows[1:]
            first_row_number += 1
        else:
            ctx.header_map = None
            ctx.method = "legacy_position"
            ctx.legacy = True
            result.warnings.append(
                _issue(
                    "legacy_layout_assumed",
                    "未找到可识别表头，按旧格式位置解析",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    table=ctx.table,
                )
            )

    if ctx.header_map is not None:
        expected = len(ctx.header_map.headers)
    else:
        expected = max(LEGACY_POSITIONS, key=lambda pair: pair[1])[1] + 1

    truncated = bool(table.get("truncated"))
    last_index = len(rows) - 1
    for offset, row in enumerate(rows):
        row_number = first_row_number + offset
        notes: list[dict[str, Any]] = []
        if isinstance(row, Mapping):
            row = list(row.values())
        if not isinstance(row, (list, tuple)):
            result.errors.append(
                _issue(
                    "row_not_parseable",
                    f"第 {row_number} 行不是可解析的行结构",
                    severity="error",
                    blocking=True,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    row=row_number,
                    table=ctx.table,
                    raw=_row_snapshot(row),
                )
            )
            continue
        cells = [_as_cell(cell) for cell in row]
        if not any(cell.text or cell.links or cell.attachment_hints for cell in cells):
            continue  # 空行
        if _cells_are_header([cell.text for cell in cells]) and ctx.header_map is not None:
            notes.append(
                _issue(
                    "repeated_header_skipped",
                    f"第 {row_number} 行重复表头，已跳过",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    row=row_number,
                    table=ctx.table,
                )
            )
            result.warnings.extend(notes)
            continue
        if len(cells) < expected:
            notes.append(
                _issue(
                    "row_short",
                    f"第 {row_number} 行只有 {len(cells)} 列（期望 {expected}），缺失列按空值处理",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    row=row_number,
                    table=ctx.table,
                )
            )
        elif len(cells) > expected:
            notes.append(
                _issue(
                    "row_extra",
                    f"第 {row_number} 行有 {len(cells)} 列（期望 {expected}），多余列已忽略",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    row=row_number,
                    table=ctx.table,
                )
            )
        # 文档截断：最后一行可能是半行，缺失列按空值处理已经不安全 → 只警告但仍
        # 试图解析；若必填字段缺失会自然阻断该行，不影响本表其他行。
        if truncated and offset == last_index and len(cells) < expected:
            notes.append(
                _issue(
                    "row_truncated",
                    f"第 {row_number} 行位于被截断的表格末尾，按不完整行处理",
                    severity="warning",
                    blocking=False,
                    source=ctx.source,
                    batch_id=ctx.batch_id,
                    page=page_number,
                    row=row_number,
                    table=ctx.table,
                )
            )
        entry, errors, warnings, attachments = _build_entry(
            cells, ctx, row_number=row_number, raw_row=row, row_notes=notes
        )
        result.warnings.extend(warnings)
        if entry is None:
            for issue in errors:
                issue["severity"] = "error"
                result.errors.append(issue)
            result.stats["failed_rows"] = result.stats.get("failed_rows", 0) + 1
            continue
        result.entries.append(entry)
        result.attachments.extend(attachments)
        result.stats["parsed_rows"] = result.stats.get("parsed_rows", 0) + 1


# ---------------------------------------------------------------------------
# 页面 / 表格归一
# ---------------------------------------------------------------------------

_PAGE_KEYS = {"url", "href", "html", "tables", "rows", "headers", "page", "number", "error"}


def _decode_bytes(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _default_fetcher(url: str, *, timeout: float = 20.0) -> bytes:
    """离线优先的默认抓取器：本地路径/file:// 直接读，http(s) 走 httpx。

    网络错误原样抛出，由调用方的重试逻辑处理；不在此处吞异常。
    """
    parsed = urlparse(url)
    if parsed.scheme in {"", "file"}:
        path = Path(unquote(parsed.path)) if parsed.scheme else Path(url)
        return path.read_bytes()
    if parsed.scheme not in {"http", "https"}:
        raise GazetteParseError(f"不支持的抓取协议：{url}")
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出清晰错误
        raise GazetteParseError("抓取 http(s) 需要安装 httpx") from exc
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _call_with_retry(
    func: Callable[[], Any],
    *,
    attempts: int,
    backoff: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[Any, int, BaseException | None]:
    """执行 ``func``，失败按 ``attempts`` 重试；返回 ``(值, 次数, 最后异常)``。"""
    attempts = max(1, int(attempts))
    sleeper = sleep or time.sleep
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func(), attempt, None
        except Exception as exc:  # 页面/附件级失败在这里隔离
            last_error = exc
            if attempt < attempts and backoff > 0:
                sleeper(backoff * attempt)
    return None, attempts, last_error


def _normalise_rows(rows: Any) -> list[Any]:
    if rows is None:
        return []
    if isinstance(rows, Mapping):
        return [list(rows.values())]
    if not isinstance(rows, (list, tuple)):
        return [rows]
    return list(rows)


def _normalise_table(table: Any) -> dict[str, Any]:
    if isinstance(table, Mapping):
        headers = table.get("headers")
        if headers is None:
            headers = table.get("header") or table.get("columns")
        rows = _normalise_rows(table.get("rows", table.get("data", [])))
        caption = _clean_text(table.get("caption") or table.get("title") or "")
        attrs = table.get("attrs") if isinstance(table.get("attrs"), Mapping) else {}
        base_url = table.get("base_url")
    elif isinstance(table, (list, tuple)) and table and isinstance(table[0], (list, tuple)):
        headers = table[0]
        rows = [list(row) for row in table[1:]]
        caption, attrs, base_url = "", {}, None
    else:
        raise GazetteParseError("无法识别的表格结构")
    headers = list(headers) if headers is not None else None
    rows = [row for row in rows if isinstance(row, (list, tuple, Mapping))]
    if rows and all(isinstance(row, Mapping) for row in rows):
        if headers is None:
            headers = list(rows[0].keys())
        rows = [list(row.values()) for row in rows]
    return {
        "headers": headers,
        "rows": rows,
        "caption": caption,
        "attrs": dict(attrs),
        "base_url": base_url,
    }


def _normalise_page(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        text = item.lstrip()
        if _URL_RE.match(text) and not text.startswith("<"):
            return {"number": index, "url": text, "html": None, "tables": None, "rows": None}
        return {"number": index, "url": None, "html": item, "tables": None, "rows": None}
    if not isinstance(item, Mapping):
        raise GazetteParseError(f"无法识别的页面结构：{type(item).__name__}")
    number = item.get("number", item.get("page", index))
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = index
    page: dict[str, Any] = {
        "number": number,
        "url": item.get("url") or item.get("href"),
        "html": item.get("html"),
        "tables": item.get("tables"),
        "rows": item.get("rows"),
        "headers": item.get("headers"),
        "caption": item.get("caption"),
        "base_url": item.get("base_url"),
        "status": item.get("status"),
        "error": item.get("error"),
    }
    if (
        page["html"] is None
        and page["tables"] is None
        and page["rows"] is None
        and not page["url"]
        and not page["error"]
    ):
        raise GazetteParseError(f"第 {number} 页没有 html/tables/rows/url 内容")
    return page


def _document_pages(document: Any) -> tuple[list[dict[str, Any]], str, Mapping[str, Any]]:
    """把各种输入形态归一成页面列表；返回 ``(pages, method, metadata)``。"""
    metadata: dict[str, Any] = {}
    if isinstance(document, str):
        text = document.lstrip()
        if _URL_RE.match(text) and not text.startswith("<"):
            return [{"number": 1, "url": text, "html": None, "tables": None, "rows": None}], "html", metadata
        return [{"number": 1, "url": None, "html": document, "tables": None, "rows": None}], "html", metadata
    if isinstance(document, Mapping):
        for key in ("document", "board", "content", "body", "data"):
            if key in document and isinstance(document[key], (str, list, Mapping)):
                inner = document[key]
                pages, method, inner_meta = _document_pages(inner)
                metadata.update(inner_meta)
                for key_name in ("source", "batch_id", "base_url", "metadata"):
                    if key_name in document:
                        metadata[key_name] = document[key_name]
                return pages, method, metadata
        if "pages" in document:
            pages = [_normalise_page(item, i + 1) for i, item in enumerate(document["pages"] or [])]
            return pages, "pages", metadata
        if "html" in document:
            return [
                {
                    "number": 1,
                    "url": document.get("url"),
                    "html": document["html"],
                    "tables": None,
                    "rows": None,
                    "base_url": document.get("base_url"),
                }
            ], "html", metadata
        if "tables" in document:
            return [
                {
                    "number": 1,
                    "url": document.get("url"),
                    "html": None,
                    "tables": document["tables"],
                    "rows": None,
                    "base_url": document.get("base_url"),
                }
            ], "tables", metadata
        if "rows" in document:
            return [
                {
                    "number": 1,
                    "url": document.get("url"),
                    "html": None,
                    "tables": None,
                    "rows": document["rows"],
                    "headers": document.get("headers"),
                    "base_url": document.get("base_url"),
                }
            ], "rows", metadata
        # 单行字典：键即表头。
        if document and all(not isinstance(value, (Mapping, list, tuple, set)) for value in document.values()):
            return [
                {
                    "number": 1,
                    "url": None,
                    "html": None,
                    "tables": [
                        {"headers": list(document.keys()), "rows": [list(document.values())]}
                    ],
                    "rows": None,
                }
            ], "row_dict", metadata
        raise GazetteParseError("无法识别的公示栏文档结构")
    if isinstance(document, (list, tuple)):
        items = list(document)
        if not items:
            return [], "empty", metadata
        mappings = [item for item in items if isinstance(item, Mapping)]
        if mappings and all(
            any(key in item for key in _PAGE_KEYS)
            and not any(key in item for key in ("标题", "title", "公告名称"))
            for item in mappings
        ):
            return [_normalise_page(item, i + 1) for i, item in enumerate(items)], "pages", metadata
        if mappings and len(mappings) == len(items) and all(
            not any(key in item for key in ("html", "tables", "pages")) for item in mappings
        ):
            # 字典行列表：键就是表头。
            headers: list[str] = []
            for item in mappings:
                for key in item:
                    if key not in headers:
                        headers.append(str(key))
            table = {
                "headers": headers,
                "rows": [[item.get(key) for key in headers] for item in mappings],
                "caption": "",
                "attrs": {},
                "base_url": None,
            }
            return [
                {"number": 1, "url": None, "html": None, "tables": [table], "rows": None}
            ], "row_dicts", metadata
        if mappings and all(("headers" in item or "rows" in item) for item in mappings):
            return [
                {
                    "number": 1,
                    "url": None,
                    "html": None,
                    "tables": [_normalise_table(item) for item in mappings],
                    "rows": None,
                }
            ], "tables", metadata
        if all(isinstance(item, (list, tuple)) for item in items):
            first = items[0]
            if first and isinstance(first[0], (list, tuple)):
                # 每一项本身是“行列表” → 文档是表格列表。
                tables = [_normalise_table(item) for item in items]
            else:
                # 二维数组：整体是一张表，首行通常是表头。
                tables = [_normalise_table(items)]
            return [
                {
                    "number": 1,
                    "url": None,
                    "html": None,
                    "tables": tables,
                    "rows": None,
                }
            ], "tables", metadata
        return [
            {"number": 1, "url": None, "html": None, "tables": None, "rows": items}
        ], "rows", metadata
    raise GazetteParseError(f"无法识别的公示栏文档类型：{type(document).__name__}")


def _document_digest(document: Any) -> str:
    """批次默认标识：只由来源和文档内容派生，重复抓取同一内容得到同一批次。"""

    def reduce(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "..."
        if isinstance(value, str):
            return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
        if isinstance(value, bytes):
            return hashlib.sha256(value).hexdigest()[:16]
        if isinstance(value, Mapping):
            return {
                str(key): reduce(item, depth + 1)
                for key, item in value.items()
                if key not in {"batch_id", "metadata", "fetched_at", "request_id"}
            }
        if isinstance(value, (list, tuple)):
            return [reduce(item, depth + 1) for item in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)

    return _sha(json.dumps(reduce(document), ensure_ascii=False, sort_keys=True, default=str))[:16]


def make_batch_id(source: str, document: Any) -> str:
    """生成确定性批次号：``来源 + 内容摘要``。

    调用方可用显式 ``batch_id`` 覆盖（例如把补抓结果归入原批次）。
    """
    return f"{_slug(source)}-{_document_digest(document)}"


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and attrs_dict.get("href"):
            self._current = {
                "href": attrs_dict["href"],
                "rel": attrs_dict.get("rel", ""),
                "text": "",
                "class": attrs_dict.get("class", ""),
                "title": attrs_dict.get("title", ""),
            }
            self.links.append(self._current)
        elif tag == "link" and attrs_dict.get("href"):
            self.links.append(
                {
                    "href": attrs_dict["href"],
                    "rel": attrs_dict.get("rel", ""),
                    "text": "",
                    "class": "",
                    "title": "",
                }
            )

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] = (self._current["text"] + " " + data).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current = None


_NEXT_TEXTS = ("下一页", "下页", "后页", "next", "next page", "older", "»", "›", ">")


def _find_next_page(html: str, base_url: str | None) -> str | None:
    parser = _LinkParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:  # pragma: no cover
        return None
    candidates: list[tuple[int, str]] = []
    for link in parser.links:
        rel = link["rel"].lower()
        text = _clean_text(link["text"]).lower()
        title = _clean_text(link["title"]).lower()
        score = 0
        if "next" in rel.split():
            score += 10
        if text in _NEXT_TEXTS or title in _NEXT_TEXTS:
            score += 5
        if "next" in link["class"].lower():
            score += 3
        if score:
            url = urljoin(base_url, link["href"]) if base_url else link["href"]
            candidates.append((score, url))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# 主解析流程
# ---------------------------------------------------------------------------


def _new_result(source: str, batch_id: str, contract: FieldContract, metadata: Mapping[str, Any]) -> ParseResult:
    result = ParseResult(source=source, batch_id=batch_id, contract=contract)
    result.metadata = dict(metadata)
    result.stats = {
        "pages": 0,
        "pages_ok": 0,
        "pages_partial": 0,
        "pages_empty": 0,
        "pages_failed": 0,
        "tables": 0,
        "rows": 0,
        "parsed": 0,
        "failed_rows": 0,
        "attachments": 0,
        "duplicates": 0,
        "warnings": 0,
        "errors": 0,
    }
    return result


def _page_tables(page: Mapping[str, Any], *, fetcher, max_retries, backoff, sleeper, source, batch_id):
    """取出一页的表格列表；抓取失败抛异常由上层记录。

    返回 ``(tables, base_url, html, attempts)``。
    """
    html = page.get("html")
    if isinstance(html, (bytes, bytearray)):
        html = _decode_bytes(bytes(html))
    attempts = 1
    if html is None and page.get("tables") is None and page.get("rows") is None:
        url = page.get("url")
        if url:
            payload, attempts, error = _call_with_retry(
                lambda: fetcher(url), attempts=max_retries, backoff=backoff, sleep=sleeper
            )
            if error is not None:
                raise PageFetchError(
                    f"{type(error).__name__}: {error}", attempts=attempts, url=url
                ) from error
            html = _decode_bytes(payload) if isinstance(payload, (bytes, bytearray)) else str(payload)
        else:
            raise GazetteParseError("页面缺少可解析内容")
    base_url = page.get("base_url") or page.get("url")
    if page.get("url") and base_url is None:
        base_url = page["url"]
    if page.get("tables") is not None:
        return [_normalise_table(table) for table in page["tables"]], base_url, html, attempts
    if page.get("rows") is not None:
        table = {
            "headers": page.get("headers"),
            "rows": _normalise_rows(page["rows"]),
            "caption": page.get("caption") or "",
            "attrs": {},
            "base_url": base_url,
        }
        return [_normalise_table(table)], base_url, html, attempts
    return extract_html_tables(html, base_url=base_url), base_url, html, attempts


def _parse_one_page(
    page: Mapping[str, Any],
    *,
    source: str,
    batch_id: str,
    contract: FieldContract,
    fetcher: Callable[[str], Any],
    max_retries: int,
    backoff: float,
    sleeper: Callable[[float], None] | None,
    result: ParseResult,
    seen_entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """解析单页；页面内任何异常只污染本页，不向调用方抛出。"""
    number = page.get("number")
    page_record: dict[str, Any] = {
        "number": number,
        "url": page.get("url"),
        "status": "pending",
        "tables": 0,
        "rows": 0,
        "entries": 0,
        "failed_rows": 0,
        "attempts": 1,
        "error": None,
    }
    if page.get("error"):
        page_record["status"] = "failed"
        page_record["error"] = _truncate(page["error"], 200)
        result.stats["pages_failed"] += 1
        result.errors.append(
            _issue(
                "page_failed",
                f"第 {number} 页在抓取阶段失败：{_truncate(page['error'], 160)}",
                severity="error",
                blocking=False,
                source=source,
                batch_id=batch_id,
                page=number,
            )
        )
        return page_record

    try:
        tables, base_url, html, attempts = _page_tables(
            page,
            fetcher=fetcher,
            max_retries=max_retries,
            backoff=backoff,
            sleeper=sleeper,
            source=source,
            batch_id=batch_id,
        )
    except Exception as exc:
        attempts = getattr(exc, "attempts", max_retries)
        page_record["status"] = "failed"
        page_record["attempts"] = attempts
        page_record["error"] = _truncate(f"{type(exc).__name__}: {exc}", 300)
        result.stats["pages_failed"] += 1
        result.errors.append(
            _issue(
                "page_fetch_failed",
                f"第 {number} 页抓取失败（已尝试 {attempts} 次）：{page_record['error']}",
                severity="error",
                blocking=False,
                source=source,
                batch_id=batch_id,
                page=number,
                attempts=attempts,
                raw=page.get("url"),
            )
        )
        return page_record

    page_entries_before = len(result.entries)
    page_failures_before = result.stats.get("failed_rows", 0)
    page_parsed_before = result.stats.get("parsed_rows", 0)
    page_duplicates_before = result.stats.get("duplicates", 0)
    page_record["attempts"] = attempts
    page_record["tables"] = len(tables)
    result.stats["tables"] += len(tables)

    for offset, table in enumerate(tables):
        # 表号按页内顺序编号，定位始终是「第 N 页第 M 张表」。
        table_number = offset + 1
        table_base = table.get("base_url") or base_url
        ctx = _ParseContext(
            source=source,
            batch_id=batch_id,
            page=number,
            table=table_number,
            base_url=table_base,
            contract=contract,
            method="header",
        )
        first_row = 2 if table.get("headers") else 1
        try:
            _parse_table(
                table,
                ctx,
                result,
                page_number=number,
                first_row_number=first_row,
            )
        except Exception as exc:  # 单表异常不应拖垮整页其他表
            result.errors.append(
                _issue(
                    "table_parse_failed",
                    f"第 {number} 页第 {table_number} 张表解析失败：{type(exc).__name__}: {exc}",
                    severity="error",
                    blocking=False,
                    source=source,
                    batch_id=batch_id,
                    page=number,
                    table=table_number,
                )
            )

    # 批内重复条目：按稳定内容键合并（同一公告重复出现在分页边界时常见）。
    new_entries = result.entries[page_entries_before:]
    deduped: list[dict[str, Any]] = []
    for entry in new_entries:
        key = entry["entry_key"]
        existing = seen_entries.get(key)
        if existing is not None:
            result.stats["duplicates"] += 1
            result.warnings.append(
                _issue(
                    "duplicate_entry_in_batch",
                    f"条目 {entry.get('title')!r} 在本批中重复出现，已合并",
                    severity="warning",
                    blocking=False,
                    source=source,
                    batch_id=batch_id,
                    page=number,
                    row=entry.get("row"),
                    table=entry.get("table"),
                    entry_key=key,
                )
            )
            # 合并附件线索（按 URL 去重），保留首次出现的条目。重复行的线索
            # 已经在解析该行时进入 result.attachments，这里只把它们挂到幸存条目上。
            known = {clue.get("url") for clue in existing.get("attachments", []) if clue.get("url")}
            for clue in entry.get("attachments", []):
                if clue.get("url") and clue["url"] not in known:
                    known.add(clue["url"])
                    clue["entry_key"] = key
                    existing.setdefault("attachments", []).append(clue)
            continue
        seen_entries[key] = entry
        deduped.append(entry)
    if deduped:
        result.entries[page_entries_before:] = deduped
    else:
        del result.entries[page_entries_before:]

    page_record["parsed_rows"] = result.stats.get("parsed_rows", 0) - page_parsed_before
    page_record["duplicates"] = result.stats.get("duplicates", 0) - page_duplicates_before
    page_record["entries"] = len(result.entries) - page_entries_before
    page_record["failed_rows"] = result.stats.get("failed_rows", 0) - page_failures_before
    page_record["rows"] = page_record["parsed_rows"] + page_record["failed_rows"]
    result.stats["rows"] += page_record["rows"]
    page_record["html"] = bool(html)
    if page_record["failed_rows"] and page_record["entries"]:
        page_record["status"] = "partial"
        result.stats["pages_partial"] += 1
    elif page_record["entries"]:
        page_record["status"] = "ok"
        result.stats["pages_ok"] += 1
    elif page_record["parsed_rows"]:
        # 整页都是批内重复：不是空页，也不是失败页。
        page_record["status"] = "deduplicated"
        result.stats["pages_ok"] += 1
    else:
        page_record["status"] = "empty"
        result.stats["pages_empty"] += 1
        if not any(issue.get("page") == number for issue in result.errors):
            result.warnings.append(
                _issue(
                    "page_empty",
                    f"第 {number} 页没有解析出任何公告",
                    severity="warning",
                    blocking=False,
                    source=source,
                    batch_id=batch_id,
                    page=number,
                )
            )
    return page_record


def parse_board(
    document: Any,
    *,
    source: str | None = None,
    batch_id: str | None = None,
    contract: FieldContract | Mapping[str, Any] | None = None,
    base_url: str | None = None,
    fetcher: Callable[[str], Any] | None = None,
    max_retries: int = 2,
    retry_backoff: float = 0.0,
    sleep: Callable[[float], None] | None = None,
    max_pages: int = 50,
    metadata: Mapping[str, Any] | None = None,
    resume_from: ParseResult | None = None,
) -> ParseResult:
    """解析一块公示栏，委派给 dedupe / attach / store 三路产物。

    参数
    ----
    document
        HTML 文本、URL、``{"pages": [...]}``、表格列表或行列表。每页可包含
        ``html`` / ``url`` / ``tables`` / ``rows``；``url`` 页面会经 ``fetcher``
        拉取，失败按 ``max_retries`` 局部重试。
    source
        公告来源标识，默认取文档中的 ``source`` 或 ``"unknown"``。
    batch_id
        解析批次，默认 ``来源 + 文档内容摘要``（重复抓取同一内容得到同一批次）。
    contract
        字段契约，决定必填/降级边界；默认只要求 ``title``。
    resume_from
        上一次的结果；只重试其失败页面并合并，其他条目原样保留、不重写。

    局部失败
    ----
    坏行 → 跳过该行，记录带 ``page/row/table/field`` 的错误，其余行照常入库；
    坏页 → 该页标记 ``failed``，其他页照常；无法识别整份文档 → 单条 fatal
    错误，结果为空而不是抛异常。
    """
    fetcher = fetcher or _default_fetcher
    contract_obj = _coerce_contract(contract)
    result_metadata: dict[str, Any] = dict(metadata or {})
    # resume 时沿用原批次的来源/批次/契约，避免补抓写进另一个批次。
    if resume_from is not None:
        if source is None:
            source = resume_from.source
        if batch_id is None:
            batch_id = resume_from.batch_id
        if contract is None:
            contract_obj = resume_from.contract
    if isinstance(document, Mapping):
        for key in ("metadata", "source", "batch_id", "base_url"):
            if key in document and key not in result_metadata:
                result_metadata[key] = document[key]
        if source is None and document.get("source"):
            source = document["source"]
        if batch_id is None and document.get("batch_id"):
            batch_id = document["batch_id"]
        if base_url is None and document.get("base_url"):
            base_url = document["base_url"]
    if isinstance(document, str) and source is None:
        source = None
    source = source or "unknown"

    try:
        pages, method, doc_meta = _document_pages(document)
    except GazetteParseError as exc:
        result = _new_result(source, batch_id or make_batch_id(source, str(document)), contract_obj, result_metadata)
        result.stats["pages"] = 0
        result.errors.append(
            _issue(
                "document_unrecognized",
                f"无法识别公示栏文档：{exc}",
                severity="error",
                blocking=True,
                fatal=True,
                source=source,
                batch_id=result.batch_id,
            )
        )
        result.stats["errors"] = len(result.errors)
        result.metadata["method"] = "unknown"
        return result

    result_metadata.update({k: v for k, v in doc_meta.items() if k not in result_metadata})
    if batch_id is None:
        batch_id = make_batch_id(source, document)
    result = _new_result(source, batch_id, contract_obj, result_metadata)
    result.metadata["method"] = method
    if not pages and resume_from is None:
        result.warnings.append(
            _issue(
                "document_empty",
                "公示栏文档没有页面或行",
                severity="warning",
                blocking=False,
                source=source,
                batch_id=batch_id,
            )
        )
        result.stats["warnings"] = 1
        return result

    # 分页：HTML 文档 + 相对/绝对“下一页”链接时自动跟进（有上限、有去重）。
    if method == "html" and pages:
        page = pages[0]
        page_base = base_url or page.get("base_url") or page.get("url")
        page["base_url"] = page_base
        if page.get("html") is None and page.get("url") and page.get("error") is None:
            # 首屏是 URL：先抓一次用于发现“下一页”链接（失败仍由后续页解析记录）。
            payload, _, error = _call_with_retry(
                lambda url=page["url"]: fetcher(url),
                attempts=max_retries,
                backoff=retry_backoff,
                sleep=sleep,
            )
            if error is None and payload is not None:
                page["html"] = (
                    _decode_bytes(payload)
                    if isinstance(payload, (bytes, bytearray))
                    else str(payload)
                )
        current_html = page.get("html")
        visited: set[str] = set()
        if page.get("url"):
            visited.add(page["url"])
        number = 1
        while current_html is not None and len(pages) < max_pages:
            next_url = _find_next_page(current_html, page_base)
            if not next_url or next_url in visited:
                break
            visited.add(next_url)
            number += 1
            pages.append(
                {
                    "number": number,
                    "url": next_url,
                    "html": None,
                    "tables": None,
                    "rows": None,
                    "base_url": next_url,
                }
            )
            try:
                payload, _, error = _call_with_retry(
                    lambda url=next_url: fetcher(url),
                    attempts=max_retries,
                    backoff=retry_backoff,
                    sleep=sleep,
                )
            except Exception as exc:  # pragma: no cover - _call_with_retry 已捕获
                error = exc
                payload = None
            if error is not None or payload is None:
                pages[-1]["error"] = f"分页抓取失败：{_truncate(error, 200)}"
                break
            current_html = _decode_bytes(payload) if isinstance(payload, (bytes, bytearray)) else str(payload)
            pages[-1]["html"] = current_html

    # resume：只重试失败页，其他结果原样复用。
    if resume_from is not None:
        wanted_numbers = {
            entry.get("number")
            for entry in resume_from.pages
            if entry.get("status") in {"failed", "empty"}
        }
        specs_by_number = {page.get("number"): page for page in pages}
        # 原批次里失败页的 URL 直接就绪时优先使用它：调用方可以只传
        # ``{"pages": []}`` + ``resume_from=``，无需重新提供整份文档。
        retry_pages: list[dict[str, Any]] = []
        for entry in resume_from.pages:
            number = entry.get("number")
            if number not in wanted_numbers:
                continue
            spec = specs_by_number.get(number)
            if spec is None or (
                spec.get("html") is None
                and spec.get("tables") is None
                and spec.get("rows") is None
                and not spec.get("url")
            ):
                if entry.get("url"):
                    spec = {
                        "number": number,
                        "url": entry["url"],
                        "html": None,
                        "tables": None,
                        "rows": None,
                        "base_url": entry.get("url"),
                    }
            if spec is not None:
                retry_pages.append(spec)
        if not retry_pages:
            return resume_from
        pages = retry_pages

    result.stats["pages"] = len(pages)
    seen_entries: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_spec = dict(page)
        if base_url and not page_spec.get("base_url"):
            page_spec["base_url"] = base_url
        page_record = _parse_one_page(
            page_spec,
            source=source,
            batch_id=batch_id,
            contract=contract_obj,
            fetcher=fetcher,
            max_retries=max(max(1, int(max_retries)), 1),
            backoff=retry_backoff,
            sleeper=sleep,
            result=result,
            seen_entries=seen_entries,
        )
        result.pages.append(page_record)

    result.stats["parsed"] = len(result.entries)
    result.stats["attachments"] = len(result.attachments)
    result.stats["warnings"] = len(result.warnings)
    result.stats["errors"] = len(result.errors)
    if (
        resume_from is None
        and result.stats["pages"]
        and result.stats["pages_failed"] == result.stats["pages"]
    ):
        result.errors.append(
            _issue(
                "batch_all_pages_failed",
                "整批所有页面均抓取失败",
                severity="error",
                blocking=True,
                fatal=True,
                source=source,
                batch_id=batch_id,
            )
        )
        result.stats["errors"] = len(result.errors)
    if resume_from is not None:
        merged = _merge_resume(resume_from, result)
        return merged
    return result


def _merge_resume(previous: ParseResult, retried: ParseResult) -> ParseResult:
    """把补抓结果合并回原批次；未失败的条目保持同一对象，避免重写。"""
    merged = _new_result(previous.source, previous.batch_id, previous.contract, previous.metadata)
    merged.entries = list(previous.entries)
    merged.attachments = list(previous.attachments)
    merged.warnings = list(previous.warnings)
    retried_pages = {page.get("number") for page in retried.pages}
    merged.errors = [
        issue
        for issue in previous.errors
        if not (
            issue.get("page") in retried_pages
            and issue.get("code") in {"page_fetch_failed", "page_failed", "page_empty"}
        )
    ]
    merged.pages = [page for page in previous.pages if page.get("number") not in retried_pages]
    merged.pages.extend(retried.pages)
    merged.pages.sort(key=lambda page: (page.get("number") is None, page.get("number")))
    merged.entries.extend(retried.entries)
    merged.attachments.extend(retried.attachments)
    merged.errors.extend(retried.errors)
    merged.warnings.extend(retried.warnings)
    merged.stats = dict(previous.stats)
    merged.stats.update(
        {
            "parsed": len(merged.entries),
            "attachments": len(merged.attachments),
            "warnings": len(merged.warnings),
            "errors": len(merged.errors),
            "recovered_pages": len(retried.pages),
            "pages": len(merged.pages),
        }
    )
    merged.stats["pages_ok"] = sum(
        1 for page in merged.pages if page.get("status") in {"ok", "deduplicated"}
    )
    merged.stats["pages_partial"] = sum(
        1 for page in merged.pages if page.get("status") == "partial"
    )
    merged.stats["pages_empty"] = sum(
        1 for page in merged.pages if page.get("status") == "empty"
    )
    merged.stats["pages_failed"] = sum(
        1 for page in merged.pages if page.get("status") == "failed"
    )
    merged.stats["rows"] = sum(int(page.get("rows") or 0) for page in merged.pages)
    merged.metadata = dict(previous.metadata)
    merged.metadata["recovered_pages"] = sorted(retried_pages)
    merged.metadata["resumed"] = True
    return merged


parse_document = parse_board
parse = parse_board


def parse_pages(
    pages: Sequence[Any],
    *,
    source: str = "unknown",
    batch_id: str | None = None,
    contract: FieldContract | Mapping[str, Any] | None = None,
    base_url: str | None = None,
    fetcher: Callable[[str], Any] | None = None,
    max_retries: int = 2,
    retry_backoff: float = 0.0,
    sleep: Callable[[float], None] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ParseResult:
    """显式分页列表入口。"""
    return parse_board(
        list(pages),
        source=source,
        batch_id=batch_id,
        contract=contract,
        base_url=base_url,
        fetcher=fetcher,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        sleep=sleep,
        metadata=metadata,
    )


def parse_rows(
    rows: Sequence[Any],
    *,
    headers: Sequence[Any] | None = None,
    source: str = "unknown",
    batch_id: str | None = None,
    contract: FieldContract | Mapping[str, Any] | None = None,
    base_url: str | None = None,
    page: int = 1,
    metadata: Mapping[str, Any] | None = None,
) -> ParseResult:
    """单页行列表入口；``headers`` 缺省时尝试从首行推断或走旧格式位置解析。"""
    spec = {
        "number": page,
        "rows": list(rows),
        "headers": list(headers) if headers is not None else None,
        "base_url": base_url,
    }
    return parse_board(
        {"pages": [spec]},
        source=source,
        batch_id=batch_id,
        contract=contract,
        base_url=base_url,
        metadata=metadata,
    )


def parse_record(record: Mapping[str, Any]) -> ParseResult:
    """把单条已结构化（或扁平）记录解析成一批一条的结果。"""
    source = str(record.get("source") or record.get("src") or "unknown")
    return parse_board(
        {"pages": [{"number": 1, "rows": [dict(record)]}]},
        source=source,
        batch_id=record.get("batch_id"),
        metadata={"single_record": True},
    )


def process(record):
    """流水线阶段：保持旧行为，同时支持整块公示栏文档。

    * 普通扁平记录：原样复制 + ``_stage_gazette_parse`` 标记（与基线一致）。
    * 含 ``html/pages/tables/rows`` 的文档：解析后把条目、附件、错误分开挂在
      ``gazette_entries`` / ``gazette_attachments`` / ``gazette_errors`` 上，
      并把完整结果放在 ``gazette_result``，由编排层分发给下游。
    """
    rec = dict(record)
    is_board = any(key in record for key in ("html", "pages", "tables", "document"))
    has_rows = isinstance(record.get("rows"), (list, tuple))
    if not (is_board or has_rows):
        rec.setdefault("_stage_gazette_parse", "naive")
        return rec

    contract = record.get("contract")
    result = parse_board(
        record,
        source=str(record.get("source") or record.get("src") or "unknown"),
        batch_id=record.get("batch_id"),
        contract=contract,
        base_url=record.get("base_url"),
        fetcher=record.get("fetcher"),
        max_retries=int(record.get("max_retries", 2)),
        retry_backoff=float(record.get("retry_backoff", 0.0)),
        metadata={"via": "process"},
    )
    rec["_stage_gazette_parse"] = "parsed"
    rec["gazette_result"] = result.as_dict()
    rec["gazette_entries"] = result.entries
    rec["gazette_attachments"] = result.attachments
    rec["gazette_errors"] = result.errors
    rec["gazette_warnings"] = result.warnings
    rec["batch"] = result.batch_meta()
    return rec


# ---------------------------------------------------------------------------
# 附件下载（逐附件隔离与重试）
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(payload)
    tmp.replace(path)


def _target_path(destination: Path, clue: Mapping[str, Any]) -> Path:
    """在 destination 内生成安全文件名；拒绝路径穿越。"""
    filename = _safe_filename(
        str(clue.get("filename") or _guess_filename(str(clue.get("url") or ""), str(clue.get("label") or "")))
    )
    candidate = (destination / filename).resolve()
    root = destination.resolve()
    if root not in candidate.parents and candidate != root:
        filename = _safe_filename(Path(filename).name)
        candidate = root / filename
    return candidate


def download_attachments(
    target: ParseResult | Iterable[Mapping[str, Any]],
    destination: str | Path,
    *,
    fetcher: Callable[[str], Any] | None = None,
    max_retries: int = 2,
    retry_backoff: float = 0.0,
    sleep: Callable[[float], None] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """下载附件线索；单个附件失败不影响其他附件，也不影响条目入库。

    * 无 URL 的线索标记 ``unresolved``，不算失败也不阻断。
    * 目标文件已存在时默认跳过（``skipped_existing``），重复抓取不重写文件；
      若线索带有 ``sha256`` 且与已有文件一致，同样视为复用。
    * 下载成功后写 ``path/sha256/size``，失败写 ``error/attempts``。

    传入 :class:`ParseResult` 时会同步更新其 ``attachments``（以及条目的
    ``attachments``），返回摘要。
    """
    fetcher = fetcher or _default_fetcher
    destination = Path(destination)
    clues: list[dict[str, Any]]
    if isinstance(target, ParseResult):
        clues = target.attachments
    else:
        clues = [dict(clue) for clue in target]

    summary: dict[str, Any] = {
        "downloaded": 0,
        "reused": 0,
        "skipped_existing": 0,
        "unresolved": 0,
        "failed": 0,
        "attachments": [],
    }
    for clue in clues:
        url = _clean_text(clue.get("url"))
        if not url:
            clue["status"] = "unresolved"
            clue.setdefault("error", "缺少附件 URL")
            summary["unresolved"] += 1
            summary["attachments"].append(dict(clue))
            continue
        path = _target_path(destination, clue)
        expected_sha = _clean_text(clue.get("sha256"))
        if path.exists() and not overwrite:
            if expected_sha:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual == expected_sha:
                    clue.update(
                        {
                            "status": "reused",
                            "path": str(path),
                            "sha256": actual,
                            "size": path.stat().st_size,
                        }
                    )
                    summary["reused"] += 1
                    summary["attachments"].append(dict(clue))
                    continue
            else:
                clue.update(
                    {
                        "status": "skipped_existing",
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size,
                    }
                )
                summary["skipped_existing"] += 1
                summary["attachments"].append(dict(clue))
                continue

        payload, attempts, error = _call_with_retry(
            lambda url=url: fetcher(url),
            attempts=max_retries,
            backoff=retry_backoff,
            sleep=sleep,
        )
        if error is not None or payload is None:
            clue.update(
                {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {_truncate(error, 200)}",
                    "attempts": attempts,
                    "path": None,
                }
            )
            summary["failed"] += 1
            summary["attachments"].append(dict(clue))
            continue
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
        try:
            _write_atomic(path, bytes(data))
        except OSError as exc:
            clue.update(
                {
                    "status": "failed",
                    "error": f"写入失败：{_truncate(exc, 200)}",
                    "attempts": attempts,
                    "path": None,
                }
            )
            summary["failed"] += 1
            summary["attachments"].append(dict(clue))
            continue
        digest = hashlib.sha256(data).hexdigest()
        clue.update(
            {
                "status": "downloaded",
                "path": str(path),
                "sha256": digest,
                "size": len(data),
                "attempts": attempts,
            }
        )
        summary["downloaded"] += 1
        summary["attachments"].append(dict(clue))

    if isinstance(target, ParseResult):
        entry_index = {entry.get("entry_key"): entry for entry in target.entries}
        for clue in target.attachments:
            entry = entry_index.get(clue.get("entry_key"))
            if entry is None:
                continue
            for attached in entry.get("attachments", []):
                if attached.get("url") == clue.get("url"):
                    attached.update(
                        {
                            key: clue[key]
                            for key in ("status", "path", "sha256", "size", "error", "attempts")
                            if key in clue
                        }
                    )
        target.stats["attachments_downloaded"] = summary["downloaded"]
        target.stats["attachments_failed"] = summary["failed"]
    return summary


# 便于测试与调用方导入
__all__ = [
    "CANONICAL_FIELDS",
    "PARSED_FIELDS",
    "DEFAULT_CONTRACT",
    "FieldContract",
    "GazetteParseError",
    "HeaderMap",
    "ParseResult",
    "canonical_header",
    "download_attachments",
    "extract_html_tables",
    "make_batch_id",
    "map_header",
    "parse",
    "parse_board",
    "parse_document",
    "parse_pages",
    "parse_record",
    "parse_rows",
    "process",
]
