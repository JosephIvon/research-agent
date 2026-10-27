"""追问答案写回报告：点「添加到报告」必须真的落到报告正文里。

改造前 `FollowupView.addLastAnswerToReport()` 只弹了个 toast——报告一个字没变，
用户以为记上了，回头看报告还是原样。这组用例钉住三件事：

- 正文真的追加了追问与答案（不是只弹提示）；
- 数据库快照同步更新，且**不冲掉**同一行的完整度评估与分数；
- 越权用户改不动别人的报告（404，且文件内容不变）。

为什么不另存新版本：另存一份会让任务—报告的关联断在新报告上（成本卡、
Trace 链接都挂在任务上），页面上的「返回报告」也会退回没追加过的旧报告。
"""
import pytest
from fastapi.testclient import TestClient

import server
from tests.conftest import TEST_USER_ID

REPORT_ID = "research_report_20260101_120000_aaaaaaaa"
OTHER_REPORT_ID = "research_report_20260101_120001_bbbbbbbb"
QUESTION = "定价策略有什么共同点？"
ANSWER = "三家都按席位计费，Acme 最便宜。"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """把数据库与输出目录隔离到 tmp_path，避免污染真实数据。"""
    from src.auth import user_db as usermod
    from src.storage import database as dbmod

    monkeypatch.setenv("USER_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setattr(usermod, "_user_db", None)
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path / "output"))

    db = dbmod.get_report_db()
    try:
        yield db
    finally:
        monkeypatch.setattr(dbmod, "_report_db", None)
        monkeypatch.setattr(usermod, "_user_db", None)


@pytest.fixture
def client():
    return TestClient(server.app)


def _register_report(db, report_id: str, owner, content: str = "# 竞品报告\n\n原始正文") -> str:
    """同时写入文件与数据库，模拟正常产出的报告。"""
    out = server._output_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{report_id}.md").write_text(content, encoding="utf-8")
    db.save_report(
        report_id,
        "task-x",
        "竞品分析",
        report_content=content,
        quality_score=8.5,
        quality_grade="B",
        metadata={"competitors": ["Acme"]},
        user_id=owner,
    )
    return report_id


def _append(client, headers, report_id: str = REPORT_ID, question: str = QUESTION, answer: str = ANSWER):
    return client.post(
        f"/research/reports/{report_id}/followup",
        json={"question": question, "answer": answer},
        headers=headers,
    )


def _file_text(report_id: str = REPORT_ID) -> str:
    return (server._output_dir() / f"{report_id}.md").read_text(encoding="utf-8")


def test_answer_lands_in_the_report_file(storage, client, auth_headers):
    """答案必须真的写进报告文件——只弹 toast 等于什么都没做。"""
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    response = _append(client, auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report_id"] == REPORT_ID, "报告 id 不能变——变了任务—报告链路就断了"

    text = _file_text()
    assert "原始正文" in text, "原正文必须保留，不能被追问覆盖"
    assert QUESTION in text, "追问问题必须落进报告"
    assert ANSWER in text, "答案必须落进报告"


def test_db_snapshot_updates_without_wiping_metadata(storage, client, auth_headers):
    """数据库快照要跟着更新，但不能把同行的分数与完整度评估冲掉。

    save_report 的 ON CONFLICT 会把 metadata_json / quality_score 一并覆盖，
    这里锁住的是"只改正文"这条边界。
    """
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    assert _append(client, auth_headers).status_code == 200

    row = storage.get_report(REPORT_ID)
    assert row is not None, "报告行不见了"
    assert ANSWER in (row["report_content"] or ""), "数据库快照没跟上正文"
    assert row["quality_score"] == 8.5, "追加一次追问就把报告分数冲掉了"
    assert row["quality_grade"] == "B", "追加一次追问就把报告等级冲掉了"
    assert "Acme" in (row["metadata_json"] or ""), "追加一次追问就把报告 metadata 冲掉了"


def test_other_user_cannot_append_to_someone_elses_report(storage, client, other_user_headers):
    """越权用户必须被挡在门外，且报告内容一个字都不能变。"""
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    response = _append(client, other_user_headers)
    # 404 而不是 403：403 等于告诉对方"这份报告存在"
    assert response.status_code == 404, f"期望 404，实际 {response.status_code}"
    assert ANSWER not in _file_text(), "越权请求把别人的报告改了"


def test_two_appends_accumulate_instead_of_overwriting(storage, client, auth_headers):
    """追加是累积的：第二次追问不能让第一次的补充消失。"""
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    assert _append(client, auth_headers).status_code == 200
    assert _append(
        client, auth_headers, question="功能差异呢？", answer="Acme 少了审计日志。"
    ).status_code == 200

    text = _file_text()
    assert QUESTION in text and ANSWER in text, "第一次追问的补充被覆盖了"
    assert "功能差异呢？" in text and "Acme 少了审计日志。" in text, "第二次追问没落进去"


def test_append_to_a_missing_report_returns_404(storage, client, auth_headers):
    """报告不存在时不能凭空造一份出来。"""
    response = _append(client, auth_headers, report_id=OTHER_REPORT_ID)
    assert response.status_code == 404
    assert not (server._output_dir() / f"{OTHER_REPORT_ID}.md").exists()


def test_append_requires_both_question_and_answer(storage, client, auth_headers):
    """空追问或空答案必须被拒——否则报告里会多出空章节。"""
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    assert _append(client, auth_headers, question="").status_code == 422
    assert _append(client, auth_headers, answer="").status_code == 422
    assert "原始正文" in _file_text()
    assert "追问补充" not in _file_text(), "被拒的请求不该写进报告"


def test_appended_section_declares_it_is_not_citation_checked(storage, client, auth_headers):
    """这一节必须自己声明"未参与引用校验"——否则读者会以为整篇都验过。"""
    _register_report(storage, REPORT_ID, TEST_USER_ID)

    assert _append(client, auth_headers).status_code == 200

    text = _file_text()
    assert "未参与引用校验" in text, "追问补充段必须声明自己的校验状态"
