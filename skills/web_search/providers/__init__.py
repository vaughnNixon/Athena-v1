import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider_name: str              # e.g. "tavily-search"
    provider_confidence: float     # 0.0 to 1.0 relevance score
    retrieved_at: str               # ISO8601 UTC timestamp
    published_date: Optional[str] = None
    language: Optional[str] = "en"
    region: Optional[str] = "GLOBAL"
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SearchResponse:
    query: str
    capability: str                 # e.g. "search.news"
    results: List[SearchResult]
    provider_used: str              # provider ID
    key_used: Optional[str] = None
    latency_ms: float = 0.0
    from_cache: bool = False

class SearchProviderAdapter:
    provider_id: str

    def search(
        self,
        query: str,
        capability: str,
        api_key: Optional[str],
        base_url: str,
        max_results: int = 5
    ) -> SearchResponse:
        raise NotImplementedError

_adapter_registry: Dict[str, SearchProviderAdapter] = {}

def register_adapter(adapter: SearchProviderAdapter):
    _adapter_registry[adapter.provider_id] = adapter

def get_adapter(provider_id: str) -> Optional[SearchProviderAdapter]:
    return _adapter_registry.get(provider_id)
