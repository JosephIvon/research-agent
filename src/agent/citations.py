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

# 裸引用碎片：切分后只剩下引用标记本身，没有承载任何数值内容。
#
# 提示词要求「引用标在句末」，模型据此输出 `…定价 99 美元/席位/月。[e:abc]`，
# 而切分在句号处断开，引用就被甩成独立碎片，上一句退化成"含数字却无引用"，
# 白白扣掉覆盖率（真实冒烟实测：同一份报告 coverage 从 1.0 掉到 0.5）。
# 唯一没有数值（已剥引用）的碎片按此归并回前一句。
CITATION_ONLY_FRAGMENT_RE = re.compile(
    r"^[\s、,，;；:：]*"
    rf"(?:\[e:[0-9a-f]{{{EVIDENCE_ID_LEN}}}\][\s、,，;；:：]*)+$"
)

# Markdown 结构编号：章节号（`### 2.1`）与有序列表标记（`1. `）。
#
# 这些数字是排版产物而非事实主张，拿去证据原文里比对必然落空——实测一份全绿的报告
# 因列表项的 `1.` 被判成 ungrounded，扎根性直接打到 0。
#
# 规则刻意收窄，避免误伤正文数字：
# - 章节号必须带分隔点（`2.1`）或紧跟标点（`1.`），`## 2024 年度总结` 的 2024 保留；
# - 列表标记必须顶格（最多 3 个前导空格，符合 Markdown 规范）且标点后紧跟空白。
# 残留风险：正文恰好以「2024. 年度…」顶格起句会被剥掉开头年份——这比原状态
# （每条列表项都被误判未扎根）小得多。
_STRUCT_NUMBER_PREFIX_RE = re.compile(
    r"^ {0,3}(?:"
    r"#{1,6}[ \t]*(?:\d+(?:\.\d+)+[.、]?|\d+[.、)][ \t]*)"
    r"|\d+[.、)][ \t]+"
    r")"
)

# 无引用句的两种性质：真·无据断言 vs 已识别缺口
#
# 「含数字却没引用」覆盖两类完全不同的句子：
#   断言："Acme 标准版定价 39 美元/席位/月"            ← 陈述外部事实却无来源，是真指控
#   缺口："付费层限速策略公开页面未披露，建议向厂商索取" ← 承认没查到，是系统做对了
#
# 真实 MiniMax 冒烟里被判 uncited 的正是后者，却被写成「含数值的结论未标注证据：…」
# ——用户读到的是系统在指控一篇诚实标注了缺口的报告。
#
# ⚠️ **本分类只用于呈现，绝不用于豁免**：两类都照样计入 citation_coverage、
# 照样可能让 passed=False。若在此给缺口句免引用，就等于开了一条
# 「写上'未披露'就绕过引用门禁」的逃逸通道——那比误报严重得多。
# `test_classification_never_exempts_from_coverage` 钉住这条底线。
GAP_ACK_RE = re.compile(
    r"未披露|未公开|未见披露|未公布|未查到|未检索到|暂无公开|暂缺|缺失|数据缺失"
    r"|无法核实|无法确认|待确认|未能确认"
    r"|建议(?:向[^，。；]{0,12})?(?:索取|确认|核实|询问|获取|补充)"
    r"|需(?:要)?(?:向[^，。；]{0,12})?(?:索取|确认|核实|询问|获取|补充)"
    r"|请求(?:向[^，。；]{0,12})?(?:索取|确认|核实|获取)"
)
CLAIM_KIND_ASSERTION = "assertion"
CLAIM_KIND_GAP_ACK = "gap_ack"


def classify_uncited(sentence: str) -> str:
    """判断无引用句是「无据断言」还是「已识别缺口」。返回二者之一。

    判定用措辞而非语义模型：确定性、可测、零依赖。代价是模型可以**选择措辞**
    来改变呈现分类——但改不了判定结果（见上方警告），所以最坏情况只是
    一句违规被描述成缺口，仍然会出现在低可信报告里。
    """
    return CLAIM_KIND_GAP_ACK if GAP_ACK_RE.search(sentence or "") else CLAIM_KIND_ASSERTION


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


def _strip_structural_numbering(report: str) -> str:
    """按行剥掉 Markdown 结构编号前缀（章节号 / 有序列表标记）。

    逐行处理而不是整篇 `re.sub`：前缀规则锚定在行首，全局替换会把句子中间的
    「…见 1. 附录」也削掉。
    """
    return "\n".join(
        _STRUCT_NUMBER_PREFIX_RE.sub("", line) for line in (report or "").splitlines()
    )


