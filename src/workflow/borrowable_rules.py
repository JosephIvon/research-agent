"""可借鉴点的确定性推导规则（竞品矩阵第三列）。

为什么需要它
------------
竞品矩阵的“可借鉴点”是**我们**的产品判断，不是竞品官方事实，所以它必须
永远标“推断”。但它不能交给模型在报告里自由发挥——模型自由写“可借鉴”会
编造动作，而且事后无法核对。这里用一张**可审计的规则表**做映射：

    模式要点（有证据的原文） → 命中规则 → 借鉴动作（推断，附来源证据 id）

没有命中规则就留空，由矩阵标“待产品判断（无直接证据）”，**不硬凑**。
规则表本身也是资产：产品策略变了，改这张表即可，不用改代码逻辑。

两条硬约束
----------
1. 动作文案里**不允许出现数字**——数字要靠证据原文佐证，规则文案里写死
   数字等于制造“无据数字”，会污染矩阵的未核实标记。
2. 动作必须是**可执行动作**（“做什么”），不是“对手有什么”的复述。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.agent.citations import make_evidence_id

# 动作文案里的数字会让“未核实”判定失真（见模块 docstring 硬约束 1）
_DIGIT_RE = re.compile(r"\d")

# 一条模式要点最多产出几条借鉴点：多于此会让“可借鉴”列变成长清单，喧宾夺主。
MAX_RULES_PER_TEXT = 2


@dataclass(frozen=True)
class BorrowableRule:
    """一条映射规则：命中关键词 → 借鉴动作。"""

    rule_id: str
    match: Tuple[str, ...]
    action: str
    domain: str

    def hits(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(keyword.lower() in lowered for keyword in self.match)


# 规则表按“先具体后通用”排序：一条模式要点只取第一条命中的规则。
RULES: Tuple[BorrowableRule, ...] = (
    BorrowableRule(
        rule_id="multi_warehouse",
        match=("多仓", "仓网", "中心仓", "前置仓", "区域仓", "仓配一体", "多中心"),
        action="多仓分层 + 外协运力：按服务半径分层建仓，远距离城市交外协干线",
        domain="仓配网络",
    ),
    BorrowableRule(
        rule_id="service_radius",
        match=("服务半径", "覆盖城市", "覆盖全国", "城市覆盖", "次日达城市"),
        action="服务半径分层：按半径切服务承诺与运费，密度不够的城市不承诺时效",
        domain="仓配网络",
    ),
    BorrowableRule(
        rule_id="outsourced_freight",
        match=("外协", "外包运力", "第三方物流", "货运平台", "承运商"),
        action="外协运力分层：干线找外协、城配自营，按线路与温区谈价并留考核",
        domain="运力组织",
    ),
    BorrowableRule(
        rule_id="cold_chain",
        match=("冷链", "温区", "冷藏", "冷冻", "冷库", "温控", "常温"),
        action="温区分层作业：温区独立库位与独立拣货线，出库校验温区不串货",
        domain="仓配网络",
    ),
    BorrowableRule(
        rule_id="wms",
        match=("wms", "仓储系统", "仓库管理", "库位", "上架", "库存管理"),
        action="WMS 作业标准化：库位/批次/上架全流程留痕，库存可追到批次与库位",
        domain="仓储作业",
    ),
    BorrowableRule(
        rule_id="coding_trace",
        match=("一物一码", "贴码", "二维码", "溯源", "扫码", "条码"),
        action="一物一码：入库贴码 + 全链扫码，溯源链从供应商到签收不断点",
        domain="仓储作业",
    ),
    BorrowableRule(
        rule_id="unpack_weighing",
        match=("拆包", "拆箱", "称重", "净重", "计重", "分装"),
        action="拆包与称重作业：支持拆箱零售与双单位库存，缺重当场留证",
        domain="仓储作业",
    ),
    BorrowableRule(
        rule_id="routing",
        match=("排线", "路线规划", "智能调度", "tms", "配载", "车辆调度"),
        action="智能排线 + 车型配载：按温区与线路拼车，调度结果可复算",
        domain="运输调度",
    ),
    BorrowableRule(
        rule_id="cutoff_promise",
        match=("截单", "次日达", "当日达", "次日晨达", "时效承诺", "配送时效"),
        action="截单时点 + 时效承诺：把截单时间写进报价与下单页，超时责任先讲清",
        domain="运输调度",
    ),
    BorrowableRule(
        rule_id="city_delivery",
        match=("城配", "同城配送", "即时配送", "最后一公里"),
        action="同城城配：接单后按时间窗排车，签收动作前置到客户侧",
        domain="运输调度",
    ),
    BorrowableRule(
        rule_id="mini_program",
        match=("小程序", "商城", "app 下单", "移动端下单", "线上商城"),
        action="商户端小程序下单：把报价、库存、时效一次给全，减少人工改单",
        domain="交易前台",
    ),
    BorrowableRule(
        rule_id="repeat_purchase",
        match=("常购", "一键复购", "购物车", "清单", "历史订单"),
        action="常购清单 + 一键复购：把复购路径压到两步，常购占比做核心指标",
        domain="交易前台",
    ),
    BorrowableRule(
        rule_id="marketplace",
        match=("入驻商", "商家入驻", "平台入驻", "第三方商家", "供应商入驻"),
        action="入驻商准入 + 分账结算：入驻商走独立费率与账期，货权与结算分开",
        domain="平台生态",
    ),
    BorrowableRule(
        rule_id="dealer_channel",
        match=("经销商", "分销", "代理", "渠道商", "加盟"),
        action="经销/分销层级价：按层级价 + 区域价，价差由系统统一维护",
        domain="渠道结构",
    ),
    BorrowableRule(
        rule_id="credit_terms",
        match=("账期", "月结", "授信", "垫资", "金融", "先货后款"),
        action="授信 + 账期：额度按客户分级审批，到期前提醒，逾期进催收清单",
        domain="结算与风控",
    ),
    BorrowableRule(
        rule_id="reconciliation",
        match=("对账", "开票", "发票", "月度结算", "账单"),
        action="月度对账 + 合并开票：对账单按周期自动生成，争议有明细可查",
        domain="结算与风控",
    ),
    BorrowableRule(
        rule_id="aftersale",
        match=("售后", "退换货", "退货", "赔付", "缺重", "异常处理"),
        action="售后窗口前置：到货限时可申诉，缺重/短少先赔后追责",
        domain="售后信任",
    ),
    BorrowableRule(
        rule_id="quality_cert",
        match=("资质", "许可证", "检验报告", "检疫", "认证", "食品安全"),
        action="资质与报告上链：每品绑报告，缺证即禁售，采购前可查",
        domain="合规资质",
    ),
    BorrowableRule(
        rule_id="pricing_strategy",
        match=("低价", "毛利", "价格战", "补贴", "特价", "薄利"),
        action="分层定价 + 毛利护栏：低价走量要绑定品类毛利底线与起订量",
        domain="价格策略",
    ),
    BorrowableRule(
        rule_id="promotion",
        match=("促销", "满减", "满赠", "秒杀", "特价菜"),
        action="阶梯促销：促销价与毛利护栏同时生效，防止越促销越亏",
        domain="价格策略",
    ),
    BorrowableRule(
        rule_id="self_supply",
        match=("自营", "厂商直配", "产地直采", "直供", "自有工厂"),
        action="产地直采 + 自营仓：把货权与品控握在自己手里，差价空间留在内部",
        domain="供应链结构",
    ),
    BorrowableRule(
        rule_id="erp_integration",
        match=("erp", "财务系统", "金蝶", "用友", "系统对接", "api"),
        action="ERP 只做总账、业务层自建：交易明细留在业务库，总账按凭证同步",
        domain="系统架构",
    ),
    BorrowableRule(
        rule_id="data_ops",
        match=("报表", "看板", "数据分析", "bi", "预警", "监控"),
        action="看板先给结论再给明细：KPI 带目标与异常队列，异常条目直达处理页",
        domain="数据运营",
    ),
    BorrowableRule(
        rule_id="digital_tools",
        match=("自研软件", "管理软件", "数字化管理", "小程序助手", "作业端"),
        action="作业端分工：商城端下单、作业端扫码，一线不碰后台系统",
        domain="系统架构",
    ),
)

RULE_BY_ID: Dict[str, BorrowableRule] = {rule.rule_id: rule for rule in RULES}


def derive_borrowable(
    pairs: Sequence[Tuple[str, Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """从模式要点推导可借鉴点。

    ``pairs`` 是 ``(模式要点文本, 抽取条目)``；条目用于取来源 key 与证据 id。
    返回的每条都带 ``rule_id``（可追溯到规则表）、``action``（动作文案）、
    ``evidence_id``（来源证据）与 ``source_key``（来源抽取键）。

    命中不到的文本不产出任何动作——宁可留“待产品判断”，也不硬凑。
    """
    derived: List[Dict[str, Any]] = []
    seen_rules: set = set()
    for text, item in pairs:
        value = str(text or "").strip()
        if not value:
            continue
        # 一条模式要点可能同时命中多个域（"小程序下单 + 入库贴码分拣"），
        # 取最多两条不同域的规则：一条模式要点只出一条借鉴点会丢信息。
        hits = 0
        for rule in RULES:
            if not rule.hits(value):
                continue
            if rule.rule_id in seen_rules:
                continue
            seen_rules.add(rule.rule_id)
            derived.append({
                "rule_id": rule.rule_id,
                "action": rule.action,
                "domain": rule.domain,
                "source_key": str((item or {}).get("key", "") or ""),
                "evidence_id": _evidence_id(item),
            })
            hits += 1
            if hits >= MAX_RULES_PER_TEXT:
                break
    return derived


def _evidence_id(item: Optional[Dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    url = str(item.get("source_url", "") or "").strip()
    key = str(item.get("key", "") or "").strip()
    value = str(item.get("value", "") or "").strip()
    if not url or not value:
        return ""
    return make_evidence_id(url, key, value)
