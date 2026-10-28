"""证据锚定推导的校验规则测试。

这些规则是「模型说的借鉴点能不能进报告」的唯一闸门：
校验放过一条 = 报告里就多一条无据的话。所以每条规则都要有反例。
"""
import asyncio

import pytest

from src.workflow import borrowable_derivation as bd


def _competitor(name="美菜", url="https://meicai.test/", extracted_data=None):
    return {
        "name": name,
        "url": url,
        "status": "success",
        "extracted_data": extracted_data if extracted_data is not None else [
            {
                "key": "business_model",
                "value": [
                    "自建仓配网络，WMS 仓储系统与 TMS 调度协同",
                    "多温层仓库分别存放冷冻、冷藏与常温商品",
                ],
                "source_url": url,
                "confidence": 0.9,
            },
            {
                "key": "pricing",
                "value": "商户按品类与配送量报价",
                "source_url": url,
                "confidence": 0.9,
            },
        ],
    }


def _row(take="温区分层：冷链库位按温区独立", key="business_model",
         span="多温层仓库分别存放冷冻、冷藏与常温商品", platform="美菜"):
    return {
        "platform": platform,
        "items": [{"anchor_key": key, "anchor_span": span, "take": take}],
    }


def test_valid_anchor_is_kept():
    result = bd.validate([_row()], [_competitor()])
    items = result.items_by_platform["美菜"]
    assert len(items) == 1
    assert items[0].take.startswith("温区分层")
    assert items[0].evidence_id
    assert not result.rejected


def test_anchor_span_must_belong_to_the_anchored_item():
    """锚点 key 指向 pricing，片段却抄的是 business_model → 必须拒。

    只查"片段在不在某个证据里"是不够的：模型很容易把 A 的证据配给 B 的 key。
    """
    result = bd.validate(
        [_row(key="pricing", span="多温层仓库分别存放冷冻、冷藏与常温商品")],
        [_competitor()],
    )
    assert not result.items_by_platform
    assert result.rejected[0]["reason"] == "锚点片段不在证据原文中"


def test_unknown_anchor_key_is_rejected():
    result = bd.validate([_row(key="不存在的字段", span="随便")], [_competitor()])
    assert not result.items_by_platform
    assert "锚点 key 不存在" in result.rejected[0]["reason"]


def test_anchor_span_must_be_verbatim():
    result = bd.validate([_row(span="多温层仓库分别存放冷冻与常温")], [_competitor()])
    assert not result.items_by_platform
    assert result.rejected[0]["reason"] == "锚点片段不在证据原文中"


def test_take_with_unsupported_number_is_rejected():
    result = bd.validate([_row(take="先做 3 个仓，覆盖 20 个城市")], [_competitor()])
    assert not result.items_by_platform
    assert "数字" in result.rejected[0]["reason"]


def test_take_with_supported_number_is_kept():
    # 数字出现在锚点原文里 → 允许（数字有据）
    result = bd.validate(
        [_row(take="温区分层：按 2 个温区拆库位")],
        [_competitor(extracted_data=[{
            "key": "business_model",
            "value": "多温层仓库分别存放冷冻、冷藏与常温商品，2 个温区",
            "source_url": "https://meicai.test/",
        }])],
    )
    # 数字有据 → 放行
    assert result.items_by_platform.get("美菜")


def test_take_length_is_bounded():
    short = bd.validate([_row(take="借鉴")], [_competitor()])
    assert not short.items_by_platform
    long_take = "借" * (bd.MAX_TAKE_LENGTH + 5)
    long_result = bd.validate([_row(take=long_take)], [_competitor()])
    assert not long_result.items_by_platform


def test_duplicate_take_is_rejected():
    rows = [_row(), _row()]
    result = bd.validate(rows, [_competitor()])
    assert len(result.items_by_platform["美菜"]) == 1
    assert any("重复" in r["reason"] for r in result.rejected)


def test_max_items_per_platform():
    # 借鉴动作里不能带数字（数字无据）——用中文序号避免误触数字规则
    prefixes = ["甲", "乙", "丙", "丁", "戊", "己"]
    rows = [_row(take=f"借鉴{prefix}：动作内容需要足够长以便通过长度校验",
                 span="多温层仓库分别存放冷冻、冷藏与常温商品")
            for prefix in prefixes]
    result = bd.validate(rows, [_competitor()])
    assert len(result.items_by_platform["美菜"]) == bd.MAX_ITEMS_PER_PLATFORM


def test_unknown_platform_is_rejected():
    result = bd.validate([_row(platform="某不知名平台")], [_competitor()])
    assert not result.items_by_platform
    assert result.rejected[0]["reason"] == "平台未在证据集合中"


def test_fenced_json_is_parsed():
    payload = '```json\n{"rows": [{"platform": "美菜", "items": [{"anchor_key": "business_model", "anchor_span": "多温层仓库分别存放冷冻、冷藏与常温商品", "take": "温区分层：冷链库位按温区独立"}]}]}\n```'
    rows = bd._parse_payload(payload)
    assert rows[0]["platform"] == "美菜"


def test_derive_survives_llm_failure():
    class Boom:
        async def achat_completion(self, messages, **kwargs):
            raise RuntimeError("模型炸了")

    result = asyncio.run(bd.derive(Boom(), "美菜仓配模式", "", [_competitor()]))
    assert not result.ok
    assert "模型炸了" in result.error


def test_derive_without_usable_evidence_returns_empty():
    class Never:
        async def achat_completion(self, messages, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("没有证据时不该调模型")

    result = asyncio.run(bd.derive(Never(), "x", "", [{"name": "空", "status": "success", "extracted_data": []}]))
    assert not result.ok


def test_prompt_carries_self_context_and_intent():
    messages = bd.build_messages("美菜仓配", "我们做团餐供应链，缺冷链运力", [_competitor()])
    assert "<!--intent:borrowable-->" in messages[0]["content"]
    assert "我们做团餐供应链" in messages[1]["content"]
    assert "多温层仓库分别存放冷冻" in messages[1]["content"]


def test_matrix_prefers_anchored_items_over_rule_table():
    from src.workflow.competitor_matrix import build_matrix

    comp = _competitor()
    derived = {
        "美菜": [{
            "take": "温区分层：冷链库位按温区独立",
            "anchor_key": "business_model",
            "anchor_span": "多温层仓库分别存放冷冻、冷藏与常温商品",
            "evidence_id": "abc123",
            "source_url": "https://meicai.test/",
        }],
    }
    matrix = build_matrix("美菜仓配", competitors=[comp], derived_by_platform=derived)
    row = matrix.rows[0]
    assert any("温区分层：冷链库位按温区独立" in item for item in row.borrowable)
    assert any("证据锚定" in w for w in row.warnings)
    # 规则表那条（WMS 作业标准化）不该再出现
    assert not any("WMS 作业标准化" in item for item in row.borrowable)


def test_matrix_falls_back_to_rule_table_without_derivation():
    from src.workflow.competitor_matrix import build_matrix

    comp = _competitor()
    comp["extracted_data"].extend([
        {"key": "positioning", "value": "生鲜 B2B 供应链服务商", "source_url": comp["url"]},
        {"key": "as_of", "value": "2026-10-28", "source_url": comp["url"]},
    ])
    matrix = build_matrix("美菜仓配", competitors=[comp])
    row = matrix.rows[0]
    assert any("规则表" in w for w in row.warnings)
    assert row.borrowable and row.borrowable[0].startswith("推断：")
    # 兜底路径也必须是有效内容（有证据、有日期、推断已标）
    assert row.content_complete


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
