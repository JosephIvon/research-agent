"""历史真实故障的回归测试：分解预算、未公开评分、搜索污染兜底。"""
import asyncio
import json

from src.tools.quality_scorer import QualityScorer
from src.workflow.mvp_workflow import CoordinatorAgent, DECOMPOSE_MAX_TOKENS


class _FakeLLM:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def achat_completion(self, prompt, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_decompose_passes_dedicated_large_max_tokens():
    """分解沿用客户端 4096 会被 <think> 吃光并静默退化单步骤。"""
    llm = _FakeLLM('```json\n[{"id":"1","description":"定价"},{"id":"2","description":"功能"}]\n```')
    steps = asyncio.run(CoordinatorAgent(llm).decompose_task("调研", []))

    assert [s["description"] for s in steps] == ["定价", "功能"]
    assert llm.kwargs["max_tokens"] == DECOMPOSE_MAX_TOKENS
    assert llm.kwargs["max_tokens"] > 4096


def test_decompose_accepts_single_object_but_drops_non_tasks():
    llm = _FakeLLM('{"id":"1","description":"单步"}')
    steps = asyncio.run(CoordinatorAgent(llm).decompose_task("调研", []))
    assert len(steps) == 1
    assert steps[0]["description"] == "单步"


def test_decompose_empty_or_non_list_falls_back_but_logs():
    """兜底仍然存在，但不能把空数组当成功返回。"""
    for bad in ("[]", '"not a list"', '{"foo": "bar"}'):
        steps = asyncio.run(CoordinatorAgent(_FakeLLM(bad)).decompose_task("调研", []))
        assert len(steps) == 1
        assert steps[0]["description"] == "分析目标网站内容"


def _comp(value, key="pricing"):
    return {"extracted_data": [{
        "key": key, "value": value, "source_url": "https://a.test/", "confidence": 0.9,
    }]}


def test_unpublished_is_not_counted_as_dimension_data():
    """“未公开”是缺口声明，不是数据；不能把全查不到的报告打满分。"""
    report = QualityScorer([_comp("未公开")]).evaluate()

    assert "定价信息" in report.missing_dimensions
    assert report.overall_score < 4.0
    assert report.is_usable is False


def test_real_value_still_counts():
    report = QualityScorer([_comp("标准版 49 元/月")]).evaluate()
    assert "定价信息" not in report.missing_dimensions
    assert report.dimensions[0].data_count == 1


class _SearchTool:
    pass


def test_polluted_bing_result_falls_back_to_searxng(monkeypatch):
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", ["searxng"])

    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/CVE-2024-1", "title": "CVE-2024-1", "content": "vulnerability"},
    ])
    monkeypatch.setattr(tool, "search_with_searxng", lambda q, n: [
        {"url": "https://meicai.test/", "title": "美菜", "content": "食材供应链"},
    ])

    results = tool.search("美菜", provider="bing")
    assert [r["url"] for r in results] == ["https://meicai.test/"]


def test_all_irrelevant_results_return_empty_not_pollution(monkeypatch):
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/x", "title": "CVE", "content": "security"},
    ])

    assert tool.search("美菜", provider="bing") == []


def test_long_chinese_query_keeps_relevant_results(monkeypatch):
    """长中文整句不能把结果全过滤掉——那是比污染更糟的静默失效。"""
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {
            "url": "https://meicai.test/",
            "title": "美菜 - 生鲜 B2B 供应链",
            "content": "美菜自建仓配网络，川为冻品按区域定价，仓配模式与定价打法对比",
        },
    ])

    query = "对比美菜与川为冻品的仓配模式与定价打法"
    results = tool.search(query, provider="bing")
    assert [r["url"] for r in results] == ["https://meicai.test/"]


def test_long_chinese_query_drops_unrelated_results(monkeypatch):
    """长查询同样要挡住无关页面：靠二元组覆盖度，不靠整串匹配。"""
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/x", "title": "CVE-2024", "content": "vulnerability advisory"},
    ])

    query = "对比美菜与川为冻品的仓配模式与定价打法"
    assert tool.search(query, provider="bing") == []


