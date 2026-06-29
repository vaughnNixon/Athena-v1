import json
import time
import datetime
import urllib.request
import urllib.parse
from typing import Optional
from . import SearchProviderAdapter, SearchResponse, SearchResult, register_adapter

class SearXNGProviderAdapter(SearchProviderAdapter):
    provider_id = "searxng-search"

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        t0 = time.monotonic()
        url = base_url.rstrip("/") + f"/search?q={urllib.parse.quote(query)}&format=json"
        
        req = urllib.request.Request(url, method="GET")
        results = []
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for item in data.get("results", [])[:max_results]:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", ""),
                        provider_name=self.provider_id,
                        provider_confidence=0.75,
                        retrieved_at=now_str,
                        published_date=item.get("publishedDate"),
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
            key_used=None,
            latency_ms=latency
        )

register_adapter(SearXNGProviderAdapter())
