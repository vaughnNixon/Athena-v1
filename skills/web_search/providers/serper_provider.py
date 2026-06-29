import json
import time
import datetime
import urllib.request
from typing import Optional
from . import SearchProviderAdapter, SearchResponse, SearchResult, register_adapter

class SerperProviderAdapter(SearchProviderAdapter):
    provider_id = "serper-search"

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        t0 = time.monotonic()
        endpoint = "/news" if capability == "search.news" else "/search"
        url = base_url.rstrip("/") + endpoint
        
        payload = {"q": query, "num": max_results}
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": api_key or ""
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        
        results = []
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                raw_items = data.get("news", []) if capability == "search.news" else data.get("organic", [])
                for item in raw_items:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("link", ""),
                        snippet=item.get("snippet", ""),
                        provider_name=self.provider_id,
                        provider_confidence=0.8,
                        retrieved_at=now_str,
                        published_date=item.get("date"),
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

register_adapter(SerperProviderAdapter())
