"""文档里的数字必须与夹具的实数一致（防止 README 再次落后于代码）。

起因：README 长期写着「14 条确定性 case」，而 `evals/cases/manifest.json` 早已是 20 条
（新增的 6 条缺口定向补抓没回填文档）。文档落后于代码是常态，但**数量类主张**
是读者第一眼会去数的东西——数不对，后面的话就都不必读了。

这类"文档声称的规模"必须有一处**唯一准数**（夹具），并且由测试把两边钉住。
"""
import io
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals" / "cases" / "manifest.json"


def _manifest_count() -> int:
    return len(json.loads(io.open(MANIFEST, encoding="utf-8").read())["cases"])


def test_中文README的评测case数与夹具一致():
    text = io.open(ROOT / "README.md", encoding="utf-8").read()
    m = re.search(r"(\d+)\s*条确定性 case", text)
    assert m, "README 里找不到「N 条确定性 case」这句话（改写了就同步改这条测试）"
    assert int(m.group(1)) == _manifest_count(), (
        f"README 写 {m.group(1)} 条，夹具实际 {_manifest_count()} 条"
    )


def test_英文README的评测case数与夹具一致():
    text = io.open(ROOT / "README_EN.md", encoding="utf-8").read()
    m = re.search(r"(\d+)\s+deterministic cases", text)
    assert m, "README_EN 里找不到「N deterministic cases」这句话（改写了就同步改这条测试）"
    assert int(m.group(1)) == _manifest_count(), (
        f"README_EN 写 {m.group(1)} 条，夹具实际 {_manifest_count()} 条"
    )
