"""依赖注入守卫：测试侧不得再 patch `mvp_workflow` 的模块属性。

注入改造最容易的失败方式，是漏迁一处、旧 patch 静默生效，于是测试变绿但
实际走的是另一条路径。这条只锁**替身怎么进去**：允许读模块常量、允许替换
实例属性（`workflow._fetch_all_competitors = ...`），但不再允许
`setattr(mvp_workflow, ...)` / `mvp_workflow.X = ...`。
"""
import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MODULE = "src.workflow.mvp_workflow"

# 批③已完成：网络边界注入点全部迁到构造参数，守卫覆盖全部模块属性。
PENDING = set()
# 整体替换被测类（server 侧的 MVPWorkflow）是测试边界，不是依赖替身，不在此列。
ALLOWED = {"MVPWorkflow"}


def _module_aliases(tree: ast.AST) -> set:
    aliases = {"mvp_workflow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.workflow":
            for alias in node.names:
                if alias.name == "mvp_workflow":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_setattr(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "setattr"
    return isinstance(func, ast.Attribute) and func.attr == "setattr"


def _offenders(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _module_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_setattr(node) and len(node.args) >= 2:
            target, name = node.args[0], node.args[1]
            symbol = name.value if isinstance(name, ast.Constant) else ""
            if isinstance(target, ast.Name) and target.id in aliases:
                found.append((node.lineno, target.id, symbol))
            elif (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and target.value.startswith(MODULE + ".")
            ):
                found.append((node.lineno, MODULE, target.value[len(MODULE) + 1:]))
        elif isinstance(node, ast.Assign):
            for item in node.targets:
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.value, ast.Name)
                    and item.value.id in aliases
                ):
                    found.append((node.lineno, item.value.id, item.attr))
    return found


def test_tests_do_not_patch_mvp_workflow_module_attributes():
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for lineno, target, symbol in _offenders(path):
            if symbol in PENDING or symbol in ALLOWED:
                continue
            offenders.append(f"{path.name}:{lineno} {target}.{symbol} = ...")
    assert not offenders, (
        "测试侧仍在 patch mvp_workflow 模块属性，请改为构造参数或实例注入：\n"
        + "\n".join(offenders)
    )