def test_login_returns_a_token_and_audits_the_real_user_id(monkeypatch):
    """登录曾因审计行引用未定义的 user_id 直接 500——把登录挡在门外。

    端点里 `principal["id"]` 才是登录人的 id；`user_id` 那个名字在这段作用域里
    根本没有定义 → NameError → 500 → 前端拿不到 token，整个界面进不去。
    """
    from fastapi.testclient import TestClient

    import server

    class _StubDB:
        def authenticate(self, username, password):
            return {"id": "user-42", "role": "admin"}

    audited = []

    class _Audit:
        def log(self, event, user_id, payload):
            audited.append((event, user_id, payload))

    monkeypatch.setattr(server, "get_user_db", lambda: _StubDB())
    monkeypatch.setattr(server, "get_audit_logger", lambda: _Audit())

    with TestClient(server.app) as client:
        r = client.post("/auth/login", json={"username": "admin", "password": "pw"})

    assert r.status_code == 200, r.text
    assert r.json()["access_token"], "登录必须拿到 token"
    assert audited, "登录成功必须留审计记录"
    assert audited[0][1] == "user-42", f"审计记的用户不对：{audited}"


def test_history_detail_restores_structured_fields_from_db(monkeypatch, tmp_path):
    """从历史里重新打开一份报告，界面曾显示"0 个竞品 / F 级 / 来源状态为空"。

    报告正文落在磁盘的 .md 里，**结构化结果只存在数据库**。冷加载路径此前只按
    markdown 拼载荷，把 score / grade / competitors 一律写成 None——库里明明是
    A 级、2 家竞品，界面却说 0 个竞品、F 级。
    """
    from fastapi.testclient import TestClient

    import server
    from src.auth.jwt_auth import create_access_token

    report_id = "research_report_20260101_000000_abcdef12"
    (tmp_path / f"{report_id}.md").write_text("# 标题\n\n正文内容\n", encoding="utf-8")
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))

    class _StubDB:
        def get_report(self, rid):
            assert rid == report_id
            return {
                "id": rid,
                "quality_score": 10.0,
                "quality_grade": "A",
                "metadata_json": json.dumps({
                    "competitors": [
                        {"name": "甲公司", "url": "https://a.test/"},
                        {"name": "乙公司", "url": "https://b.test/"},
                    ],
                    "missing_dimensions": ["reviews"],
                    "verification_score": 9.0,
                }),
            }
    monkeypatch.setattr(server, "get_report_db", lambda: _StubDB())

    token = create_access_token({"sub": "admin-user", "username": "admin", "role": "admin"})
    with TestClient(server.app) as client:
        r = client.get(
            f"/research/history/{report_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["score"] == 10.0, "质量分必须从库里取回"
    assert body["grade"] == "A", "等级必须从库里取回"
    assert [c["name"] for c in body["competitors"]] == ["甲公司", "乙公司"], "竞品必须从库里取回"
    assert body["missing_dimensions"] == ["reviews"]
    # 库里只存成功抓到的竞品；不补 status 的话前端默认显示「失败」——那是假的。
    assert [c["status"] for c in body["competitors"]] == ["success", "success"]


def test_history_detail_without_db_row_still_serves_markdown(monkeypatch, tmp_path):
    """库里没有这一行时（文件兜底报告），仍要能打开——只是结构化字段为空。"""
    from fastapi.testclient import TestClient

    import server
    from src.auth.jwt_auth import create_access_token

    report_id = "research_report_20260101_000001_abcdef12"
    (tmp_path / f"{report_id}.md").write_text("# 标题\n\n正文内容\n", encoding="utf-8")
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))

    class _EmptyDB:
        def get_report(self, rid):
            return None

    monkeypatch.setattr(server, "get_report_db", lambda: _EmptyDB())

    token = create_access_token({"sub": "admin-user", "username": "admin", "role": "admin"})
    with TestClient(server.app) as client:
        r = client.get(
            f"/research/history/{report_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert "正文内容" in body["markdown"]
    assert body["competitors"] is None, "没有库里数据时不得编造竞品"


def test_persisted_report_metadata_keeps_per_competitor_detail(tmp_path, monkeypatch):
    """逐家明细必须落库。

    此前只存 name + url，于是从历史里重新打开报告，「来源状态」每行都显示
    "0 条信息 / 0 个站内页"——读的时候根本没有任何明细可显示。
    """
    from src.storage import database as dbmod
    from src.workflow.mvp_workflow import MVPWorkflow

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)

    report_id = "research_report_20260101_000002_abcdef12"
    state = {
        "report_id": report_id,
        "task_id": "task-0001",
        "user_query": "调研一下",
        "user_id": "u1",
        "status": "completed",
        "report_final": "# 报告\n",
        "quality_score": 8.0,
        "quality_grade": "A",
        "missing_dimensions": ["reviews"],
        "verification_score": 9.0,
        "competitors": [
            {
                "name": "甲公司",
                "url": "https://a.test/",
                "status": "success",
                "extracted_data": [
                    {
                        "key": "pricing",
                        "value": "99 元/月",
                        "source_url": "https://a.test/pricing",
                        "confidence": 0.9,
                        "模型多吐的字段": "不该原样落库",
                    },
                ],
                "raw_pages": [{"url": "https://a.test/", "content_length": 100}],
                "discovered_urls": ["https://a.test/pricing"],
                "failed_pages": [{"url": "https://a.test/x", "error": "TimeoutError", "elapsed_seconds": 1.5}],
                "error_message": None,
            },
            {"name": "抓失败的", "url": "https://b.test/", "status": "failed"},
        ],
    }

    # 落库这一步不碰模型，注入桩客户端即可，免得测试依赖真实 API key
    MVPWorkflow(llm_client=object())._persist_to_sqlite(state)

    row = dbmod.get_report_db().get_report(report_id)
    assert row is not None, "报告没落库"
    meta = json.loads(row["metadata_json"])
    assert len(meta["competitors"]) == 1, "只落库抓成功的竞品"

    rec = meta["competitors"][0]
    assert rec["name"] == "甲公司"
    assert rec["url"] == "https://a.test/"
    assert rec["status"] == "success"
    assert rec["raw_pages"] == [{"url": "https://a.test/", "content_length": 100}]
    assert rec["discovered_urls"] == ["https://a.test/pricing"]
    assert rec["failed_pages"][0]["error"] == "TimeoutError"
    item = rec["extracted_data"][0]
    assert item["key"] == "pricing" and item["value"] == "99 元/月"
    assert item["source_url"] == "https://a.test/pricing"
    assert "模型多吐的字段" not in item, "只留规范字段，别让模型多吐的字段撑大 metadata"


