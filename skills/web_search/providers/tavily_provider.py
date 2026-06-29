import json
import time
import datetime
import urllib.request
import urllib.parse
from typing import Optional
from . import SearchProviderAdapter, SearchResponse, SearchResult, register_adapter

class TavilyProviderAdapter(SearchProviderAdapter):
    provider_id = "tavily-search"

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        t0 = time.monotonic()
        url = base_url.rstrip("/") + "/search"
        topic = "news" if capability == "search.news" else "general"
        
        payload = {
            "api_key": api_key or "",
            "query": query,
            "topic": topic,
            "max_results": max_results
        }
        
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        
        results = []
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for item in data.get("results", []):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", item.get("snippet", "")),
                        provider_name=self.provider_id,
                        provider_confidence=float(item.get("score", 0.8)),
                        retrieved_at=now_str,
                        published_date=item.get("published_date"),
                        raw_metadata=item
                    ))
        except Exception as exc:
            # Re-raise so provider manager handles failure recording
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

register_adapter(TavilyProviderAdapter())
