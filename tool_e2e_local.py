"""CLI 端到端：本地静态站点 + fake LLM 脚本，跑 main.py 全链路并校验矩阵章节。

证据 id 用 make_evidence_id(url, key, value) 现场算，保证报告里的 [e:id]
与工作流从同一份抽取结果构建的 EvidenceStore 对得上（引用校验才有效）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.agent.citations import make_evidence_id  # noqa: E402

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"
SITES = REPO / "evals" / "cases" / "matrix_b2b" / "sites"

EXTRACT = [
    {"key": "name", "value": "美菜", "confidence": 0.9},
    {"key": "positioning", "value": "生鲜 B2B 供应链服务商，自建仓配网络，服务餐饮与团餐商户", "confidence": 0.9},
    {"key": "business_model", "value": ["自建仓配网络，WMS 仓储系统与 TMS 调度协同", "商品二维码全程溯源，一物一码入库贴码", "多温层仓库分别存放冷冻、冷藏与常温商品"], "confidence": 0.9},
    {"key": "pricing", "value": "商户按品类与配送量报价，城市商品按区域定价", "confidence": 0.9},
    {"key": "features", "value": ["多温层仓配", "一物一码溯源", "智能排线调度"], "confidence": 0.9},
    {"key": "target_users", "value": "餐饮门店与团餐单位的采购与后厨负责人", "confidence": 0.9},
    {"key": "integrations", "value": "开放接口供餐饮 SaaS 对接订单与库存", "confidence": 0.9},
    {"key": "limitations", "value": "边远城市仓配时效不稳", "confidence": 0.9},
    {"key": "reviews", "value": "商户评价配送稳定但账单明细需人工核对", "confidence": 0.9},
    {"key": "as_of", "value": "2026-10-28", "confidence": 0.9},
]

url = f"{BASE}/meicai/index.html"
eid = {item["key"]: make_evidence_id(url, item["key"], str(item["value"])) for item in EXTRACT}

script = {
    "decompose": ['[{"id":"1","description":"分析仓配模式与定价打法","priority":1}]'],
    "extract": [json.dumps(EXTRACT, ensure_ascii=False)],
    "borrowable": [
        json.dumps({
            "rows": [{
                "platform": "美菜",
                "items": [{
                    "anchor_key": "business_model",
                    "anchor_span": "多温层仓库分别存放冷冻、冷藏与常温商品",
                    "take": "温区分层：冷链库位与拣货线按温区独立，出库校验不串货",
                }],
            }],
        }, ensure_ascii=False)
    ],
    "report": [
        (
            "## 一、概述\n\n美菜是生鲜 B2B 供应链服务商 [e:{positioning}]。\n\n"
            "## 二、模式要点\n\n自建仓配网络并用 WMS 与 TMS 协同调度 [e:{business_model}]。\n\n"
            "## 三、定价\n\n商户按品类与配送量报价 [e:{pricing}]。\n\n"
            "## 四、目标用户\n\n面向餐饮门店与团餐单位 [e:{target_users}]。\n"
        ).format(**eid)
    ],
}

script_path = REPO / "_e2e_fake_script.json"
script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

server = subprocess.Popen(
    [sys.executable, "-m", "http.server", str(PORT), "--bind", "127.0.0.1", "--directory", str(SITES)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(1.5)
try:
    env = dict(os.environ)
    env["LLM_PROVIDER"] = "fake"
    env["LLM_FAKE_SCRIPT"] = str(script_path)
    # 本地静态站点是私网地址：走仓库已有的显式开关（生产默认 false，失败即拒）
    env["ALLOW_PRIVATE_NETWORK_URLS"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "main.py", "--query", "美菜的仓配模式与定价打法",
         "--urls", f"{BASE}/meicai/index.html", "--no-search",
         "--self-context", "我们是做团餐的中央厨房，想建自己的冷链仓配网络"],
        cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    print("=== main.py exit code:", proc.returncode)
    print("--- stdout (tail) ---")
    print(proc.stdout[-2500:])
    if proc.stderr.strip():
        print("--- stderr (tail) ---")
        print(proc.stderr[-2000:])
finally:
    server.terminate()
    server.wait(timeout=10)
    # 脚本是本轮的临时产物：用完删掉，别留在仓库里冒充正式配置
    script_path.unlink(missing_ok=True)