def iter_claims(report: str) -> List[Tuple[str, List[str]]]:
    """切出句子并标注其中是否含数值主张。

    返回 `[(句子, 数值列表)]`。**只有含数值的句子才被视为需要引用的事实主张**——
    "该产品定位清晰"这类概括句强制要求引用既无意义也会逼模型编造引用。

    两处必须先做归一，否则会把模型的正确写法判成违规：

    1. **剥掉结构编号**：`### 2.1`、`1. ` 里的数字是排版产物，进扎根性比对必挂。
    2. **把句末裸引用归并回上一句**：`… 99 美元/月。[e:abc]` 在句号处被切开，
       引用落到独立碎片后，上一句就成了"含数字却无引用"的假违规。

    抽数值前还必须先剥掉 `[e:...]` 标记：evidence_id 是 12 位十六进制，里面全是数字
    （如 `[e:501e2d117056]`），不剥会被当成三个"事实数值"，让扎根性永远判不通过。
    """
    sentences: List[str] = []
    buffer = ""
    for raw in SENTENCE_SPLIT_RE.split(_strip_structural_numbering(report or "")):
        fragment = raw.strip()
        if not fragment:
            continue
        if CITATION_ONLY_FRAGMENT_RE.match(fragment):
            # 归并回上一句；若上一句不存在（引用出现在报告开头）则丢弃——
            # 凭空造一条"只有引用没有内容"的主张毫无意义。
            if buffer:
                buffer = f"{buffer} {fragment}"
            continue
        if buffer:
            sentences.append(buffer)
        buffer = fragment
    if buffer:
        sentences.append(buffer)

    claims: List[Tuple[str, List[str]]] = []
    for sentence in sentences:
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


# --------------------------------------------------------------------------
# 推导数值：单列 `derived_claims`，不阻塞交付
#
# 方案文档 §4-M2 写死的边界：扎根性**只判直接引用数值**（原文数字原样出现），
# 推导数值单列记录。否则"折算后 39.2 美元"（= 49 × 8 ÷ 10）这种**正确**推导会被判编造，
# 扎根率被误判拉低、门槛形同虚设——真实 MiniMax 冒烟里就是这么把一份定价正确的报告打回重生成的。
#
# 判据必须是**全确定性**的纯函数：不调 LLM、不含随机性。理由与本模块开头一致——
# 让模型判定"这是不是它自己算出来的"就是循环论证（模型给自己打分），门禁必须能复现。
#
# ## 形状规则（这几条是"推导"和"编一个前提再算"之间的全部区别）
#
# 自由枚举两层代数是不行的，实测会被两类恒等式击穿：
#   - 中性元：`8/8 + 8 = 9` → 编造的 `$9` 被"推导"出来；
#   - 变号术：`(10 - 49) + 99 = 60` → 编造的 `60 QPM` 被"推导"出来。
# 两者都会直接击穿既有回归测试（`$9`、`60 QPM` 必须判无据），因此规则收窄为：
#
#   R1 最多 2 个运算、深度最多 2 层（`(a op b) op c`），不递归展开；
#   R2 同一个数不得重复使用（挡 `8/8`；按**值**判断，按出处判断等于没写）；
#   R3 一条推导式里最多出现 1 个常数（`49 × 10 × 12 = 5880`：一个证据数乘两个
#      单位换算常数，没有任何语义，必须挡）；
#   R4 常数只能出现在乘除位：可以作乘数或**除数**，但不能参与加减、也不能当被除数
#      （`12 ÷ (8 ÷ 10) = 15` 正是靠"常数当被除数"凑出来的）；
#   R5 整条式子必须至少含 1 个来自证据原文的数，否则只是常数之间的算术，与证据无关
#      （一层里 `10 × 12` 因此根本不会进索引）。
#
# 按此规则对证据 {49, 99, 8} 实测：39.2（`49÷10×8`）、79.2、`49×12=588`、
# `49+99=148`、`99-49=50`、`(49+99)×12=1776` 全部识别为推导；
# 而 9 / 60 / 97 / 23520 / 2352 / 15 / 5880 全部仍判无据。
# 冒烟里的 23,520 尤其关键：`50 × 49 × 12 × 0.8` 是三层乘法，超出两层预算直接落空，
# 常数集没有 50 是**第二道**独立防线（实测把 50 塞进常数集，23520 依然推导不出来——
# 第一道是深度上限，第二道是常数集，两道都不靠对方）。
#
# ## 常数集为什么是这四个
#
# 常数就是"允许凭空出现的数字"，每多一个，报告能在无据情况下算出的数就多一批。
# 代价是实测出来的（对证据 {49,99,8} 扫 1..300 整数，数"被误认成推导"的个数）：
#   - `10`（十进制）、`12`（月数）、`100`（百分）、`1000`（千）**全部免费**：
#     只留 {10,12} 是 23 个，加上 100/1000 仍是 23 个。R4 把它们限定在乘除位，
#     它们只是单位换算系数，配不出别的数来。这四个都留。
#   - `5`、`20` 这类**通用倍数**很贵：23 -> 41（+78%）。因为它能乘任何一个证据数，
#     `49 × 5 = 245` 立刻成立。凡是"乘一下就成立"的数，一律不进。
#   - `50`（团队规模）、`2024`（年份）这类**语义化**的数同样不进：它们不是换算系数，
#     是"编一个前提"最顺手的东西。真实冒烟里那句「以 50 人团队…年化 23,520 美元」
#     的 50 就是模型自己假设的，它必须只存在于报告里、绝不进入可推导的操作数池。
# --------------------------------------------------------------------------

