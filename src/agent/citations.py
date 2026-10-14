"""引用校验闭环：让报告里的事实句能追溯到真实抓取到的证据。

## 解决什么

原 `verify_consistency()` 用「LLM 自报 confidence 求平均」当作可信度——那是**循环论证**：
模型对自己的答案打高分，不代表答案对。`QualityScorer` 同样用关键词命中数量冒充质量分。
本模块提供一套**全确定性**的校验：同样的报告 + 同样的证据，永远得到同样的结论。

## 四指标各自回答什么（以及不回答什么）

| 指标 | 能回答 | **不能**回答 |
|---|---|---|
| 引用有效性 `citation_validity` | 引用 id 是否存在、URL 是否属于真实采集来源 | 该来源是否真的支持这条结论 |
| 引用覆盖度 `citation_coverage` | 含数字的事实句里有多少带了引用 | 引用内容是否正确 |
| 数值扎根性 `grounding` | 数字归一化后能否在所引证据原文中找到 | 数字的对象/时间范围/单位是否一致，也不区分相关与因果 |
| 幻觉 URL `hallucinated_urls` | 报告是否引用了未抓取到的地址 | 来源本身是否过时或只是营销说法 |

四指标全是字符串与集合运算。它们证明「没有格式性造假」，**不证明「事实正确」**。
对外表述只能是"未发现格式性造假"，不得写成"已验证真实性"。

"语义支持性"（证据是否真正支持这条主张）是第五类问题，只能靠人工标注集量化，
不在本模块范围内——用模型判定来补就是 LLM-as-Judge，正是本项目刻意避开的路线。
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# 引用标记：[e:abc123def456]
EVIDENCE_ID_LEN = 12
CITATION_RE = re.compile(rf"\[e:([0-9a-f]{{{EVIDENCE_ID_LEN}}})\]")

# 报告正文中出现的 URL
URL_RE = re.compile(r"https?://[^\s)\]\"'<>`|]+")

# 机器附录豁免区：附录（如「数据完整度评估」）由确定性代码生成，其中的数字
# （5.6/10、进度条、数据条数）来自程序计算而非模型的事实主张。把它纳入引用
# 校验会把所有带附录的报告误判为低可信——fake 全流程实测抓到过这个误伤：
# 一份引用全绿的报告，因附录里的进度条数字被判出 4 处 uncited_claim。
MACHINE_APPENDIX_RE = re.compile(r"<!--machine-appendix-->.*?<!--/machine-appendix-->", re.DOTALL)


def strip_machine_appendices(report: str) -> str:
    """剔除机器附录，返回应纳入引用校验的报告主体。"""
    return MACHINE_APPENDIX_RE.sub("", report or "")

# 数值 token：支持千分位与小数。不匹配年份以外的中文数字（汉字数字归一化易误伤，
# 例如"第一"不是可核验的数值主张）。
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# 句子切分：中文与英文句末标点 + 换行
SENTENCE_SPLIT_RE = re.compile(r"[。！？!?\n]+")

# 门槛。低于 coverage / validity / grounding 即视为不合格报告。
DEFAULT_THRESHOLDS = {
    "citation_coverage": 0.90,
    "citation_validity": 1.00,
    "grounding": 0.90,
    "max_hallucinated_urls": 0,
}


def _normalize_numbers(text: str) -> str:
    """去掉千分位逗号，让 `1,000` 与 `1000` 在扎根性比对时等价。"""
    return (text or "").replace(",", "")


def extract_numbers(text: str) -> List[str]:
    """抽取数值 token（已去掉千分位）。用于判断一句话是否含可核验的数值主张。"""
    return [m.replace(",", "") for m in NUMBER_RE.findall(text or "")]


@dataclass(frozen=True)
class Evidence:
    """一条可追溯的证据。`value` 是原文片段，扎根性比对只在它内部进行。"""

    evidence_id: str
    url: str
    key: str
    value: str
    summary: str = ""

    @property
    def citation(self) -> str:
        return f"[e:{self.evidence_id}]"

    @property
    def numbers(self) -> Set[float]:
        """证据原文中出现的全部数值（已归一）。

        扎根性必须按**数值集合**比对而不是子串：`$9` 用子串匹配会命中原文里的 `99`、
        `49`、`19`，等于给编造的数字发通行证。
        """
        out: Set[float] = set()
        for token in NUMBER_RE.findall(_normalize_numbers(self.value)):
            try:
                out.add(float(token))
            except ValueError:
                continue
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"evidence_id": self.evidence_id, "url": self.url, "key": self.key}


def make_evidence_id(url: str, key: str, value: str) -> str:
    """evidence_id = sha256(url\\0key\\0value)[:12]。

    NUL 分隔，避免拼接歧义（`a`+`bc` 与 `ab`+`c` 不能撞成同一个 id）。
    内容变化才会换 id——这是特性不是缺陷：证据被改写后，旧 id 不该继续代表新内容。
    """
    raw = f"{url or ''}\x00{key or ''}\x00{value or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:EVIDENCE_ID_LEN]


def _summarize(value: str, limit: int = 48) -> str:
    flat = " ".join((value or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


@dataclass
class EvidenceStore:
    """一次调研抓到的全部证据。报告里出现的每个 `[e:id]` 都必须能在这里查到。"""

    evidences: List[Evidence] = field(default_factory=list)
    _by_id: Dict[str, Evidence] = field(default_factory=dict, repr=False)
    _urls: Set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_extracted_data(cls, items: Iterable[Dict[str, Any]]) -> "EvidenceStore":
        store = cls()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "") or "").strip()
            if not value:
                continue
            store.add(
                url=str(item.get("source_url", "") or "").strip(),
                key=str(item.get("key", "") or "").strip(),
                value=value,
            )
        return store

    def add(self, url: str, key: str, value: str) -> Evidence:
        eid = make_evidence_id(url, key, value)
        if eid not in self._by_id:
            evidence = Evidence(
                evidence_id=eid,
                url=url,
                key=key,
                value=value,
                summary=_summarize(value),
            )
            self.evidences.append(evidence)
            self._by_id[eid] = evidence
            if url:
                self._urls.add(url)
        return self._by_id[eid]

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return self._by_id.get(evidence_id)

    @property
    def urls(self) -> Set[str]:
        return set(self._urls)

    def __len__(self) -> int:
        return len(self.evidences)

    def table(self) -> str:
        """喂给报告提示词的紧凑证据表。

        刻意只给 id / key / 一行摘要，不给完整 value——完整原文已经过多轮提取，
        再整段塞进提示词既超预算又挤占模型的注意力预算。扎根性比对在本地做，
        不依赖模型是否记得原文。
        """
        if not self.evidences:
            return "（本次调研没有可引用的结构化证据）"
        lines = ["[e:id] | 维度 | 来源 | 摘要"]
        for ev in self.evidences:
            lines.append(
                f"{ev.citation} | {ev.key or '-'} | {ev.url or '未标注来源'} | {ev.summary}"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {"count": len(self.evidences), "evidences": [e.to_dict() for e in self.evidences]}


def iter_claims(report: str) -> List[Tuple[str, List[str]]]:
    """切出句子并标注其中是否含数值主张。

    返回 `[(句子, 数值列表)]`。**只有含数值的句子才被视为需要引用的事实主张**——
    "该产品定位清晰"这类概括句强制要求引用既无意义也会逼模型编造引用。

    抽数值前必须先剥掉 `[e:...]` 标记：evidence_id 是 12 位十六进制，里面全是数字
    （如 `[e:501e2d117056]`），不剥会被当成三个"事实数值"，让扎根性永远判不通过。
    """
    claims: List[Tuple[str, List[str]]] = []
    for raw in SENTENCE_SPLIT_RE.split(report or ""):
        sentence = raw.strip()
        if not sentence:
            continue
        numbers = extract_numbers(CITATION_RE.sub(" ", sentence))
        if numbers:
            claims.append((sentence, numbers))
    return claims


def _cited_ids(sentence: str) -> List[str]:
    return CITATION_RE.findall(sentence)


def _grounded(sentence_numbers: Sequence[str], evidence: Evidence) -> bool:
    """句中每个数值都能在该证据原文中找到。

    全称而非存在量词：`定价 $49` 出现在证据里、但 `60 QPM` 是编的，整句仍应判未扎根。
    """
    available = evidence.numbers
    for token in sentence_numbers:
        try:
            if float(token) not in available:
                return False
        except ValueError:
            return False
    return True


def verify_report(
    report: str,
    store: EvidenceStore,
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """校验报告的引用完整性。返回四指标 + 违规清单 + 是否合格。

    违规清单是给重生成提示词用的——只说"不合格"没用，得指出具体哪句、缺什么。
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)

    violations: List[Dict[str, Any]] = []

    # 附录（数据完整度等）是程序算出来的，不是模型主张：校验只看主体
    body = strip_machine_appendices(report)

    # --- 引用有效性：报告里引用的 id 必须存在 ---
    all_cited = CITATION_RE.findall(body)
    unknown_ids = sorted({eid for eid in all_cited if store.get(eid) is None})
    citation_validity = (
        1.0 if not all_cited else (len(all_cited) - len(unknown_ids)) / len(all_cited)
    )
    for eid in unknown_ids:
        violations.append({
            "type": "unknown_citation",
            "detail": f"引用了不存在的证据 id [e:{eid}]",
        })

    # --- 覆盖度与扎根性：逐句判定含数值的事实主张 ---
    claims = iter_claims(body)
    cited_claims = 0
    grounded_claims = 0
    for sentence, numbers in claims:
        cited = _cited_ids(sentence)
        if not cited:
            violations.append({
                "type": "uncited_claim",
                "detail": f"含数值的结论未标注证据：{_trim(sentence)}",
            })
            continue
        cited_claims += 1

        sources = [store.get(eid) for eid in cited]
        present = [ev for ev in sources if ev is not None]
        if not present:
            continue  # 未知 id 已在 validity 记过，不重复计入扎根性

        missing = [n for n in numbers if not any(_grounded([n], ev) for ev in present)]
        if missing:
            violations.append({
                "type": "ungrounded_claim",
                "detail": (
                    f"数值 {'、'.join(missing)} 在所引证据原文中找不到：{_trim(sentence)}"
                ),
            })
        else:
            grounded_claims += 1

    citation_coverage = 1.0 if not claims else cited_claims / len(claims)
    grounding = 1.0 if not cited_claims else grounded_claims / cited_claims

    # --- 幻觉 URL：报告出现的 URL 必须属于实际抓取来源 ---
    report_urls = {u.rstrip(".,;，。；") for u in URL_RE.findall(body)}
    hallucinated_urls = sorted(report_urls - store.urls)

    passed = (
        citation_coverage >= limits["citation_coverage"]
        and citation_validity >= limits["citation_validity"]
        and grounding >= limits["grounding"]
        and len(hallucinated_urls) <= limits["max_hallucinated_urls"]
    )

    return {
        "passed": passed,
        "citation_coverage": round(citation_coverage, 3),
        "citation_validity": round(citation_validity, 3),
        "grounding": round(grounding, 3),
        "hallucinated_urls": hallucinated_urls,
        "violations": violations,
        "violation_count": len(violations),
        "claims_checked": len(claims),
        "citations_seen": len(all_cited),
        "evidence_count": len(store),
    }


def _trim(sentence: str, limit: int = 80) -> str:
    flat = " ".join(sentence.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def format_violations(result: Dict[str, Any], limit: int = 12) -> str:
    """把违规清单渲染成可直接塞进重生成提示词的中文清单。"""
    violations = result.get("violations") or []
    if not violations:
        return ""
    lines = ["【上一版报告存在的问题，必须逐条修正】"]
    for item in violations[:limit]:
        lines.append(f"- [{item.get('type')}] {item.get('detail')}")
    remaining = len(violations) - limit
    if remaining > 0:
        lines.append(f"- ……另有 {remaining} 处同类问题")
    lines.append(
        "注意：不要靠删掉数字来规避——没有证据支撑的结论应当删除整句，"
        "而不是保留一个没有来源的断言。"
    )
    return "\n".join(lines)