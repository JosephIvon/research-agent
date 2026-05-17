"""智能调研助手主入口"""
import argparse
import sys
import json
import getpass
import os
from src.workflow.mvp_workflow import run_sync
from src.config.settings import MAX_FETCH_URLS
from src.security.url_validation import validate_fetch_url, validate_fetch_urls


def parse_selectors(selectors_str: str) -> dict:
    """解析选择器JSON字符串"""
    try:
        selectors = json.loads(selectors_str)
        if not isinstance(selectors, dict):
            raise ValueError("选择器必须是JSON对象")
        unknown_keys = set(selectors) - {"username", "password", "submit"}
        if unknown_keys:
            raise ValueError(f"不支持的选择器字段: {sorted(unknown_keys)}")
        for key in ["username", "password", "submit"]:
            if key in selectors and isinstance(selectors[key], str):
                selectors[key] = [selectors[key]]
            if key in selectors:
                values = selectors[key]
                if not isinstance(values, list) or not values or len(values) > 10:
                    raise ValueError(f"{key} 选择器数量必须在 1-10 之间")
        return selectors
    except json.JSONDecodeError as e:
        raise ValueError(f"选择器JSON格式错误: {e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="智能调研助手 - AI驱动的竞品分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 自动搜索并生成报告
  python main.py --query "分析AI助手产品"

  # 指定URL列表进行横向对比
  python main.py --query "在线文档产品对比" --urls https://shimo.im https://docs.qq.com https://www.wps.cn

  # 使用自定义登录选择器
  python main.py --query "分析内部系统" --login-url https://example.com/login \\
      --username admin --password-env RESEARCH_LOGIN_PASSWORD \\
      --selectors '{"username": "input[name=admin_user]", "password": "input[name=admin_pwd]", "submit": "button[type=submit]"}'

  # 禁用自动搜索
  python main.py --query "产品分析" --urls https://example.com --no-search

  # 同步到飞书
  python main.py --query "竞品分析" --urls https://example.com --sync feishu
        """
    )

    parser.add_argument("--query", "-q", type=str, required=True, help="调研主题")
    parser.add_argument("--urls", "-u", type=str, nargs="+", help="目标竞品网站URL列表")
    parser.add_argument("--login-url", type=str, help="登录页面URL（如需登录）")
    parser.add_argument("--username", type=str, help="登录用户名")
    parser.add_argument("--password", type=str, help="登录密码（不推荐，可能出现在进程列表中）")
    parser.add_argument("--password-env", type=str, help="从指定环境变量读取登录密码")
    parser.add_argument("--selectors", type=str,
                        help="自定义登录表单选择器（JSON格式），如：'{\"username\": \"input[name=user]\", \"password\": \"input[name=pwd]\"}'")
    parser.add_argument("--sync", type=str, nargs="+", choices=["feishu", "tencent"],
                        help="同步目标平台")
    parser.add_argument("--no-search", action="store_true", help="禁用自动搜索")

    args = parser.parse_args()

    try:
        if args.urls:
            if len(args.urls) > MAX_FETCH_URLS:
                raise ValueError(f"目标URL数量不能超过 {MAX_FETCH_URLS} 个")
            args.urls = validate_fetch_urls(args.urls)
        if args.login_url:
            args.login_url = validate_fetch_url(args.login_url)
    except ValueError as e:
        print(f"❌ URL校验错误: {e}")
        sys.exit(1)

    auth_credentials = None
    password = args.password
    if args.password_env:
        password = os.getenv(args.password_env)
        if password is None:
            print(f"❌ 环境变量 {args.password_env} 未设置")
            sys.exit(1)

    if args.username and not password:
        password = getpass.getpass("登录密码: ")

    if args.password:
        print("⚠️ 建议改用 --password-env，避免登录密码出现在进程列表或Shell历史中")

    if args.username and password:
        auth_credentials = {
            "username": args.username,
            "password": password
        }

    custom_selectors = None
    if args.selectors:
        try:
            custom_selectors = parse_selectors(args.selectors)
            print(f"✓ 使用自定义选择器: {list(custom_selectors.keys())}")
        except ValueError as e:
            print(f"❌ 选择器解析错误: {e}")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"🚀 智能调研助手 - 竞品横向对比报告生成")
    print(f"{'='*60}")
    print(f"\n📋 调研主题: {args.query}")
    print(f"🎯 目标URL: {args.urls or '（将自动搜索）'}")

    if args.urls and len(args.urls) >= 2:
        print(f"📊 竞品数量: {len(args.urls)}（将生成横向对比报告）")
    elif args.urls:
        print(f"📊 竞品数量: {len(args.urls)}")

    try:
        result = run_sync(
            user_query=args.query,
            target_urls=args.urls or [],
            auth_credentials=auth_credentials,
            login_url=args.login_url,
            sync_targets=args.sync,
            enable_search=not args.no_search,
            custom_selectors=custom_selectors
        )

        print(f"\n{'='*60}")
        print(f"📌 执行结果")
        print(f"{'='*60}")
        print(f"状态: {result['status']}")
        print(f"终止原因: {result['termination_reason']}")

        if result.get("competitors"):
            success_count = sum(1 for c in result["competitors"] if c["status"] == "success")
            print(f"竞品抓取: {success_count}/{len(result['competitors'])} 成功")
            for comp in result["competitors"]:
                icon = "✅" if comp["status"] == "success" else "❌"
                url_display = comp['url'][:45] + "..." if len(comp['url']) > 45 else comp['url']
                print(f"  {icon} {comp['name']} ({url_display})")

        if result.get("verification_score") is not None:
            print(f"可信度评分: {result['verification_score']}/10")

        if result.get("quality_score") is not None:
            grade_display = result.get('quality_grade', 'N/A')
            print(f"📋 数据质量: {result['quality_score']:.1f}/10（{grade_display}级）")
            if result.get("missing_dimensions"):
                missing = ', '.join(result['missing_dimensions'][:3])
                print(f"   缺失维度: {missing}{'...' if len(result['missing_dimensions']) > 3 else ''}")

        if result.get("report_final"):
            print(f"\n📄 报告预览:")
            print("-" * 60)
            preview = result["report_final"][:1500] + "\n... (省略)" if len(result["report_final"]) > 1500 else result["report_final"]
            print(preview)
            print("-" * 60)

            if result.get("sync_results", {}).get("local_file"):
                print(f"\n💾 报告已保存: {result['sync_results']['local_file']}")

        if result.get("sync_results"):
            for platform, doc_id in result["sync_results"].items():
                if platform != "local_file":
                    print(f"🔄 {platform}同步: {doc_id}")

        if result.get("errors"):
            print(f"\n⚠️ 错误记录:")
            for error in result["errors"][:3]:
                print(f"  - [{error['stage']}] {error['error_type']}: {error['message']}")

        print(f"\n{'='*60}")

    except Exception as e:
        print(f"\n❌ 执行失败: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
