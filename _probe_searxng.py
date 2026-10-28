"""临时探针：SearXNG 在连续查询下的行为（限流？超时？空结果？）。"""
import time

import requests

QUERIES = [
    "猫箱 竞品", "百度搭子 竞品", "AI陪伴 应用 竞品", "猫箱 官网",
    "百度搭子 官网", "Talkie 竞品", "猫箱 功能", "百度搭子 功能",
]

for query in QUERIES:
    started = time.time()
    try:
        response = requests.get(
            "http://localhost:8888/search",
            params={"q": query, "format": "json", "categories": "general",
                    "language": "zh-CN", "limit": 8},
            timeout=30,
        )
        data = response.json()
        results = data.get("results", [])
        print(f"{query!r:26} status={response.status_code} results={len(results)} {time.time()-started:.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"{query!r:26} FAIL {type(exc).__name__}: {exc}")
