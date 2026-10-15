"""把 pytest junitxml 的失败用例输出为 GitHub Actions annotation。

日志下载需要登录，而 check-runs/annotations API 匿名可读——把失败清单
写进 annotation，CI 红的时候不开网页也能定位到具体测试。
"""
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1] if len(sys.argv) > 1 else "pytest-report.xml"
root = ET.parse(path).getroot()

count = 0
for case in root.iter("testcase"):
    for kind in ("failure", "error"):
        for item in case.iter(kind):
            name = f"{case.get('classname', '')}::{case.get('name', '?')}"
            message = (item.get("message") or item.text or "").replace("\r", " ")
            # 300 字符会把带事件时间线的失败信息拦腰截断（实测：时间线在
            # citation_check 处被砍，最后错误整个丢失）。E2E 失败信息现在
            # 可能长达 1-2KB，放宽到 4000，仍远低于 GitHub annotation 上限。
            message = " ".join(message.split())[:4000].replace("%", "%25")
            print(f"::error title=pytest:{name}::{message}")
            count += 1

if count == 0:
    print("::error title=pytest::junitxml 中没有失败用例（异常退出？请看完整日志）")
