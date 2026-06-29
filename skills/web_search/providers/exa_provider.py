import json
import time
import datetime
import urllib.request
from typing import Optional
from . import SearchProviderAdapter, SearchResponse, SearchResult, register_adapter

class ExaProviderAdapter(SearchProviderAdapter):
    provider_id = "exa-search"

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        t0 = time.monotonic()
        url = (base_url or "https://api.exa.ai").rstrip("/") + "/search"
        
        payload = {
            "query": query,
            "numResults": max_results,
            "contents": {"text": True}
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or ""
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        
        results = []
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for item in data.get("results", []):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("text", "")[:300],
                        provider_name=self.provider_id,
                        provider_confidence=float(item.get("score", 0.85)),
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
            key_used=api_key[:4] + "..." if api_key else None,
            latency_ms=latency
        )

register_adapter(ExaProviderAdapter())
