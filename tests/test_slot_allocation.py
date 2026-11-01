"""名额分配：must_include 修复 / 提名审计 / MMR 开关 / 名额账。

背景（2026-11-01 第九轮实测）：
- 80 个名额里 90% 给了模型提名的产品、10% 给 C3 通道、**0% 给纯搜索**；
- 计划里的 must_include（"必须包含 XX"）**12 轮命中数 0** —— 被静默忽略；
- 赛道全集 289 条里只有 8% 的**名字**含赛道词、31% 的名字+证据含赛道词
  ⇒ "没有赛道证据就挡掉"会错杀 69% 的真竞品 ⇒ 审计只能**降权**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.workflow import admission_rules as ar  # noqa: E402
from src.workflow import research_plan as rp  # noqa: E402
from src.workflow.track_scope import same_track_verdict  # noqa: E402


def _plan(**kwargs):
    plan = rp.ResearchPlan(entity_name="猫箱", track_keywords=["AI角色扮演"])
    plan.competitor_candidates = list(kwargs.get("nominees") or [])
    criteria = dict(kwargs.get("criteria") or {})
    criteria.setdefault("max_count", int(kwargs.get("max_count", 8)))
    criteria.setdefault("min_count", int(kwargs.get("min_count", 2)))
    plan.criteria = criteria
    return plan


def _cand(name, url, description="", site_type="product_site", dims=0):
    return {"name": name, "url": url, "description": description,
            "dimension_count": dims, "site_type": site_type}


# -- must_include ---------------------------------------------------------

def test_must_include_clause_is_split_into_names():
    assert rp.parse_must_include(["星野、筑梦岛"]) == ["星野", "筑梦岛"]
    assert rp.parse_must_include(["Talkie, Character.AI"]) == ["Talkie", "Character.AI"]


def test_must_include_description_clause_stays_inert():
    """描述性条款拆出来的整句不能匹配任何品牌（否则会误伤）。"""
    out = rp.parse_must_include(["有可核验的官网、官方说明页或应用商店页面"])
    assert out == ["有可核验的官网", "官方说明页或应用商店页面"]
    assert not rp._match_named("星野", out)


def test_named_competitor_takes_a_slot():
    """被点名的产品必须真的进名单（这是修复前的 bug：命中数恒为 0）。"""
    plan = _plan(criteria={"must_include": ["星野、筑梦岛"]})
    candidates = [
        _cand("Alpha", "https://alpha.example/"),
        _cand("星野", "https://xingye.example/"),
        _cand("筑梦岛", "https://zhumengdao.example/"),
    ]
    out = rp.admit_competitors(candidates, plan)
    names = [s["name"] for s in out["selected"]]
    assert "星野" in names and "筑梦岛" in names
    assert out["slot_ledger"]["must_include_hit"] == 2


def test_named_competitor_is_kept_even_with_a_lower_score():
    """点名优先：分数低的也要占名额（用户点名了就要有）。"""
    plan = _plan(criteria={"must_include": ["小众产品"]})
    candidates = [
        _cand("大牌", "https://big.example/"),
        _cand("小众产品", "https://niche.example/"),
    ]
    out = rp.admit_competitors(candidates, plan)
    assert "小众产品" in [s["name"] for s in out["selected"]]


# -- 提名审计 -------------------------------------------------------------

def test_nominated_candidate_without_track_evidence_is_penalized_not_blocked():
    """点名了但没有任何赛道证据 ⇒ 降权，**不挡**（92% 的真竞品都这样 ✗）。"""
    plan = _plan(nominees=["某没听过的小众"], criteria={"admission_rule": rp.DEFAULT_ADMISSION_RULE})
    candidates = [_cand("某没听过的小众", "https://niche.example/", description="一个产品")]
    out = rp.admit_competitors(candidates, plan)
    item = out["scored"][0]
    assert item["gate_passed"] is True
    assert item.get("audit_flag")
    assert item["score"] == pytest.approx(0.5 + 0.25 - rp.NOMINATION_AUDIT_PENALTY)


def test_registry_nominated_candidate_is_not_penalized():
    """名册里的品牌有正面证据 ⇒ 不降权。"""
    plan = _plan(nominees=["星野"], criteria={"admission_rule": rp.DEFAULT_ADMISSION_RULE})
    candidates = [_cand("星野", "https://xingye.example/", description="一个产品")]
    out = rp.admit_competitors(candidates, plan)
    item = out["scored"][0]
    assert not item.get("audit_flag")
    assert item["score"] == pytest.approx(0.75)


def test_general_assistant_nomination_is_rejected():
    """通用助手（ChatGPT）被点名也挡掉 —— 决策：提名审计要挡非赛道产品。"""
    plan = _plan(nominees=["ChatGPT"], criteria={"admission_rule": rp.DEFAULT_ADMISSION_RULE})
    candidates = [_cand("ChatGPT", "https://chatgpt.example/", description="通用 AI 助手")]
    out = rp.admit_competitors(candidates, plan)
    item = out["scored"][0]
    assert item["gate_passed"] is False
    assert any("非本赛道" in r for r in item["gate_reasons"])


def test_clone_with_assistant_word_in_name_is_not_rejected():
    """"ChatGPT 角色扮演"是本赛道的克隆产品，不能因为名字带 chatgpt 就被挡。"""
    plan = _plan(nominees=["ChatGPT 角色扮演"],
                 criteria={"admission_rule": rp.DEFAULT_ADMISSION_RULE})
    candidates = [_cand("ChatGPT 角色扮演", "https://clone.example/", description="AI角色扮演")]
    out = rp.admit_competitors(candidates, plan)
    assert out["scored"][0]["gate_passed"] is True


def test_registry_candidate_passes_the_track_check():
    verdict, reason = same_track_verdict(name="星野", url="https://www.ai-xingye.cn/")
    assert verdict == "pass"


def test_unknown_track_verdict_for_plain_brand():
    verdict, _ = same_track_verdict(name="某个没听过的小众", url="https://unknown.example/")
    assert verdict == "unknown"


# -- MMR 开关 -------------------------------------------------------------

def _scored_rows():
    return [
        {"url": "https://a.example/", "name": "星野", "score": 0.75, "gate_passed": True,
         "description": "AI角色扮演应用"},
        {"url": "https://b.example/", "name": "Character.AI", "score": 0.75, "gate_passed": True,
         "description": "AI角色扮演应用"},
        {"url": "https://c.example/", "name": "小众A", "score": 0.5, "gate_passed": True,
         "description": "AI角色扮演应用"},
    ]


def test_mmr_switch_off_is_the_default_and_identical_to_plain_selection():
    plain = rp.select_competitors(_scored_rows(), max_count=2)
    off = rp.select_competitors(_scored_rows(), max_count=2, diversity=None)
    assert [i["url"] for i in plain] == [i["url"] for i in off]


def test_mmr_with_lambda_one_is_equivalent_to_plain_selection():
    """λ=1.0 时 MMR 退化成现行规则（逐条等价 ✓）。"""
    plain = rp.select_competitors(_scored_rows(), max_count=3)
    mmr = rp.select_competitors(_scored_rows(), max_count=3,
                                diversity={"lam": 1.0, "similarity": "name"})
    assert [i["url"] for i in plain] == [i["url"] for i in mmr]


def test_mmr_keeps_host_dedup_and_score_floor():
    rows = _scored_rows() + [
        {"url": "https://a.example/about", "name": "星野 关于", "score": 0.75,
         "gate_passed": True, "description": ""},
        {"url": "https://low.example/", "name": "低分", "score": 0.1,
         "gate_passed": True, "description": ""},
    ]
    out = rp.select_competitors(rows, max_count=5, min_score=0.5,
                                diversity={"lam": 0.6, "similarity": "name"})
    urls = [i["url"] for i in out]
    assert "https://a.example/about" not in urls      # 同主机只算一家
    assert "https://low.example/" not in urls          # 低于地板的不进


def test_mmr_description_similarity_mode_runs():
    out = rp.select_competitors(_scored_rows(), max_count=2,
                                diversity={"lam": 0.5, "similarity": "description"})
    assert len(out) == 2


# -- 名额账 ---------------------------------------------------------------

def test_slot_ledger_records_sources_and_displaced():
    plan = _plan(nominees=["星野"], criteria={"must_include": ["筑梦岛"]}, max_count=2)
    candidates = [
        _cand("星野", "https://xingye.example/"),
        _cand("筑梦岛", "https://zhumengdao.example/"),
        _cand("落选者", "https://loser.example/"),
    ]
    out = rp.admit_competitors(candidates, plan)
    ledger = out["slot_ledger"]
    sources = {s["name"]: s["source"] for s in ledger["slots"]}
    assert sources["星野"] == "模型提名"
    assert sources["筑梦岛"] == "计划点名"
    assert ledger["source_counter"]["模型提名"] == 1
    assert ledger["source_counter"]["计划点名"] == 1
    assert any(d["name"] == "落选者" and d["why"] == "名额已满"
               for d in ledger["displaced"])


def test_selected_items_carry_their_slot_source():
    plan = _plan(nominees=["星野"])
    out = rp.admit_competitors([_cand("星野", "https://xingye.example/")], plan)
    assert out["selected"][0]["source"] == "模型提名"