def test_brand_display_name_reads_the_brand_out_of_a_structured_value():
    """模型把品牌名吐成 dict 时，不能把整个字面量当品牌名显示。"""
    from src.tools.comparison_tool import brand_display_name, infer_competitor_name

    assert brand_display_name({"brand": "旺店通", "company": "北京掌上先机"}) == "旺店通"
    assert brand_display_name({"site_name": "viso.ai", "brand": "Viso"}) == "Viso"
    assert brand_display_name("Blue Yonder") == "Blue Yonder"
    assert brand_display_name(None) == ""
    assert brand_display_name({"unrelated": 1}) == ""

    # 抽取出的 name 项是 dict 时，竞品名取其中的品牌名
    extracted = [{"key": "name", "value": {"brand": "金蝶制造云", "solution": "云解决方案"}}]
    assert infer_competitor_name("https://sjzerp.test/", extracted) == "金蝶制造云"


def test_brand_display_name_also_untangles_a_stringified_dict():
    """旧报告里存的是「对象被 str() 之后的字符串」，也得还原出品牌名。

    推断品牌名时做过一次 str()，于是落库的是 "{'brand': '旺店通', ...}" 这样的
    字符串——不是 dict，光认 dict 修不了历史数据。
    """
    from src.tools.comparison_tool import brand_display_name

    stored = "{'brand': '慧策旺店通（旺店通）', 'company': '北京掌上先机网络科技有限公司'}"
    assert brand_display_name(stored) == "慧策旺店通（旺店通）"

    # 不是 dict 字面量的普通字符串原样返回，别硬还原
    assert brand_display_name("Blue Yonder") == "Blue Yonder"
    assert brand_display_name("{不是字典}") == "{不是字典}"