# 推导允许使用的固定常数集。**只此一份**，不提供任意实数入口：
# value -> 含义（写进记录里，便于人工审计这个数是怎么来的）
DERIVATION_CONSTANTS: Dict[float, str] = {
    10.0: "十进制基数（8 折 = 8/10、小数位换算）",
    12.0: "月数（年化 = 月价 × 12）",
    100.0: "百分数基数（占比 = 部分/100）",
    1000.0: "千（每千席位、千位换算）",
}

# 浮点容差，**只有一个旋钮**：`_DERIVATION_KEY_DIGITS`（取整到小数点后 9 位）。
#
# 取 9 位是因为报告里的数字最多两位小数，这个精度比它细 7 个数量级：
# `39.3` 与 `39.2` 差 0.001，取整后落在不同键上，绝不会被当成同一个推导结果——
# 「近似命中不算推导」就是靠它落实的（放宽到 0 位，`39.3` 会被 `49÷10×8` 命中）。
# 同时它又足以吸收 IEEE754 的表示误差：`392/10` 与字面量 `39.2` 的差只有 1 个 ULP
# （相对 ~1e-16），取整后同键。
# 刻意不写成 `math.isclose(rel_tol=...)`：那会让比较变成线性扫描，把查表优势（O(1)）
# 换成一个名义上的旋钮——实测那次"把相对容差调到 0.1"的变异**一个测试都没红**，
# 因为真正把关的是取整键。容差旋钮必须同时是真正生效的那一个。
_DERIVATION_KEY_DIGITS = 9

# 操作数规模上限。只作为性能保险丝：R1（两层候选的中间结果表）是 O(操作数²)，
# 实测 62 个操作数时朴素扫描单个数字要 2 秒——报告校验是同步门禁，不能这么慢。
# 真实证据都是短片段（个位数到几十个数字），够不到这个上限；真超了就按数值升序截断，
# 保证结果与截断方式无关地确定。
MAX_DERIVATION_OPERANDS = 32


def _derivation_add(a: float, b: float) -> float:
    return a + b


def _derivation_sub(a: float, b: float) -> float:
    return a - b


def _derivation_mul(a: float, b: float) -> float:
    return a * b


def _derivation_div(a: float, b: float) -> Optional[float]:
    return None if b == 0 else a / b


# 运算表。符号用 ×/÷ 是为了推导式给人看时与报告写法一致。
_DERIVATION_OPS: Tuple[Tuple[str, Any], ...] = (
    ("+", _derivation_add),
    ("-", _derivation_sub),
    ("×", _derivation_mul),
    ("÷", _derivation_div),
)
_ADDITIVE_OPS = ("+", "-")


@dataclass(frozen=True)
class _Operand:
    """推导式里的一个操作数，连同它的出处。

    `label` 只渲染数值（`49`、`10`），常量含义不进推导式——
    否则记录会变成 `(49÷10（十进制基数…）)×8` 这种没法读的东西；
    哪个数来自常数集由记录里的 `constants` 字段交代，含义见 `DERIVATION_CONSTANTS`。
    """

    value: float
    is_constant: bool
    key: str

    @property
    def label(self) -> str:
        return _fmt_number(self.value)

    @property
    def evidence_id(self) -> Optional[str]:
        return None if self.is_constant else self.key


