#!/usr/bin/env python3
"""qcheck - deterministic-only quality gate (runs in every tool, git hook and CI).

设计约束（见 quality-rules.md；在仓内快照里跑时那份规则的抽取版是 .quality/rules.min.md）：
  - QR-00：这里**只有确定性检查**。任何 LLM 评审结论都是提示，不进本文件。
  - QR-11：每条检查的强度（阻断/提示）从 quality-rules.md 的表里读，
          不在本文件重复定义——策略只有一个权威源。
  - QR-15：外部件缺失 = 退出码 2 并说明缺什么，绝不静默跳过（静默跳过等于 CI 上空跑）。
  - QR-12：命令分支判"仓内有没有执行载体"，不判本机 PATH 找不找得到（0.4.0 起）——
          机器相关的判定进阻断位，同一个 commit 会在两台机上得到两个结论。

用法：
    python qcheck.py --staged                 # git 暂存区（pre-commit 用）
    python qcheck.py --changed BASE_SHA        # 相对某提交的改动（CI 增量闸门用）
    python qcheck.py --all [ROOT]             # 整仓
    python qcheck.py FILE...                  # 指定文件
    python qcheck.py --all --json             # SARIF 2.1.0 结构化输出
退出码：0=无阻断项 / 1=有阻断项 / 2=环境或依赖缺失
快照自检：与 VENDOR.json 同目录运行时（即 .quality/qcheck.py），先比对自己的 md5。
对不上就是闸门本体在仓里被手改过，exit 2 不产出判定——被改过的闸门报绿不算绿。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "0.4.0"
HERE = Path(__file__).resolve().parent

TEXT_SUFFIXES = {".py", ".md", ".json", ".sh", ".ps1", ".cmd", ".bat", ".toml",
                 ".yaml", ".yml", ".js", ".ts", ".csv", ".txt"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".hf-cache",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
             ".idea", ".trae", ".qoder", "data", ".uv-cache",
             # .scratch/ 是一次性探针和第三方 README 副本：不是交付物，扫它只会把闸门
             # 的精确率数字做成假的（80 处 QR-15 命中全在这里）。跳过必须在表头显式说出来，
             # 否则就是一个没人知道的盲区（QR-15 的"静默跳过"判据用在工具自己身上）。
             ".scratch", ".ipynb_checkpoints"}
MAX_BYTES = 2_000_000
OPT_OUT = "qcheck:allow"

SEVERITY_RE = re.compile(r"^\|\s*(QR-\d+)\s*\|[^|]*\|[^|]*\|\s*([^|]+?)\s*\|", re.M)


def read_rules(path: Path) -> dict[str, str]:
    """rule id -> 强度原文（"阻断" / "提示" / "阻断（仅运行时可判…）"）。"""
    if not path.is_file():
        sys.stderr.write("FATAL: rules file missing: %s\n"
                         "  qcheck 的强度策略只在 quality-rules.md 里定义，缺它就没有判定依据；\n"
                         "  要么补回该文件，要么用 --rules-file 指明位置。\n" % path)
        sys.exit(2)
    text = path.read_text(encoding="utf-8-sig")
    sev = {}
    for rid, cell in SEVERITY_RE.findall(text):
        sev.setdefault(rid, cell.split("（")[0].split("(")[0].strip())
    return sev


def load_lines(path: Path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, "unreadable: %s" % exc
    if len(raw) > MAX_BYTES:
        return None, "skipped: file larger than %d bytes" % MAX_BYTES
    try:
        return raw.decode("utf-8-sig").splitlines(), None
    except UnicodeDecodeError:
        return None, "skipped: not utf-8 decodable (binary)"


def find(path: list, rule: str, message: str) -> None:
    path.append({"rule": rule, "file": None, "line": None, "message": message})


# ---------------------------------------------------------------- QR-15
PATH_RE = re.compile(r"(?<![\w])([A-Za-z]:[\\/]{1,2}[A-Za-z0-9_.~\-\\/ ]{3,}|\\\\wsl\$[\\/]\S+)")
# `os.environ.get("PROGRAMFILES", r"C:\Program Files")` 里的第二个实参是**标准目录的
# 回退默认值**，不是"这台机的数据根"：换任何一台 Windows 机它都对，而且环境变量优先。
# 把它算成硬编码会逼作者写成更难读的形状（msr 的 Blender 探测就是这个形状）。
# 只豁免"紧跟在 getenv 默认位上的那一段字面量"，同一行别处的绝对路径照旧命中。
ENV_DEFAULT_RE = re.compile(r"(?:os\.environ\.get|os\.getenv|environ\.setdefault)\(\s*"
                            r"[\"'][A-Za-z_]+[\"']\s*,\s*[rbuf]{0,2}[\"']")


def qr15_hardcoded_paths(rel: str, lines: list, out: list) -> None:
    if rel.suffix.lower() in {".md", ".txt", ".json"}:
        return  # 文档里的历史绝对路径是事实记录（memory 条目 7 只要求标注）；
                # .json 多为机器生成的测量记录，里面记着当次输入路径同样是事实而非策略
    for i, line in enumerate(lines, 1):
        if OPT_OUT in line or line.lstrip().startswith("#"):
            continue
        m = PATH_RE.search(line)
        if not m:
            continue
        env_default = ENV_DEFAULT_RE.search(line)
        # 逐个命中判：同一行里"getenv 的默认值"豁免，别处的绝对路径照旧报。
        hits = [h for h in PATH_RE.finditer(line)
                if not (env_default and h.start() >= env_default.end() - 1)]
        if not hits:
            continue
        out.append({"rule": "QR-15", "file": rel, "line": i,
                    "message": "hardcoded machine-absolute path %r; use runtime "
                               "resolution (Path(__file__).parents[N]) or an env var"
                               % hits[0].group(1)})


# ---------------------------------------------------------------- QR-17
BUDGET_RE = re.compile(r"\b([A-Z_]*(?:MAX|BUDGET|LIMIT)[A-Z_]*)\s*=\s*\d+")
# 只有"预算名指向一段可被截断的文本/字节载荷"才算 QR-17 的注入通道。列表 top-k
# （_MAX_UNIQUE_FEATURES、GLOBAL_DEFAULT_LIMIT）丢的是条目、本来就是设计行为，
# 把它们算进来会让每个 [:MAX_X] 切片都变命中——实测 research-agent 三处列表截断就是这样误伤的。
PAYLOAD_BUDGET_RE = re.compile(r"(?:CHARS|BYTES|TOKENS|WORDS|LINES|LENGTH|LEN|SIZE|"
                               r"CONTEXT|PROMPT|PAYLOAD|TEXT|MEMORY|BUDGET)", re.I)
# 只认 `X[:MAX_*]` 这种"用预算常量切的裸切片"。QR-17 的依据事故（F16）就是这个形状
# （body[:MAX_CHARS]）。带字面量上界的 `rows[:5]` 是展示截断，不是注入通道，
# 把它算进来会让每一条 top-k 切片都变成命中——实测 serve.py 的 missing[:5] 就是这样误伤的。
BARE_SLICE_RE = re.compile(r"\b[\w.\[\]]+\s*\[\s*:\s*([A-Z_]*(?:MAX|BUDGET|LIMIT)[A-Z_]*)\s*\]")
OVERFLOW_REPORTED_RE = re.compile(
    r"rfind\(|splitlines|TRUNCAT|truncat|截断|溢出|超出预算|over.?budget", re.I)


def qr17_silent_truncation(rel, lines: list, out: list) -> None:
    if rel.suffix.lower() != ".py":
        return
    text = "\n".join(lines)
    budgets = {b for b in BUDGET_RE.findall(text) if PAYLOAD_BUDGET_RE.search(b)}
    if not budgets or OVERFLOW_REPORTED_RE.search(text):
        return
    for m in BARE_SLICE_RE.finditer(text):
        if m.group(1) not in budgets:
            continue
        out.append({"rule": "QR-17", "file": rel, "line": text[:m.start()].count("\n") + 1,
                    "message": "injection channel truncates silently: payload budget %s is cut by "
                               "bare slice %s with no line boundary and no overflow notice — "
                               "content past the budget vanishes without telling the reader "
                               "(fix: cut at a line boundary and state the loss in the header)"
                               % (m.group(1), m.group(0))})


# ---------------------------------------------------------------- QR-12
FENCE_RE = re.compile(r"^[ \t]*```[ \t]*([\w+-]*)[ \t]*$", re.M)
KNOWN_CLI = {"python", "python3", "py", "pip", "pip3", "git", "node", "npm", "npx", "uv",
             "ruff", "pytest", "mypy", "make", "curl", "wget", "docker", "gh", "conda"}
IGNORED_TOKENS = {"echo", "ls", "cat", "source", "export", "set", "for", "while", "if",
                  "then", "fi", "do", "done", "true", "python.exe", "py.exe"}
WRAPPERS = {"cd", "sudo", "time", "env"}
# 连字符必须在字符类里：本规则要抓的真实缺陷形状（`huggingface-cli`、`no-such-tool-qqq`）
# 全是带连字符的命令名，早先漏掉 "-" 会让 QR-12 对它自己的依据样例失明。
TOKEN_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]{1,30}$")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
PIPE_RE = re.compile(r"&&|;|\|")
# 判据 2026-10-29 由"本机 command -v 找得到"改成"仓内存在执行载体"（ADR 见 ledger W2d）。
# 原因：前者是机器相关的——同一个 commit 在装了 make 的机器上绿、在没装的笔记本上红，
# 拦的是"我这台机器缺什么"，不是"文档里的这一步不可复现"，违反 QR-00 的确定性前提。
# BASE_TOOLS 里的东西按"任何开发机/CI 镜像都有"的假设放过：它们的存在性不是本仓的缺陷形状。
BASE_TOOLS = {"python", "python3", "py", "pip", "pip3", "git", "node", "npm", "npx",
              "bash", "sh", "curl", "wget"}
# 这些解释器带脚本参数时，脚本本身必须在仓里——路径存在与否是可判定的，不因解释器常见而豁免。
INTERP_ARGS = {"python", "python3", "py", "bash", "sh", "node"}
# 带值的开关：`make -C sub target` 的 `sub` 不是目标名，不能当第一个操作数。
VALUE_FLAGS = {"-C", "-f", "--file", "--makefile", "-I", "-l", "-o"}
SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ps1", ".bash")


def looks_like_path(tok: str) -> bool:
    return ("/" in tok or "\\" in tok or tok.endswith(SCRIPT_SUFFIXES)) and "<" not in tok


class Carriers:
    """仓内"这一步怎么会存在"的证据集合。只在真的需要一个判定时才建（懒），按根目录缓存。

    载体只有四类，全是仓内可指认的工件：Makefile 目标 / 仓内脚本文件 / 依赖清单或安装行 /
    容器工件（Dockerfile、compose）。.scratch 与 data 明确不读：那里面是第三方 README 副本和
    数据集，让别人的 `apt-get install` 行来给我们的文档发载体，等于把判据反向外包。
    """

    PRUNE = SKIP_DIRS | {"logs", "outputs", ".quality"}

    def __init__(self, root: Path) -> None:
        self.targets: set[str] = set()
        self.declared: set[str] = set()
        self.artifacts: set[str] = set()
        self.makefiles = 0
        self.scanned = 0
        self.root = root
        self._walk()

    def _walk(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            depth = len(Path(dirpath).relative_to(self.root).parts)
            dirnames[:] = [d for d in dirnames
                           if d not in self.PRUNE and (not d.startswith(".") or d == ".github")]
            if depth >= 3:
                dirnames[:] = []
            for fn in filenames:
                self.scanned += 1
                if self.scanned > 20000:
                    return
                p = Path(dirpath) / fn
                if p.is_symlink() or not p.is_file():
                    continue
                if fn in ("Makefile", "makefile", "GNUmakefile") or fn.endswith(".mk"):
                    self.makefiles += 1
                    self.targets |= self._make_targets(p)
                elif fn in LOCK_TOOLS:
                    # 锁文件是工具链自己写下的工件：仓里有 uv.lock，"这仓用 uv 起环境"就是
                    # 仓内可指认的事实，不需要再找一条 `uv install` 行（uv 的动词是 sync/run）。
                    self.artifacts |= LOCK_TOOLS[fn]
                    if MANIFEST_RE.match(fn):
                        self.declared |= self._manifest_names(p, fn)
                elif MANIFEST_RE.match(fn):
                    self.declared |= self._manifest_names(p, fn)
                elif DOCKER_ARTIFACT_RE.match(fn):
                    self.artifacts |= {"docker", "docker-compose", "compose"}
                elif fn.endswith((".md", ".txt", ".sh", ".yml", ".yaml")):
                    self.declared |= self._install_lines(p)

    def _text(self, p: Path) -> str:
        try:
            raw = p.read_bytes()
        except OSError:
            return ""
        if len(raw) > MAX_BYTES:
            return ""
        return raw.decode("utf-8-sig", "replace")

    def _make_targets(self, p: Path) -> set:
        out = set()
        for m in re.finditer(r"^([A-Za-z][A-Za-z0-9_.\- ]*?):(?!=)", self._text(p), re.M):
            out.update(m.group(1).split())
        return out

    def _manifest_names(self, p: Path, fn: str) -> set:
        text = self._text(p)
        names = set()
        if fn.startswith(("requirements", "constraints")):
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                if m:
                    names.add(m.group(1))
        elif fn == "package.json":
            names.update(n.split("/")[-1] for n in
                         re.findall(r"\"([A-Za-z@][A-Za-z0-9@/._-]{1,40})\"\s*:\s*\"[\^~\d*]", text))
        else:
            names.update(re.findall(r"[\"']([A-Za-z][A-Za-z0-9._-]{1,30})[\"']", text))
            names.update(re.findall(r"^\s*-\s*([A-Za-z][A-Za-z0-9._-]{1,30})\b", text, re.M))
        return {normalize_pkg(n) for n in names}

    def _install_lines(self, p: Path) -> set:
        names = set()
        text = self._text(p)
        for m in INSTALL_HEAD_RE.finditer(text):
            names.add(normalize_pkg(m.group(1)))          # 安装器本身也是仓内声明的载体
            tail = re.split(r"&&|;|\|", m.string[m.end():].split("\n", 1)[0], maxsplit=1)[0]
            names.update(normalize_pkg(w) for w in tail.split()
                         if TOKEN_RE.match(w) and not w.startswith("-"))
            if len(names) > 4000:
                break
        return names


def normalize_pkg(name: str) -> str:
    return name.lower().replace("_", "-")


MANIFEST_RE = re.compile(r"^(requirements[-\w.]*\.txt|constraints[-\w.]*\.txt|pyproject\.toml|"
                         r"setup\.py|setup\.cfg|environment\.ya?ml|package\.json|Pipfile)$")
DOCKER_ARTIFACT_RE = re.compile(r"^(docker-compose[-\w.]*\.ya?ml|compose\.ya?ml|Dockerfile[-\w.]*)$")
# group(1) 是执行安装的包管理器：它自己也是仓内声明的一部分（`uv pip install …` 里仓主
# 明确说"这个仓用 uv 起环境"），所以一并进 declared，而不是把 uv/pipx 之类塞进 BASE_TOOLS
# 当"任何机器都有"——那是本机假设的复发。
INSTALL_HEAD_RE = re.compile(r"\b(pip3?|uv|poetry|pipx|conda|apt(?:-get)?|dnf|yum|npm|pnpm|yarn|"
                             r"cargo|choco|winget|brew|scoop)(?:\s+pip)?\b[^\n]*?\b(?:install|add)\b")
# 锁文件/环境文件本身就是"这个仓用 X 起环境"的仓内工件，不需要一条不存在的 `uv install` 行。
LOCK_TOOLS = {"uv.lock": {"uv"}, "poetry.lock": {"poetry"}, "Pipfile.lock": {"pipenv"},
              "conda-lock.yml": {"conda"}, "environment.yml": {"conda"},
              "package-lock.json": {"npm"}, "pnpm-lock.yaml": {"pnpm"}, "yarn.lock": {"yarn"},
              "Cargo.lock": {"cargo"}}
_CARRIERS: dict[str, Carriers] = {}
RUN_ROOT: Path | None = None


def carriers_for(root: Path) -> Carriers:
    key = str(root.resolve())
    if key not in _CARRIERS:
        _CARRIERS[key] = Carriers(root)
    return _CARRIERS[key]


def operand_after(args: list) -> str | None:
    for idx, a in enumerate(args):
        if a in VALUE_FLAGS:
            continue
        if a.startswith("-"):
            continue
        if ASSIGN_RE.match(a):
            continue
        if a.lower() in ("install", "run", "add"):
            continue
        if idx and args[idx - 1] in VALUE_FLAGS:
            continue      # 上一个开关的值，不是操作数
        return a
    return None


def script_in_repo(root: Path, rel, arg: str) -> bool:
    cleaned = arg.replace("\\", "/").rstrip("/;,.")
    bases = [root]
    if isinstance(rel, Path) and rel.parent != Path("."):
        bases.append(root / rel.parent)
    for base in bases:
        cand = base / cleaned
        try:
            cand.resolve().relative_to(root.resolve())
        except ValueError:
            continue        # 逃出仓根的路径不算载体
        if cand.exists():
            return True
    return False


def qr12_command_existence(rel, lines: list, out: list) -> None:
    if rel.suffix.lower() not in {".md", ".txt", ".sh"}:
        return
    root = RUN_ROOT or Path.cwd()
    in_fence = False
    reported: set[str] = set()     # 同一份文档里同一个命令只报一次：README 里 8 行 `make ...`
                                   # 是 1 个问题（文档教了一条仓内不存在的步骤），不是 8 个。
    for i, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        body = re.sub(r"^\s*(\$|>)\s*", "", line).strip()
        if not body or body.startswith("#") or OPT_OUT in line:
            continue
        for segment in PIPE_RE.split(body):
            tokens = segment.split()
            pos = 0
            while pos < len(tokens) and (ASSIGN_RE.match(tokens[pos]) or tokens[pos] in WRAPPERS):
                pos += 1
            if pos >= len(tokens):
                continue
            token = tokens[pos].strip("\"'")
            if not TOKEN_RE.match(token) or token in IGNORED_TOKENS:
                continue
            # 大写一律不当命令看：文档里的 WP-0 / WP-2.1 是工作包编号（出现在 ``` 围栏里画的
            # 依赖图上），不是可执行步；而本规则考察的命令名按约定全小写。
            if any(ch.isupper() for ch in token):
                continue
            if "-" not in token and token not in KNOWN_CLI:
                continue
            args = tokens[pos + 1:]
            op = operand_after(args)
            car = carriers_for(root)
            why = carrier_gap(token, op, root, rel, car)
            if why is None or token in reported:
                continue
            reported.add(token)
            hint = ("本机 PATH 可解析" if shutil.which(token) else "本机 PATH 也不可解析") if \
                token not in INTERP_ARGS else "本机 PATH 状态不参与本判定"
            out.append({"rule": "QR-12", "file": rel, "line": i,
                        "message": "%s (in-repo execution carrier required: Makefile target / repo "
                                   "script / declared dependency or install line / container "
                                   "artifact; %s，仅信息)" % (why, hint)})


def carrier_gap(token: str, op, root: Path, rel, car: Carriers):
    """返回缺陷描述，或 None 表示这一步在仓内有可指认的载体。"""
    low = normalize_pkg(token)
    if token in INTERP_ARGS and op and looks_like_path(op):
        if not script_in_repo(root, rel, op):
            return "documented script %r is not in the repo, so the step cannot be run" % op
        return None
    if token == "make":
        if op is None:
            return None if car.makefiles else "documented `make` with no Makefile anywhere in the repo"
        if op in car.targets:
            return None
        return "documented `make %s` but no Makefile in the repo defines that target" % op
    if token in BASE_TOOLS:
        return None
    if low in car.declared or low in car.artifacts or low in car.targets:
        return None
    return "documented command %r has no carrier in the repo: no Makefile target, no repo script, " \
           "no dependency manifest or install line, no Docker/compose artifact declares it" % token


# ---------------------------------------------------------------- QR-13
# 判据形状取自 research-agent 的真实事故（git show 4440599^:src/tools/quality_scorer.py）：
# 维度表里同时声明了规范字段名（"key": "pricing"）和关键词表（[..., "price"]），
# 存在性判断却只做 `kw.lower() in key` 的子串匹配——"price" 不是 "pricing" 的子串，
# 于是该维度永远读成"缺失"。修复（4440599）是在同一个 test 里加上 `dim["key"].lower() in key`，
# 所以"规范名有没有进入匹配集"才是可判定的分水岭；只比较字面量相近与否会把
# 修复前后两版判成同一个结果（实测如此），那是假的检出。
CANON_DECL_RE = re.compile(r"[\"']key[\"']\s*:\s*[\"']([A-Za-z][A-Za-z0-9_]{2,})[\"']")
NEEDLE_LIST_RE = re.compile(
    r"[\"'](?:keywords|synonyms|aliases|patterns|terms|nouns)[\"']\s*:\s*\[([^\]]*)\]", re.S)
LIST_LITERAL_RE = re.compile(r"[\"']([A-Za-z][A-Za-z0-9_]{2,})[\"']")
SUBSTR_ON_NAME_RE = re.compile(r"\bin\s+\w*(?:key|keys|field|fields|column|header|attr)\w*\b", re.I)
NAME_ATTRIBUTION_RE = re.compile(r"[\"']key[\"']\s*\]\s*(?:\.lower\(\)?)?\s+in\s")


def _near_miss_pairs(canonicals: set, needles: set) -> list[tuple[str, str]]:
    pairs = []
    for c in sorted(canonicals):
        for n in sorted(needles):
            if n == c or len(n) < 4 or c[:4] != n[:4]:
                continue
            if n in c or c in n:
                continue          # 子串能撞上就不是静默缺失，只是宽松
            pairs.append((c, n))
    return pairs


def qr13_substring_existence(rel, lines: list, out: list) -> None:
    if rel.suffix.lower() != ".py":
        return
    text = "\n".join(lines)
    if not SUBSTR_ON_NAME_RE.search(text):
        return
    if NAME_ATTRIBUTION_RE.search(text):
        return                    # 规范名已进入匹配集（修复后的形状）
    canonicals = set(CANON_DECL_RE.findall(text))
    if not canonicals:
        return
    needles = set()
    for body in NEEDLE_LIST_RE.findall(text):
        needles |= set(LIST_LITERAL_RE.findall(body))
    pairs = _near_miss_pairs(canonicals, needles)
    if not pairs:
        return
    line = text[:SUBSTR_ON_NAME_RE.search(text).start()].count("\n") + 1
    out.append({"rule": "QR-13", "file": rel, "line": line,
                "message": "existence test is substring matching on field names, but the declared "
                           "canonical keys never enter the match set; %s can never be matched by %s "
                           "(no substring relation) so those dimensions silently read as missing — "
                           "test the declared key, not only the keyword list"
                           % (", ".join(sorted({repr(c) for c, _ in pairs})),
                              ", ".join(sorted({repr(n) for _, n in pairs})))})


# ---------------------------------------------------------------- QR-14
def qr14_duplicate_columns(rel: str, lines: list, out: list) -> None:
    if rel.suffix.lower() != ".csv" or not lines:
        return
    header = [c.strip() for c in lines[0].split(",")]
    dupes = sorted({c for c in header if c and header.count(c) > 1})
    if dupes:
        out.append({"rule": "QR-14", "file": rel, "line": 1,
                    "message": "duplicate column names %s in CSV header; readers keyed by name "
                               "(csv.DictReader) silently keep only the last, so human labels get "
                               "ignored" % dupes})


# ---------------------------------------------------------------- QR-11
# 键名尾部先剥"方法词"（fraction/gcd/...），再看"单位词"（pct/count/...）。
# 只有 stem 相同、单位相同、scope 相同才算同一量的两处权威源——
# 早期版本直接砍掉最后一个 token 当 stem，把 `_count` 与 `_pct` 当成同名，
# 在一个真实锚文件上造出 2 个假阳性（3,911,644 vs 97.07 本来就是两个不同的量）。
METHOD_TOKENS = {"fraction", "gcd", "float", "rounded", "impl", "method", "2dp", "v1", "v2"}
UNIT_TOKENS = {"pct", "percent", "count", "total", "num", "n", "rate", "ms", "s"}
TOLERANCE = 0.01


def _stem_and_unit(key: str) -> tuple[str, str]:
    tokens = key.split("_")
    while len(tokens) > 1 and tokens[-1].lower() in METHOD_TOKENS:
        tokens.pop()
    unit = ""
    if len(tokens) > 1 and tokens[-1].lower() in UNIT_TOKENS:
        unit = tokens[-1].lower()
        tokens.pop()
    return "_".join(tokens), unit


def qr11_duplicate_authority(rel: str, lines: list, out: list) -> None:
    if rel.suffix.lower() != ".json":
        return
    try:
        data = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        return

    def walk(node):
        if isinstance(node, dict):
            grouped: dict[tuple[str, str], list[tuple[str, object]]] = {}
            for key, val in node.items():
                if isinstance(val, dict) and "value" in val and "scope" in val:
                    grouped.setdefault(_stem_and_unit(key), []).append((key, val))
                else:
                    walk(val)
            for (stem, unit), items in grouped.items():
                if len(items) < 2:
                    continue
                by_scope: dict[str, list[tuple[str, object]]] = {}
                for key, val in items:
                    by_scope.setdefault(str(val.get("scope")), []).append((key, val))
                for scope, same in by_scope.items():
                    nums = [(k, v["value"]) for k, v in same
                            if isinstance(v["value"], (int, float)) and not isinstance(v["value"], bool)]
                    if len(nums) < 2:
                        continue
                    lo = min(v for _, v in nums)
                    hi = max(v for _, v in nums)
                    if hi and (hi - lo) / abs(hi) > TOLERANCE:
                        out.append({"rule": "QR-11", "file": rel, "line": None,
                                    "message": "measure %r (unit=%r) with scope=%r has %d different "
                                               "authoritative values %s inside one file; a number "
                                               "may have exactly one authority, the rest must "
                                               "reference it or be renamed"
                                               % (stem, unit or "-", scope, len(nums),
                                                  ", ".join("%s=%s" % (k, v) for k, v in nums))})
        elif isinstance(node, list):
            for idx, val in enumerate(node):
                walk(val)

    walk(data)


# ---------------------------------------------------------------- QR-10
# 首版行级判据在 research-agent --all 打满 53 处，逐条分池后只有个位数是真缺陷，
# 三类占绝大多数且都是**判据自己的错**（2026-10-29 分池，见 quality-rules.md §D 条目 6）：
#   ① 阈值行：`≥ 80%`、`必须 100%` 是判据/门槛，本来就没有样本量；
#   ② 同行已带样本量：`16/19 (84%)`、`6 页→2 页`、`n=714`、`42,350` 都算分母在场；
#   ③ URL 百分号编码：`%E7%8B%AC` 里的 "7%" 被当百分比——先确认数字两侧不是
#      词字符或另一个百分号，再算百分比。
PCT_RE = re.compile(r"(?<![%\w.])\d+(?:\.\d+)?\s*%(?![%\w])")
ANNOTATION_RE = re.compile(
    r"\b\d+\s*/\s*\d+\b"                 # 分数写法本身就是样本量
    r"|\b\d+\s*→\s*\d+\b"                # 6 页→2 页 / (6→2)：分子分母都在场
    r"|\bn\s*="                          # n=714
    r"|\d{1,3}(?:,\d{3})+"               # 42,350 —— 千分位计数
    r"|\d+\s*(?:个|家|页|条|次|例|篇|张|款|人|词|问|句|遍|组|轮|片|uid)"  # 带量词的计数
    r"|全量|抽样|样本|分片|白名单|池子|sample|scope"
    # msr 池裁出来的第四类误报：对照基准写在同一行（`对官方 dev 自报，±10% 带内`、
    # `README 0.35895 ±10%`）。这类百分比是"相对某个已声明锚点的差"，不是"总体的率"，
    # 锚点在场就说明读者能回溯——要求再抄一遍样本量不是规则本意。
    r"|自报|口径|锚|带内|带外|对照|基准|\bvs\b|\bBAND\b|ABOVE_BAND", re.I)
# 阈值与"非测量百分比"一起剥掉再判：±10% 是容差带、95% CI 是置信水平，两者都没有样本量
# 这回事（msr 的 a5-t2ranking 与两份预注册表头就是这样被误伤的）。剥掉之后行里还有
# 百分比才算"报告了一个测量率"。
NON_RATE_PCT_RE = re.compile(r"[±]\s*\d+(?:\.\d+)?\s*%"                    # ±10% 容差带
                             r"|(?:≥|≤|>|<|>=|<=|不低于|不少于|必须|目标|阈值|门槛|上限|下限|"
                             r"floor|ceiling)\s*\d+(?:\.\d+)?\s*%"
                             # 英文侧同一件事：政策数字的写法是 "must be 100%" / "at least 90%"，
                             # 测量率的写法是 "we measured 100%"。动词决定语义，只认前者。
                             r"|(?:must(?:\s+(?:be|reach|exceed|hit|stay))?|should\s+be|at\s+least|"
                             r"no\s+more\s+than|required(?:\s+to\s+be)?|target(?:ed|ing)?|gate[d]?\s+at|"
                             r"threshold(?:ed)?(?:\s+at)?|cap(?:ped)?\s+at)\s*\d+(?:\.\d+)?\s*%"
                             r"|\d{1,3}(?:\.\d+)?\s*%\s*(?:CI|置信)"        # 95% CI
                             r"|\bCI\s*(?:95|99|90|68)\b", re.I)
# 表头声明的豁免分两种，都是"读者能回溯"的意思，但强度不同：
# ① 分母列（| 样本 | 数量 | n | 出处 |）——这批文档的约定就是把分母放表头，要求每行重抄
#    不是规则本意（research-agent 的 gap-refetch 表全走这个约定）；
# ② 政策列（| 阈值 | 门槛 | gate |）——同一张表里的百分比是**被规定的数**，根本没有样本量
#    这回事（research-agent ch04 的"留 10%"、ch06 的"不存在 99% 安全"两条残留误报的形状）。
#    政策列的豁免要让位给测量列：表头同时写"阈值"和"实测"时这张表谈的就是观测值，不豁免。
# **只有表块首行（表头）能开启豁免**：msr `spike-render-corpus.md` 实测过一条把豁免打穿的
# 写法——数据行里出现"口径""出处"（"视角协议要改口径"）就把同表后续所有行判成有分母，
# 三处真缺陷静默。表头与数据行的区别不是词表，是位置。
DENOM_SCOPE_RE = re.compile(r"样本|分母|计数|数量|规模|请求|口径|出处|来源|"
                            r"\b(?:requests?|counts?|n)\b", re.I)
POLICY_SCOPE_RE = re.compile(r"阈值|门槛|门禁|\b(?:targets?|thresholds?|gates?)\b", re.I)
MEASURED_COL_RE = re.compile(r"实测|测量|观测|实得|\b(?:measured|observed)\b", re.I)
SEP_ROW_RE = re.compile(r"\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|")


def qr10_unscoped_rate(rel: str, lines: list, out: list) -> None:
    if rel.suffix.lower() not in {".md", ".txt"}:
        return
    header_scoped: bool | None = None      # None = 当前不在表块里
    for i, line in enumerate(lines, 1):
        row = line.lstrip()
        if not row.startswith("|"):
            header_scoped = None           # 表块以非表格行结束
            continue
        if SEP_ROW_RE.search(row):
            continue                       # 分隔行既不是表头也不可能是数据行
        if header_scoped is None:
            header_scoped = bool(DENOM_SCOPE_RE.search(line)) or (
                bool(POLICY_SCOPE_RE.search(line)) and not MEASURED_COL_RE.search(line))
        if not PCT_RE.search(line):
            continue
        if not PCT_RE.search(NON_RATE_PCT_RE.sub("", line)):
            continue          # 这一行的百分比全是阈值/容差带/置信水平写法
        if ANNOTATION_RE.search(line) or header_scoped:
            continue          # 同行带了分母，或本表表头声明了分母列
        out.append({"rule": "QR-10", "file": rel, "line": i,
                    "message": "table row carries a measured percentage with no sample size or "
                               "scope in the same row (nor a denominator column in the table "
                               "header); a sampled value placed in a recompute slot silently "
                               "becomes a population claim"})


# ---------------------------------------------------------------- QR-18
# QR-18 管的是"写-审-改-再审"这种**由模型输出决定何时结束**的闭环。因此：
#   循环必须是无上界的（while True / while attempt < ...）——`for _ in range(n)` 本身
#   就已经带了最大轮次，那是停机条件的一种，不该命中；
#   "审"必须是真调用（review(...) / revise(...) / 评审），不是词形相邻的变量名——
#   实测 src/retrieval/metrics.py 的 bootstrap 循环 `for _ in range(n_resamples)`
#   被旧词表里的 "resampl" 误伤，那是统计量，不是智能体闭环。
LOOP_RE = re.compile(r"\bwhile\s+True\b|\bwhile\s+(?:attempt|round|tries|tries_count)\b")
REVIEWISH_RE = re.compile(r"\b(?:review|reviews|revise|revises|retry|retries|critique|judge)s?"
                          r"\s*\(|评审|再审", re.I)
STOPVOCAB_RE = re.compile(r"max_rounds|max_attempts|max_iter|max_retries|budget|deadline|"
                          r"no_progress|stall|give_up|轮次上限|token_budget", re.I)


def qr18_loop_without_stop(rel, lines: list, out: list) -> None:
    if rel.suffix.lower() != ".py":
        return
    text = "\n".join(lines)
    m_loop = LOOP_RE.search(text)
    if m_loop and REVIEWISH_RE.search(text) and not STOPVOCAB_RE.search(text):
        m_review = REVIEWISH_RE.search(text)
        out.append({"rule": "QR-18", "file": rel, "line": text[:m_loop.start()].count("\n") + 1,
                    "message": "unbounded review/revise loop (%s near line %d) with no stop "
                               "condition in this file — no max rounds, no token budget, no "
                               "no-progress exit; a closed loop without a stop condition "
                               "is a money burner"
                               % (m_loop.group(0), text[:m_review.start()].count("\n") + 1)})


# ---------------------------------------------------------------- QR-19
METRIC_RE = re.compile(r"准确率|检出率|召回率|覆盖率|precision|recall|accuracy|F1|NDCG|MAP@", re.I)
NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*%|\b\d{3,}\b")
# 行级判定试过：真实评测文档里同一条数往往把依据写在相邻行（"本机实现：ir_measures 0.4.3 对账"、
# "README 0.35895 ±10% 以内"），行级判据在 36 个文档上打出 70 处命中、多数是误伤。
# 误报会把闸门关掉，所以这里降到**文件级**：整份文档一次都没出现复算依据才算违规。
PROVENANCE_RE = re.compile(r"artifact|取数|复算|fixture|实测|命令|口径|来源|论文|对账|对拍|"
                           r"pytrec|ir_measures|锚|judge\.py|README", re.I)


def qr19_metric_without_provenance(rel: str, lines: list, out: list) -> None:
    if rel.suffix.lower() not in {".md", ".txt"}:
        return
    claims = 0
    for line in lines:
        if line.lstrip().startswith("#") or OPT_OUT in line:
            continue
        if METRIC_RE.search(line) and NUM_RE.search(line):
            claims += 1
    if claims and not PROVENANCE_RE.search("\n".join(lines)):
        out.append({"rule": "QR-19", "file": rel, "line": 1,
                    "message": "file states %d metric numbers (accuracy/recall/coverage style) "
                               "yet contains no recompute anchor anywhere (artifact id, the command "
                               "to rerun it, or 论文口径/本机实测口径 separation)" % claims})


CHECKS = [qr10_unscoped_rate, qr11_duplicate_authority, qr12_command_existence,
          qr13_substring_existence, qr14_duplicate_columns, qr15_hardcoded_paths,
          qr17_silent_truncation, qr18_loop_without_stop, qr19_metric_without_provenance]

RUNTIME_ONLY = [("QR-16", "接口需被真调过一次：这是运行时事实，静态检查看不见它。"
                          "由 CI 步骤或人真调一次结案，不在本工具静默跳过。"),
                # QR-12 有两个分支，本工具只实现了命令分支（载体判定，纯静态可判）。字段名/枚举
                # 分支要靠"对照上游 schema 实测快照"才能判——第六轮那个 `click` 枚举缺陷今天仍然
                # 只有人评能抓。让每次运行都打出这条，策略行就不会暗示存在一个没实现的检查。
                ("QR-12·字段名分支", "上游字段名/枚举值是否存在需要实测 schema 快照，静态检查看不见它。"
                          "由人对快照或运行时断言结案；本工具的 QR-12 只判命令分支（仓内执行载体）。")]

# 执行阶段登记表（不是策略！）：quality-rules.md 里 QR-10/QR-19 的强度仍是"阻断"，
# 但这两条静态近似的**误报率还没实测**，直接上阻断位会让开发第一次提交就撞墙、
# 结果是把闸门关掉。所以先报告不拦，攒够一轮真实命中/误报数据再用 --strict 升级。
# 每条必须写明"为什么还在观察"和"复查触发条件"，否则就会永久停在观察位。
OBSERVE = {
    # 这里刻意不写命中数：数字会随仓里文档增删而变，写死之后就没人再数一遍（QR-19 的
    # "未核数字不得引用"用在工具自己身上）。要数字就现算，命令在下面这条说明里。
    "QR-10": "行级判据已在 research-agent --all 上逐条分池并据此收紧（阈值写法/同行分母/"
             "URL 百分号/表头分母列四类误报已修，3 处真缺陷已回填分母）；"
             "material-search-rec 那一池还没裁，所以仍只报不拦。引用前先重算：python "
             ".quality/qcheck.py --root <仓> --rules-file .quality/rules.min.md --all ；"
             "升回阻断的前置条件：msr 池裁完 + 两仓真缺陷清零",
    "QR-19": "判据已从行级收紧到文件级（行级成批误伤，故降级为观察）；"
             "复查条件：文件级判据在两个试点仓跑满两周且零误伤",
}


def load_ignored(root: Path) -> list[str]:
    """.qcheck-ignore：每行一个**目录名**（不是 glob、不是路径），整棵树里同名目录都跳过。

    存在的理由：缺陷样例集（selftest/、F16/input/、F01/input/）里全是大意违规的样本，
    它们必须是故意的合法内容；扫到它们就让本仓 --all 永久红灯，而一个天天红的闸门
    训练出来的习惯是"无视闸门"。这个例外只在目录级，--staged 里同样生效，且必须在表头
    显式报出来（QR-15 的"静默跳过等于没有闸门"对工具自身同样适用）。
    """
    path = root / ".qcheck-ignore"
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        names.append(entry.rstrip("/\\").split("/")[-1].split("\\")[-1])
    return names


def verify_snapshot() -> str | None:
    """跑的是仓内快照（.quality/qcheck.py）时先验自己没被手改过。

    VENDOR.json 里的 qcheck_md5 是导出时刻的哈希；对不上就是有人在仓里改了闸门本体，
    此后任何"通过"都不再是 quality-rules.md 说了算——所以这里只有一条路：exit 2。
    权威源（tools/qcheck.py）旁边没有 VENDOR.json，直接返回 None，行为不变。
    """
    vendor = HERE / "VENDOR.json"
    if not vendor.is_file():
        return None
    try:
        data = json.loads(vendor.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write("FATAL: 快照台账 VENDOR.json 解析失败: %s\n"
                         "  台账坏了就无法证明这份闸门是原样导出的，拒绝判定。\n" % exc)
        sys.exit(2)
    recorded = data.get("qcheck_md5")
    actual = hashlib.md5(Path(__file__).resolve().read_bytes()).hexdigest()
    if recorded != actual:
        sys.stderr.write(
            "FATAL: 快照被改动 —— .quality/qcheck.py 与导出时（%s）的台账 md5 不一致\n"
            "  台账: %s\n  实际: %s\n"
            "  要么从权威源重新导出（python tools/sync-gate.py <仓>），要么撤掉这次改动；\n"
            "  闸门本体在被改过的状态下不产出任何判定（exit 2，不是 exit 1）。\n"
            % (data.get("exported", "?"), recorded, actual))
        sys.exit(2)
    return str(data.get("exported", "?"))


def git_z_list(root: Path, *args: str) -> tuple[int, list[str], str]:
    """跑一条带 -z 输出的 git plumbing，返回 (退出码, NUL 分隔的名字, stderr 文本)。
    两个必须点，都是这台机器上真炸过的：
      ① text=True 按本机 codepage（GBK）解码 git 的输出——仓库里有中文名文件时
         读管道线程直接 UnicodeDecodeError，proc.stdout 变成 None（"闸门崩在
         'AttributeError: NoneType.split'"，看起来像工具坏了，其实是编码）；
      ② 不加 -z 时 git 把非 ASCII 路径转成 `"book/chapters/\345\274\225…"` 的
         八进制引号形式，root/rel 拼出来的路径不存在——**改动集里的中文名文件
         会静默逃出闸门**。-z 输出原始 UTF-8，不引号。"""
    proc = subprocess.run(["git", "-C", str(root), *args, "-z"], capture_output=True)
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n.strip()]
    return proc.returncode, names, proc.stderr.decode("utf-8", "replace").strip()


def all_tree_files(root: Path, drop: set) -> list[Path]:
    """--all 的范围 = **有可能被推到远端的文件**，不是目录里的每个字节。
    .gitignore 掉的（一次性探针、生成的 e2e 报告）既进不了 commit 也进不了 CI，
    把它们算进本地债务会让本地数字和远端看到的数字不一致（实测 research-agent 的
    4 处 QR-15 全在 gitignored 的 output/_probe_*.py 里）。不在 git 工作树里、
    或 git 问不出来时，退回目录遍历，但必须明说这次连被忽略的文件也扫了。"""
    if shutil.which("git") is not None:
        code, names, err = git_z_list(root, "ls-files", "--cached", "--others",
                                      "--exclude-standard")
        if code == 0:
            files = []
            for rel in names:
                p = root / rel
                if (p.suffix.lower() in TEXT_SUFFIXES and p.is_file()
                        and not any(part in drop for part in Path(rel).parts)):
                    files.append(p)
            return files
        sys.stderr.write("  NOTICE: git ls-files 失败（%s）——本次按目录遍历，"
                         ".gitignore 掉的文件也在范围内。\n" % err[:120])
    else:
        sys.stderr.write("  NOTICE: git 不在 PATH——本次按目录遍历，"
                         ".gitignore 掉的文件也在范围内。\n")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in drop]
        for name in filenames:
            if Path(name).suffix.lower() in TEXT_SUFFIXES:
                files.append(Path(dirpath) / name)
    return files


def iter_targets(args) -> tuple[list[Path], Path]:
    root = Path(args.root or ".").resolve()
    ignored = load_ignored(root)
    drop = SKIP_DIRS | set(ignored)
    if args.staged or args.changed:
        if shutil.which("git") is None:
            sys.stderr.write("FATAL: %s 需要 git 但 git 不在 PATH\n"
                             % ("--changed" if args.changed else "--staged"))
            sys.exit(2)
        if args.changed:
            base = args.changed
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", base):
                sys.stderr.write(
                    "FATAL: --changed 要的是提交号（7–40 位十六进制），收到的是 %r。\n"
                    "  先解析：git rev-parse HEAD~1。只收 SHA 是因为日志要能事后回答\n"
                    "  '这次是和哪个提交比的'——HEAD~1 这类符号引用会随历史移动。\n"
                    "  基线拿不到就拒绝判定：CI 在不知道'和什么比'的情况下报绿是假阴性。\n" % base)
                sys.exit(2)
            cmd = ["diff", "--name-only", "--diff-filter=ACM", base]
            fail_msg = "git diff %s 失败（浅克隆？基线提交不在历史里？）" % base
        else:
            cmd = ["diff", "--cached", "--name-only", "--diff-filter=ACM"]
            fail_msg = "git diff --cached 失败"
        code, names, err = git_z_list(root, *cmd)
        if code != 0:
            sys.stderr.write("FATAL: %s: %s\n" % (fail_msg, err))
            sys.exit(2)
        files = [(root / f) for f in names]
        return [f for f in files if f.is_file()
                and not any(part in drop for part in f.parts[len(root.parts):])], root
    if args.all:
        return all_tree_files(root, drop), root
    paths = [Path(p) for p in args.paths]
    for p in paths:
        if not p.exists():
            sys.stderr.write("FATAL: path does not exist: %s\n" % p)
            sys.exit(2)
    files = []
    for p in paths:
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                if sub.is_file() and sub.suffix.lower() in TEXT_SUFFIXES and \
                        not any(part in drop for part in sub.parts):
                    files.append(sub)
        else:
            # 显式点名的文件一律要扫，哪怕它落在 .qcheck-ignore 的目录里——
            # 自检就是靠这一条来扫缺陷样例集的；例外只对"目录级遍历"生效。
            files.append(p)
    return files, root


def to_sarif(findings, severities) -> dict:
    rules, results = [], []
    for rule in sorted(severities):
        rules.append({"id": rule, "name": rule,
                      "defaultConfiguration": {"level": "error" if severities[rule] == "阻断" else "warning"}})
    for f in findings:
        loc = {}
        if f["file"] is not None:
            loc = {"physicalLocation": {"artifactLocation": {"uri": str(f["file"]).replace("\\", "/")}}}
            if f["line"]:
                loc["physicalLocation"]["region"] = {"startLine": f["line"]}
        results.append({"ruleId": f["rule"],
                        "level": "error" if severities.get(f["rule"]) == "阻断" else "warning",
                        "message": {"text": f["message"]},
                        "locations": [loc] if loc else []})
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "qcheck", "version": VERSION, "rules": rules}},
                      "results": results}]}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="qcheck", description="deterministic-only quality gate")
    ap.add_argument("paths", nargs="*", help="files or directories to check")
    ap.add_argument("--staged", action="store_true", help="check git staged files (pre-commit)")
    ap.add_argument("--changed", metavar="BASE_SHA",
                    help="check files changed against a commit (CI: incremental gate, "
                         "so legacy debt does not make CI permanently red)")
    ap.add_argument("--all", action="store_true", help="walk the whole tree")
    ap.add_argument("--root", help="repository root (default: cwd)")
    ap.add_argument("--rules-file", default=str(HERE.parent / "quality-rules.md"))
    ap.add_argument("--json", action="store_true", help="emit SARIF 2.1.0 instead of text")
    ap.add_argument("--strict", action="store_true",
                    help="promote every OBSERVE-listed rule to blocking (use once its 误报率 is measured)")
    args = ap.parse_args()

    if [args.staged, bool(args.changed), args.all].count(True) > 1:
        sys.stderr.write("FATAL: --staged / --changed / --all 三者互斥（各自定义不同的文件集）\n")
        return 2

    snapshot_exported = verify_snapshot()

    severities = read_rules(Path(args.rules_file))

    files, root = iter_targets(args)
    global RUN_ROOT
    RUN_ROOT = root          # QR-12 的执行载体要在仓根上找，不能按单个文件的目录找
    # 跳过的目录是闸门的已知盲区，必须在表头说出来，不能让它变成一个没人知道的默认值。
    blind = sorted(d for d in SKIP_DIRS if (root / d).is_dir()) if args.all else []
    ignored = load_ignored(root)
    findings: list[dict] = []
    skipped = 0
    for path in files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        lines, why = load_lines(path)
        if lines is None:
            skipped += 1
            continue
        for check in CHECKS:
            check(rel, lines, findings)

    used_rules = sorted({f["rule"] for f in findings})
    drift = [r for r in used_rules if r not in severities]
    if drift:
        sys.stderr.write("FATAL: checks produced rule ids absent from the rules file: %s\n"
                         "  策略与实现已脱钩（QR-11）；先修 quality-rules.md 或本文件。\n"
                         % ", ".join(drift))
        return 2

    def enforcement(f) -> str:
        if f["rule"] in OBSERVE and not args.strict:
            return "observe"
        return "block" if severities.get(f["rule"]) == "阻断" else "hint"

    blocking = [f for f in findings if enforcement(f) == "block"]
    observed = [f for f in findings if enforcement(f) == "observe"]
    hints = [f for f in findings if enforcement(f) == "hint"]

    if args.json:
        sarif = to_sarif(findings, severities)
        sarif["runs"][0]["properties"] = {
            "qcheck:version": VERSION,
            "qcheck:filesScanned": len(files),
            "qcheck:filesSkippedUnreadable": skipped,
            "qcheck:skippedDirsPresent": blind,
            "qcheck:qcheckIgnore": ignored,
            "qcheck:skippedDirNames": sorted(SKIP_DIRS),
            "qcheck:blocking": len(blocking),
            "qcheck:observeRules": OBSERVE if not args.strict else {},
            "qcheck:runtimeOnlyRules": [{"id": r, "note": n} for r, n in RUNTIME_ONLY],
        }
        if snapshot_exported is not None:
            sarif["runs"][0]["properties"]["qcheck:vendoredSnapshot"] = {
                "exported": snapshot_exported,
                "note": "判定用的是仓内快照，不是 self-improving 权威源；快照可能落后",
            }
        sys.stdout.write(json.dumps(sarif, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write("qcheck %s :: %d file(s) scanned, %d unreadable skipped\n"
                         % (VERSION, len(files), skipped))
        if snapshot_exported is not None:
            sys.stdout.write("  NOTICE: 按仓内快照判定（导出日 %s，非 self-improving 权威源）。\n"
                             % snapshot_exported)
        if blind:
            sys.stdout.write("  已知盲区（本次未扫，按 SKIP_DIRS 跳过）: %s\n" % ", ".join(blind))
        if ignored:
            sys.stdout.write("  已知盲区（.qcheck-ignore 声明的例外）: %s —— 缺陷样例集，"
                             "由 selftest/F**/judge.py 逐文件扫，例外只对目录遍历生效\n"
                             % ", ".join(ignored))
        if not files:
            # 空判定范围必须是显式事实，不能和"扫了但没问题"共用一个绿。
            scope = ("--changed %s" % args.changed) if args.changed else \
                    ("--staged" if args.staged else "--all")
            sys.stdout.write("  NOTICE: 判定范围是 0 个文件（scope=%s：改动集为空，"
                             "或改动全落在跳过目录里）——本轮没有任何检查发生，绿不等于没问题。\n"
                             % scope)
        for f in sorted(findings, key=lambda x: (x["rule"], str(x["file"]), x["line"] or 0)):
            where = "%s:%s" % (f["file"], f["line"]) if f["line"] else str(f["file"])
            sys.stdout.write("  [%-7s] %s %s\n           %s\n"
                             % (enforcement(f), f["rule"], where, f["message"]))
        for rule, note in RUNTIME_ONLY:
            sys.stdout.write("  [runtime ] %s %s\n" % (rule, note))
        if observed:
            sys.stdout.write("  observe mode (报告不拦)：\n")
            for rule in sorted({f["rule"] for f in observed}):
                sys.stdout.write("    %s: %s\n" % (rule, OBSERVE[rule]))
        sys.stdout.write("summary: %d blocking, %d observe, %d hint, rules authority=%s\n"
                         % (len(blocking), len(observed), len(hints), args.rules_file))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
