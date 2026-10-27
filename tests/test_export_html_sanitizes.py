"""导出 HTML 不得把报告正文里的裸 HTML 当活标记渲染。

报告正文含抓取来的网页内容与模型产出（不可信输入）。Python-Markdown 默认把裸
HTML 原样放行——实测 `<script>` 与 `<img onerror=...>` 都会原样进入导出文件，
等于把提示注入升级成导出文件里的脚本执行（前端有 DOMPurify，导出这条没有）。

这组用例钉住三件事：裸 HTML 一律转义、机器附录标记不露成文本、
正常 Markdown 照常渲染（净化不能把报告本身弄坏）。
"""
from src.tools.export import export_to_html


def _export(tmp_path, content: str) -> str:
    out = tmp_path / "report.html"
    export_to_html(content, str(out))
    return out.read_text(encoding="utf-8")


def test_script_tag_is_neutralised(tmp_path):
    """脚本标签不能原样进导出文件。"""
    html = _export(tmp_path, "# 报告\n\n<script>alert(1)</script>\n")
    assert "<script>" not in html, "脚本标签原样进了导出文件"
    assert "&lt;script&gt;" in html, "裸 HTML 应转义成文本"


def test_inline_event_handler_is_neutralised(tmp_path):
    """带事件处理器的内联标签同样不能活。"""
    html = _export(tmp_path, "# 报告\n\n<img src=x onerror=alert(1)>\n")
    assert "<img" not in html, "带事件处理器的标签原样进了导出文件"
    assert "&lt;img" in html, "裸 HTML 应转义成文本"


def test_machine_appendix_markers_do_not_leak_as_text(tmp_path):
    """机器附录标记是内部元数据，转义后不能漏成可见文本。"""
    html = _export(tmp_path, "# 报告\n\n<!--machine-appendix-->\n\n## 附录\n\n<!--/machine-appendix-->\n")
    assert "machine-appendix" not in html, "内部标记漏成了可见文本"
    assert "附录" in html, "附录内容本身必须保留"


def test_normal_markdown_still_renders(tmp_path):
    """净化不能把报告本身弄坏：标题、加粗、行内代码、表格都得照常渲染。"""
    html = _export(tmp_path, "# 标题\n\n**加粗** 与 `code`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1" in html
    assert "<strong>加粗</strong>" in html
    assert "<code>code</code>" in html
    assert "<table>" in html


def test_markdown_images_still_render(tmp_path):
    """Markdown 图片语法不是裸 HTML——净化不能把它一起干掉。"""
    html = _export(tmp_path, "![图](https://example.com/a.png)\n")
    assert '<img alt="图" src="https://example.com/a.png"' in html