@dataclass(frozen=True)
class _Derivation:
    """一条已成立的推导式。中间结果也要留出处，深度 2 才能做操作数复用检查。"""

    expression: str
    operands: Tuple[float, ...]
    constants: Tuple[float, ...]
    evidence_ids: Tuple[str, ...]
    is_constant_only_operand: bool  # 该节点自身是否就是一个裸常数

    @property
    def used_keys(self) -> frozenset:
        """这个子式用过的**数值**。

        必须按值而不是按出处判断复用：`8/8` 是拿同一个**数**用两次；反之同一条证据里的
        `99 - 49` 是两个不同的数，合法。早先误用 evidence_id 当键，等于把 R2 写成了死代码。
        """
        return frozenset(self.operands)


def _derivation_operands(evidences: Sequence[Evidence]) -> List[_Operand]:
    """构造操作数池：所引证据的数值 + 固定常数集。

    按值去重（同值优先保留证据出处，这样 `10 × 12` 里 10 能算证据数），
    再按 (值, 是否常数, key) 排序固定遍历顺序——遍历顺序决定命中哪条推导式，
    必须与输入顺序、字典序、哈希随机化都无关，否则同输入不同输出，门禁就废了。
    """
    pool: Dict[float, _Operand] = {}
    for ev in evidences:
        for value in ev.numbers:
            if value not in pool:
                pool[value] = _Operand(value, False, ev.evidence_id)
    for value in DERIVATION_CONSTANTS:
        if value not in pool:
            pool[value] = _Operand(value, True, f"const:{value}")

    ordered = sorted(pool.values(), key=lambda o: (o.value, o.is_constant, o.key))
    if len(ordered) > MAX_DERIVATION_OPERANDS:
        ordered = ordered[:MAX_DERIVATION_OPERANDS]
    return ordered


def _fmt_number(value: float) -> str:
    """把浮点渲染成人读的短形式：49.0 -> 49，0.8 -> 0.8，39.2 -> 39.2。"""
    text = f"{value:.{_DERIVATION_KEY_DIGITS}g}"
    return text


def _safe_float(token: str) -> float:
    """数值 token -> float。抽出来的 token 一定可转 float，转不动就当 0（不会命中任何推导式）。"""
    try:
        return float(token)
    except ValueError:
        return 0.0


def _level_one_index(operands: Sequence[_Operand]) -> Dict[float, List[_Derivation]]:
    """预计算"一层运算"的全部合法结果：值 -> 推导式列表。

    这一层可以只算一次：同一句里多个待判定数字共用同一张表，
    实测把 O(数字 × 操作数²) 降到 O(操作数² + 数字 × 操作数)。
    """
    index: Dict[float, List[_Derivation]] = {}
    for left in operands:
        for right in operands:
            if left.value == right.value:
                continue  # R2：同一个数不得重复使用（挡 8/8）
            for op, fn in _DERIVATION_OPS:
                # R4：常数不能参与加减，也不能当被除数
                if op in _ADDITIVE_OPS and (left.is_constant or right.is_constant):
                    continue
                if op == "÷" and left.is_constant:
                    continue
                if left.is_constant and right.is_constant:
                    continue  # R3/R5：全是常数的算术与证据无关
                value = fn(left.value, right.value)
                if value is None:
                    continue
                key = round(value, _DERIVATION_KEY_DIGITS)
                index.setdefault(key, []).append(
                    _Derivation(
                        expression=f"{left.label}{op}{right.label}",
                        operands=(left.value, right.value),
                        constants=(left.value,) if left.is_constant else (
                            (right.value,) if right.is_constant else ()
                        ),
                        evidence_ids=tuple(
                            sorted({left.evidence_id, right.evidence_id} - {None})
                        ),
                        is_constant_only_operand=False,
                    )
                )
    return index


