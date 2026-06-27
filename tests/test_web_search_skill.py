import os
import json
import pytest
import skills
from service_providers_manager import get_service_manager, ServiceProvider
from search_cache import SearchCache
import skills.web_search # triggers auto-registration

def test_service_providers_manager_routing():
    sm = get_service_manager()
    sm.reset_all_failures()
    
    # Ensure tavily-search provider is healthy and enabled for test
    p = sm.get_healthiest_provider(category="search", capability="search.web")
    assert p is not None
    assert p.category == "search"
    assert "search.web" in p.supported_capabilities

def test_search_cache():
    cache = SearchCache(ttl_seconds=60)
    cache.clear()
    
    cap = "search.web"
    query = "python programming news"
    data = {"query": query, "capability": cap, "provider_used": "mock", "results": []}
    
    assert cache.get(cap, query) is None
    cache.set(cap, query, data)
    
    cached = cache.get(cap, query)
    assert cached is not None
    assert cached["from_cache"] is True
    assert cached["query"] == query

def test_web_search_skill_execution(monkeypatch):
    sm = get_service_manager()
    sm.reset_all_failures()
    
    # Mock Tavily adapter response so test is hermetic without requiring live external network/API key
    from skills.web_search.providers import get_adapter, SearchResponse, SearchResult
    adapter = get_adapter("tavily-search")
    
    def mock_search(query, capability, api_key, base_url, max_results=5):
        return SearchResponse(
            query=query,
            capability=capability,
            results=[
                SearchResult(
                    title="Mock Result Title",
                    url="https://example.com/mock",
                    snippet="This is a mock snippet for testing.",
                    provider_name="tavily-search",
                    provider_confidence=0.9,
                    retrieved_at="2026-06-27T00:00:00Z"
                )
            ],
            provider_used="tavily-search",
            latency_ms=120.0
        )
        
    monkeypatch.setattr(adapter, "search", mock_search)
    
    skill = skills.get("web_search")
    assert skill is not None
    
    res = skill.run(task="latest python news", memory_context="", capability="search.news")
    assert res is not None
    assert "Mock Result Title" in res.user_output
    assert res.aal_summary["outcome"] == "success"
    assert res.memory_payload == [] # Requirement 5: Skill produces NO raw memory payload
    assert len(res.artifacts) == 3   # Requirement 8: First-class artifacts created
    
    # Verify artifact files were created on disk
    for art in res.artifacts:
        assert os.path.exists(art["path"])
