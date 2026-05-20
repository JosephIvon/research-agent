from src.crawler.web_crawler import WebCrawler
from src.tools.quality_scorer import QualityScorer
from src.workflow.mvp_workflow import MVPWorkflow
import asyncio


def test_quality_scorer_matches_dimension_keywords_in_values():
    competitors = [
        {
            "name": "api000.com",
            "url": "https://api000.com",
            "status": "success",
            "extracted_data": [
                {
                    "key": "platform_summary",
                    "value": (
                        "Supports 40+ AI model providers including OpenAI, Claude, Gemini, and DeepSeek. "
                        "Provides Chat API, Image API, Audio API, and OpenAI SDK compatible integration. "
                        "Built for enterprise customers and developers with pay-as-you-go pricing, no subscription."
                    ),
                    "source_url": "https://api000.com",
                }
            ],
        }
    ]

    report = QualityScorer(competitors).evaluate()
    dimensions = {dim.key: dim for dim in report.dimensions}

    assert dimensions["models"].has_data
    assert dimensions["pricing"].has_data
    assert dimensions["integrations"].has_data
    assert dimensions["target_users"].has_data


def test_crawler_discovers_high_value_research_links_from_navigation():
    html = """
    <html>
      <body>
        <nav>
          <a href="/pricing">Pricing</a>
          <a href="/docs/quickstart">Developer docs</a>
          <a href="/models">Models</a>
          <a href="/blog">Blog</a>
        </nav>
        <main>
          <a href="/console">Console</a>
          <a href="https://external.example/docs">External docs</a>
        </main>
      </body>
    </html>
    """

    links = WebCrawler.discover_research_links("https://api000.com", html, max_links=4)

    assert links[:3] == [
        "https://api000.com/pricing",
        "https://api000.com/docs/quickstart",
        "https://api000.com/models",
    ]
    assert "https://external.example/docs" not in links


def test_workflow_fetches_site_profile_instead_of_single_page(monkeypatch):
    captured = {}

    class FakeCrawler:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def fetch_site_profile(self, url, **kwargs):
            captured["profile_url"] = url
            return {
                "content": "### Source URL: https://example.com\nModels, pricing, API workflow",
                "pages": [{"url": "https://example.com", "content": "homepage"}],
                "discovered_urls": ["https://example.com/pricing"],
            }

    class FakeCoordinator:
        async def extract_key_info(self, raw_content, url):
            captured["raw_content"] = raw_content
            return [{"key": "models", "value": "Models and pricing", "source_url": url}]

    monkeypatch.setattr("src.workflow.mvp_workflow.WebCrawler", lambda: FakeCrawler())
    workflow = object.__new__(MVPWorkflow)
    workflow.coordinator = FakeCoordinator()
    workflow._event_emitter = None
    workflow.state = {"task_id": "test"}

    result = asyncio.run(workflow._fetch_single_competitor("https://example.com"))

    assert captured["profile_url"] == "https://example.com"
    assert "API workflow" in captured["raw_content"]
    assert result["raw_pages"][0]["url"] == "https://example.com"
    assert result["discovered_urls"] == ["https://example.com/pricing"]