def _build_derivation(operands: Sequence[_Operand], index: Dict[float, List[_Derivation]],
                      target: float) -> Optional[_Derivation]:
    """在两层预算内反推：给定结果与外层操作数，中间值是**唯一确定**的，查表即可。

    正向枚举是 O(操作数³)，实测最坏 62 个操作数时单个数字要 2 秒；
    反推把它降到 O(操作数)，遍历顺序固定，命中哪条推导式也就固定。

    两层预算里的**一层**单独走查表：结果本身就是某个合法一层式子的值（`99 - 49 = 50`、
    `10 × 12 = 120`、`35% - 20% = 15 个百分点`），直接命中即可。方案文档举的两个例子
    恰好都是一层的，只做反推会把它们全部漏判成编造。
    """
    direct = index.get(round(target, _DERIVATION_KEY_DIGITS))
    if direct:
        return direct[0]
    for outer in operands:
        for op, _fn in _DERIVATION_OPS:
            for mid_on_left in (True, False):
                if op in _ADDITIVE_OPS and outer.is_constant:
                    continue  # R4：常数不参与加减
                if op == "÷" and not mid_on_left and outer.is_constant:
                    continue  # R4：常数不能当被除数
                if outer.value == 0 and op in ("×", "÷"):
                    continue
                # 反推中间值：mid op outer == target（mid_on_left）或 outer op mid == target
                if mid_on_left:
                    if op == "+":
                        needed = target - outer.value
                    elif op == "-":
                        needed = target + outer.value
                    elif op == "×":
                        needed = target / outer.value
                    else:
                        needed = target * outer.value
                else:
                    if op == "+":
                        needed = target - outer.value
                    elif op == "-":
                        needed = outer.value - target
                    elif op == "×":
                        needed = target / outer.value
                    else:
                        if target == 0:
                            continue
                        needed = outer.value / target
                for mid in index.get(round(needed, _DERIVATION_KEY_DIGITS), []):
                    if outer.value in mid.used_keys:
                        continue  # R2
                    if mid.constants and outer.is_constant:
                        continue  # R3
                    if mid_on_left:
                        expr = f"({mid.expression}){op}{outer.label}"
                    else:
                        expr = f"{outer.label}{op}({mid.expression})"
                    return _Derivation(
                        expression=expr,
                        operands=mid.operands + (outer.value,),
                        constants=mid.constants + ((outer.value,) if outer.is_constant else ()),
                        evidence_ids=tuple(sorted(set(mid.evidence_ids) | ({outer.evidence_id} - {None}))),
                        is_constant_only_operand=False,
                    )
    return None


def derive_number(
    token: str,
    evidences: Sequence[Evidence],
) -> Optional[Dict[str, Any]]:
    """判定句中这个数字能否由**所引证据**的数值推导得出。是则返回推导记录，否则 None。

    纯函数：不调 LLM、不读时钟、不用随机数，同样的 (token, evidences) 永远同样的结果。
    操作数只能来自 `evidences` 的数值集合或 `DERIVATION_CONSTANTS`——
    **不能**读整个 store，否则报告随便引一条证据就能把别处的数字借来当"前提"。
    """
    try:
        target = float(token)
    except ValueError:
        return None
    operands, index = _derivation_index(evidences)
    return _probe_derivation(operands, index, target)


def _derivation_index(
    evidences: Sequence[Evidence],
) -> Tuple[List[_Operand], Dict[float, List[_Derivation]]]:
    """一句话只建一次索引：多个待判定数字共用，省掉重复的 O(操作数²) 预处理。"""
    operands = _derivation_operands(evidences)
    return operands, _level_one_index(operands)


def _probe_derivation(
    operands: Sequence[_Operand],
    index: Dict[float, List[_Derivation]],
    target: float,
) -> Optional[Dict[str, Any]]:
    if not operands:
        return None
    found = _build_derivation(operands, index, target)
    if found is None:
        return None
    return {
        "value": _fmt_number(target),
        "expression": f"{found.expression} = {_fmt_number(target)}",
        "operands": [_fmt_number(v) for v in found.operands],
        "constants": [_fmt_number(v) for v in found.constants],
        "evidence_ids": list(found.evidence_ids),
    }


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
    derived_claims: List[Dict[str, Any]] = []
    for sentence, numbers in claims:
        cited = _cited_ids(sentence)
        if not cited:
            kind = classify_uncited(sentence)
            violations.append({
                "type": "uncited_claim",
                # kind 只决定**呈现措辞**：两类都照常计入 coverage、都照常可能导致
                # 不通过。缺口句不是免引用，只是"没指控系统该指控的事"。
                "kind": kind,
                # 前缀也必须跟着换。若缺口句仍顶着「含数值的结论未标注证据」，
                # 哪怕单列成组，读起来还是一句指控——而这句恰恰是模型在诚实认缺口。
                "detail": (
                    f"已标注的信息缺口（{_trim(sentence)}）" if kind == CLAIM_KIND_GAP_ACK
                    else f"含数值的结论未标注证据：{_trim(sentence)}"
                ),
            })
            # 无引用句一律不推导：没有出处就没有"可推导的证据"，
            # 推导能力不能顺带把缺引用洗成已核实。
            continue
        cited_claims += 1

        sources = [store.get(eid) for eid in cited]
        present = [ev for ev in sources if ev is not None]
        if not present:
            continue  # 未知 id 已在 validity 记过，不重复计入扎根性

        # 每个数字三选一：原文直接引用 / 由所引证据推导 / 无据。
        # 推导数不进 `missing`，因此不报 ungrounded_claim，也不拖累该句的扎根判定。
        operands, index = _derivation_index(present)
        missing: List[str] = []
        for n in numbers:
            if any(_grounded([n], ev) for ev in present):
                continue
            record = _probe_derivation(operands, index, _safe_float(n))
            if record is not None:
                derived_claims.append({"sentence": _trim(sentence), **record})
                continue
            missing.append(n)
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
        # 推导数值单列留痕，**不进** violations 也不改门槛：
        # 它们不是造假，是模型自己算出来的；记下来供人工审计，重生成提示词不必提。
        "derived_claims": derived_claims,
        "derived_claim_count": len(derived_claims),
        "claims_checked": len(claims),
        "citations_seen": len(all_cited),
        "evidence_count": len(store),
    }


