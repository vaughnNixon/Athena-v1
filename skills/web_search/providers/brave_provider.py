import json
import time
import datetime
import urllib.request
import urllib.parse
from typing import Optional
from . import SearchProviderAdapter, SearchResponse, SearchResult, register_adapter

class BraveProviderAdapter(SearchProviderAdapter):
    provider_id = "brave-search"

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        t0 = time.monotonic()
        endpoint = "/res/v1/news/search" if capability == "search.news" else "/res/v1/web/search"
        url = base_url.rstrip("/") + endpoint + f"?q={urllib.parse.quote(query)}&count={max_results}"
        
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key or ""
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        
        results = []
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                raw_results = data.get("web", {}).get("results", []) if capability != "search.news" else data.get("results", [])
                for item in raw_results:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("description", ""),
                        provider_name=self.provider_id,
                        provider_confidence=0.85,
                        retrieved_at=now_str,
                        published_date=item.get("page_age"),
                        raw_metadata=item
                    ))
        except Exception as exc:
            raise exc

        latency = (time.monotonic() - t0) * 1000
        return SearchResponse(
            query=query,
            capability=capability,
            results=results,
            provider_used=self.provider_id,
            key_used=api_key[:4] + "..." if api_key else None,
            latency_ms=latency
        )

register_adapter(BraveProviderAdapter())
