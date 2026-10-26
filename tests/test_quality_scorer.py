"""质量评分器的维度识别：按规范数据 key 命中，不靠关键词子串撞。

回归背景：关键词表写 "price"、数据 key 是 "pricing"，而 "price" 不是 "pricing"
的子串，值（"标准版 $10/月"）也不含任何关键词 → 定价维度被误判成「❌ 缺失」，
16/20 个评测 case 的附录自评表因此批量造假，judge 只能照着错账扣分。
"""
from src.tools.quality_scorer import QualityScorer


def _comp(key: str, value: str) -> dict:
    return {"extracted_data": [{"key": key, "value": value,
                                 "source_url": "https://acme.test/x", "confidence": 0.9}]}


def test_pricing_detected_by_canonical_key():
    """key=pricing、value 不含任何关键词 → 仍必须判为有数据。"""
    rep = QualityScorer([_comp("pricing", "标准版 $10/月。")]).evaluate()
    pricing = next(d for d in rep.dimensions if d.key == "pricing")
    assert pricing.has_data is True
    assert pricing.data_count == 1
    assert "定价信息" not in rep.missing_dimensions


def test_missing_dimension_still_reported_when_no_data():
    """没有 pricing 数据时仍然要报缺失（别把修复写成"永远有数据"）。"""
    rep = QualityScorer([_comp("features", "支持任务管理与日历同步。")]).evaluate()
    assert "定价信息" in rep.missing_dimensions


def test_key_match_does_not_leak_across_dimensions():
    """pricing 的数据不能被算成别的维度有数据。"""
    rep = QualityScorer([_comp("pricing", "标准版 $10/月。")]).evaluate()
    by_key = {d.key: d for d in rep.dimensions}
    assert by_key["pricing"].has_data is True
    for other in ("features", "models", "target_users", "integrations",
                  "limitations", "reviews"):
        assert by_key[other].has_data is False, other