def _trim(sentence: str, limit: int = 80) -> str:
    flat = " ".join(sentence.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# 违规类型的人话说明。给模型看英文标识符没有意义，它需要知道**要改什么**。
VIOLATION_HINTS = {
    "uncited_claim": "含数值的结论没有标注任何证据来源",
    "ungrounded_claim": "句中的数值在所引证据原文里找不到，可能是记错或臆造",
    "unknown_citation": "引用了不存在的证据 id（只能从下方证据表里选）",
    "hallucinated_url": "出现了本次未抓取到的 URL",
}


def format_violations(result: Dict[str, Any], limit: int = 3) -> str:
    """把违规清单渲染成可直接塞进重生成提示词的中文清单。

    **按类型聚合计数**，每类只留少量示例，不平铺全部条目。

    实测平铺是有害的：一次真实冒烟里同类指控刷了 60 条，提示词预算被"缺引用"
    三个字吃掉大半，真正稀缺的 `ungrounded_claim` 信号反而被截断挤出。模型需要的是
    「哪一类错、错了几处、典型长什么样」，不是逐条念罪状。
    """
    violations = result.get("violations") or []
    if not violations:
        return ""

    grouped: Dict[str, List[str]] = {}
    gap_ack: List[str] = []
    for item in violations:
        vtype = str(item.get("type") or "unknown")
        detail = str(item.get("detail") or "")
        # 缺口承认句单列：让模型去给"建议向厂商索取确认"补引用，既白烧重生成预算，
        # 补出来的引用在语义上也是错的（要引用的证据并不存在）。
        if vtype == "uncited_claim" and item.get("kind") == CLAIM_KIND_GAP_ACK:
            gap_ack.append(detail)
            continue
        grouped.setdefault(vtype, []).append(detail)

    fixable = len(violations) - len(gap_ack)
    headline = (
        f"【上一版报告存在的问题，必须逐条修正】共 {len(violations)} 处，"
        f"分 {len(grouped)} 类："
        if not gap_ack else
        f"【上一版报告存在的问题】共 {len(violations)} 处，"
        f"其中 {fixable} 处需要修正、{len(gap_ack)} 处是已标注的信息缺口："
    )
    lines = [headline]
    for vtype, details in grouped.items():
        hint = VIOLATION_HINTS.get(vtype, "需要修正")
        lines.append(f"- [{vtype}] {len(details)} 处：{hint}。示例：")
        for detail in details[:limit]:
            lines.append(f"    · {_trim(detail)}")
        if len(details) > limit:
            lines.append(f"    · ……该类另有 {len(details) - limit} 处，按同类问题一并修正")

    if gap_ack:
        lines.append(
            f"- [已识别的信息缺口] {len(gap_ack)} 处：这些句子在承认某项信息未公开/未披露。"
            "**不需要补引用**——要引用的证据本来就不存在。保留即可，示例："
        )
        for detail in gap_ack[:limit]:
            lines.append(f"    · {_trim(detail)}")
        if len(gap_ack) > limit:
            lines.append(f"    · ……该类另有 {len(gap_ack) - limit} 处，同样无需补引用")

    lines.append(
        "注意：不要靠删掉数字来规避——没有证据支撑的结论应当删除整句，"
        "而不是保留一个没有来源的断言。"
    )
    return "\n".join(lines)