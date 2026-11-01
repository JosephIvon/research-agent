"""金标准域活性行核：批量探活 + 证据落盘，供人确认金标准域变更。

背景（第十三轮实测 ✗）：Talkie 的金标准域 talkie-ai.com 在搜索后端不可见，
缺口 12/12 无法收敛——先弄清**金标准本身是否还准**：域死了？迁移了？
还是活着只是引擎不收录？

纪律：
- 本脚本**只读**金标准 ✗ 不改它——域变更必须带着证据走人确认后另行提交 ✓；
- 每个域记录：HTTP 状态 / 重定向链 / 最终域 / 页面标题——
  "最终域 ≠ 金标准域"就是**域迁移候选**（决定性证据形态 ✓）；
- 只用标准库（urllib + 线程池），不引新依赖 ✓。

用法：
    uv run python evals/check_golden_hosts.py \
        --golden evals/discovery_golden/companion_apps.json \
        --golden evals/discovery_golden/track_universe.json \
        --out evals/discovery_golden/host_liveness_20261102.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def load_hosts(path: Path) -> list:
    """读金标准文件，返回 [{file, name, hosts}]。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("golden")
    if not (isinstance(entries, list) and entries):
        entries = []
        for block in (data.get("subjects") or {}).values():
            entries.extend((block or {}).get("golden") or [])
    out = []
    for e in entries:
        out.append({
            "file": path.name,
            "name": str(e.get("name") or ""),
            "hosts": [h for h in (e.get("hosts") or []) if h],
        })
    return out


class _RedirectRecorder(urlrequest.HTTPRedirectHandler):
    """记录重定向链——"官方域 301 → 新域"是域迁移的决定性证据 ✓。"""

    def __init__(self):
        self.chain: list = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append({"code": code, "to": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_host(host: str, timeout: int) -> dict:
    """探一个域：跟随重定向，记录状态/最终域/标题。"""
    scheme = "https"
    recorder = _RedirectRecorder()
    opener = urlrequest.build_opener(recorder)
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.6")]
    last_err = ""
    for attempt, sch in enumerate((scheme, "http")):  # https 失败再试 http ✓
        url = f"{sch}://{host}/"
        try:
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read(200_000).decode("utf-8", "ignore")
                m = _TITLE_RE.search(body)
                title = _WS_RE.sub(" ", _TAG_RE.sub("", m.group(1))).strip()[:80] if m else ""
                final_host = (urlparse(resp.geturl()).hostname or "").lower()
                return {
                    "host": host, "status": resp.status, "ok": True,
                    "final_url": resp.geturl(), "final_host": final_host,
                    "redirects": recorder.chain[:4], "title": title,
                    "error": "",
                }
        except urlerror.HTTPError as exc:
            # 4xx/5xx 是**服务器有响应**：域活着，但页面没了/拒绝 ✓
            return {"host": host, "status": exc.code, "ok": False,
                    "final_url": f"{sch}://{host}/", "final_host": host,
                    "redirects": recorder.chain[:4], "title": "",
                    "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001 —— DNS/超时/TLS 都算不可达
            last_err = f"{type(exc).__name__}: {exc}"[:120]
    return {"host": host, "status": 0, "ok": False,
            "final_url": "", "final_host": "", "redirects": [],
            "title": "", "error": last_err}


def classify(result: dict) -> str:
    if result["ok"] and result["final_host"] and result["final_host"] not in (
            result["host"], "www." + result["host"]):
        return "迁移候选"
    if result["ok"]:
        return "存活"
    if result["status"] in (401, 403):
        return "拒绝爬虫（域活）"
    if result["status"] == 404:
        return "404（服务器在，页面没了）"
    if result["status"] and result["status"] >= 500:
        return f"{result['status']}（服务器在）"
    return "不可达"


def main() -> int:
    parser = argparse.ArgumentParser(description="金标准域活性行核（只读 ✗ 不改金标准）")
    parser.add_argument("--golden", action="append", required=True,
                        help="金标准 JSON（可多次）")
    parser.add_argument("--out", required=True, help="证据报告 markdown 输出路径")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--only-hosts", default="",
                        help="只探这个文件里的域（每行一个）——复测用（如清掉代理看国内直连视角 ✓）")
    args = parser.parse_args()

    entries: list = []
    for g in args.golden:
        entries.extend(load_hosts(Path(g)))
    # 去重探活（同一域只探一次 ✓），但报告里保留每个条目的引用
    unique_hosts = sorted({h for e in entries for h in e["hosts"]})
    if args.only_hosts:
        wanted = {
            line.strip().lower()
            for line in Path(args.only_hosts).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        entries = [e for e in entries if any(h.lower() in wanted for h in e["hosts"])]
        unique_hosts = [h for h in unique_hosts if h.lower() in wanted]
    print(f"金标准条目 {len(entries)} 条，待探域 {len(unique_hosts)} 个（{args.workers} 并发）")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = dict(zip(unique_hosts, pool.map(
            lambda h: probe_host(h, args.timeout), unique_hosts)))

    lines: list = []
    lines.append("# 金标准域活性行核证据（只读探活 ✗ 本报告不改金标准）")
    lines.append("")
    lines.append(f"- 探活时间：{args.out and __import__('datetime').date.today().isoformat()}")
    lines.append(f"- 范围：{'、'.join(Path(g).name for g in args.golden)}"
                 f"（{len(entries)} 条 / {len(unique_hosts)} 个唯一域）")
    lines.append(f"- 判读规则：`迁移候选` = 域仍响应但重定向到别的域（决定性证据 ✓）；"
                 f"`不可达` = DNS/超时/TLS 全失败；`404/5xx` = 服务器在但页面没了/拒绝。")
    lines.append("")
    lines.append("| 判读 | 金标准域 | HTTP | 最终域 | 页面标题 | 重定向链 | 引用条目 |")
    lines.append("|---|---|---|---|---|---|---|")

    by_host: dict = {}
    for e in entries:
        for h in e["hosts"]:
            by_host.setdefault(h, set()).add(f"{e['name']}")
    order = {"迁移候选": 0, "不可达": 1, "404（服务器在，页面没了）": 2,
             "拒绝爬虫（域活）": 3}
    rows = sorted(results.items(),
                  key=lambda kv: (order.get(classify(kv[1]), 4), kv[0]))
    counts: dict = {}
    for host, r in rows:
        verdict = classify(r)
        counts[verdict] = counts.get(verdict, 0) + 1
        refs = "、".join(sorted(by_host.get(host, set()))[:3])
        chain = " → ".join(f"{s['to']}"[:48] for s in r["redirects"]) or "—"
        title = r["title"].replace("|", "\\|")[:60]
        lines.append(f"| {verdict} | {host} | {r['status'] or '—'} | "
                     f"{r['final_host'] or '—'} | {title or '—'} | {chain} | {refs} |")
    lines.insert(4, "")
    lines.insert(4, "、".join(f"{k} {v} 个" for k, v in sorted(counts.items(),
                                                        key=lambda kv: -kv[1])))

    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"证据报告：{out}")
    print("判读汇总：", "、".join(f"{k} {v}" for k, v in sorted(counts.items(),
                                                      key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
