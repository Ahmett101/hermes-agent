"""Regression tests for Firecrawl extract status handling."""

import pytest

from plugins.web.firecrawl import provider as firecrawl_provider


class _RefusedScrapeClient:
    def scrape(self, *, url, formats):
        return {
            "markdown": "",
            "html": "",
            "metadata": {
                "title": "Access denied",
                "sourceURL": url,
                "statusCode": 403,
                "error": "Forbidden",
            },
        }


@pytest.mark.asyncio
async def test_firecrawl_extract_reports_target_status_refusal(monkeypatch):
    """Firecrawl metadata.statusCode failures must surface as errors."""
    monkeypatch.setattr(firecrawl_provider, "_use_keyless_ring", lambda: False)
    monkeypatch.setattr(
        firecrawl_provider,
        "_get_firecrawl_client",
        lambda: _RefusedScrapeClient(),
    )
    monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl_provider, "is_safe_url", lambda url: True)

    result = await firecrawl_provider.FirecrawlWebSearchProvider().extract(
        ["https://example.com/paywalled"],
        format="markdown",
    )

    assert result == [
        {
            "url": "https://example.com/paywalled",
            "title": "Access denied",
            "content": "",
            "raw_content": "",
            "error": "Firecrawl target returned HTTP 403: Forbidden",
            "metadata": {
                "title": "Access denied",
                "sourceURL": "https://example.com/paywalled",
                "statusCode": 403,
                "error": "Forbidden",
            },
        }
    ]
