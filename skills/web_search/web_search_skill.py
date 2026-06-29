import os
import json
import time

from dataclasses import asdict
from typing import Optional, Any
from skills.base_skill import BaseSkill
from skills.manifest import SkillManifest
from skills.policies import PERM_NETWORK_HTTP, PERM_STORAGE_ARTIFACTS
from subagent_result import SubagentResult
from service_providers_manager import get_service_manager
from search_cache import SearchCache
from .providers import get_adapter, SearchResponse

_cache = SearchCache(ttl_seconds=3600)

class WebSearchSkill(BaseSkill):
    def __init__(self):
        manifest = SkillManifest(
            name="web_search",
            version="1.3.0",
            athena_api=1,
            description="Executes namespaced search queries across multi-provider networks.",
            author="Athena Core",
            capabilities=[
                "search.web", "search.news", "search.image", 
                "search.video", "search.academic", "search.code",
                "search.documentation", "search.maps", "search.shopping"
            ],
            permissions=[PERM_NETWORK_HTTP, PERM_STORAGE_ARTIFACTS]
        )
        super().__init__(manifest=manifest)

    def run(self, *args, **kwargs) -> SubagentResult:
        # Flexible argument handling for legacy and SkillContext calls
        ctx = None
        task = ""
        capability = None
        
        if len(args) >= 2 and hasattr(args[0], "task_id"):
            ctx = args[0]
            task = args[1]
        elif len(args) >= 1 and isinstance(args[0], str):
            task = args[0]
        else:
            task = kwargs.get("task", "")
            
        cap = kwargs.get("capability") or (ctx.capability if ctx else "search.web")
        query = task.strip()

        # 1. Check Search Cache
        cached_data = _cache.get(cap, query)
        if cached_data:
            from .providers import SearchResult
            results = [SearchResult(**r) for r in cached_data.get("results", [])]
            response = SearchResponse(
                query=cached_data["query"],
                capability=cached_data["capability"],
                results=results,
                provider_used=cached_data["provider_used"],
                key_used=cached_data.get("key_used"),
                latency_ms=cached_data.get("latency_ms", 0.0),
                from_cache=True
            )
            return self._build_subagent_result(task, cap, response, ctx)

        # 2. Multi-Provider Failover Execution
        q_clean = query.lower().replace("can u search the web for me about", "").replace("search the web for me about", "").replace("search web for", "").replace("search for", "").strip()
        search_query = q_clean if q_clean else query
        response = self._execute_with_failover(search_query, cap, ctx)

        if not response:
            return SubagentResult(
                user_output="Search execution failed across all configured search providers.",
                aal_summary={
                    "task": task,
                    "skill_used": self.manifest.name,
                    "capability": cap,
                    "outcome": "failed",
                    "confidence": 0.0,
                    "notes": "All search providers exhausted or disabled."
                },
                memory_payload=[],
                artifacts=[]
            )

        # 3. Populate Cache
        resp_dict = {
            "query": response.query,
            "capability": response.capability,
            "results": [asdict(r) for r in response.results],
            "provider_used": response.provider_used,
            "key_used": response.key_used,
            "latency_ms": response.latency_ms
        }
        _cache.set(cap, query, resp_dict)

        # 4. Construct SubagentResult
        return self._build_subagent_result(task, cap, response, ctx)

    def _execute_with_failover(self, query: str, capability: str, ctx: Any = None) -> Optional[SearchResponse]:
        manager = ctx.services if ctx and getattr(ctx, "services", None) else get_service_manager()
        skip_providers = []
        skip_keys = {}
        self_heal_done = False

        while True:
            provider = manager.get_healthiest_provider(
                category="search",
                capability=capability,
                skip_providers=skip_providers
            )

            if provider is None:
                if not self_heal_done:
                    manager.reset_all_failures()
                    self_heal_done = True
                    skip_providers = []
                    skip_keys = {}
                    continue
                return None

            key = manager.get_active_key(provider.id, skip_keys=skip_keys.get(provider.id, []))
            if key is None and provider.api_keys:
                skip_providers.append(provider.id)
                continue

            adapter = get_adapter(provider.id)
            if adapter is None:
                skip_providers.append(provider.id)
                continue

            try:
                t0 = time.monotonic()
                res = adapter.search(
                    query=query,
                    capability=capability,
                    api_key=key,
                    base_url=provider.base_url
                )
                latency = (time.monotonic() - t0) * 1000
                manager.record_success(provider.id, key, latency)
                return res
            except Exception as exc:
                manager.record_failure(provider.id, key, exc)
                if key:
                    skip_keys.setdefault(provider.id, []).append(key)
                else:
                    skip_providers.append(provider.id)

    def _build_subagent_result(self, task: str, capability: str, response: SearchResponse, ctx: Any = None) -> SubagentResult:
        import config
        base_art_dir = ctx.artifacts_dir if ctx and getattr(ctx, "artifacts_dir", None) else config.get_athena_home() / "artifacts"
        artifacts_dir = base_art_dir / f"search_{int(time.time()*1000)}"
        os.makedirs(artifacts_dir, exist_ok=True)

        results_path = str(artifacts_dir / "search_results.json")
        citations_path = str(artifacts_dir / "citations.json")
        raw_resp_path = str(artifacts_dir / "raw_provider_response.json")

        results_data = [asdict(r) for r in response.results]
        citations_data = [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in response.results]
        raw_resp_data = {
            "query": response.query,
            "capability": response.capability,
            "provider": response.provider_used,
            "from_cache": response.from_cache,
            "results": results_data
        }

        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results_data, f, indent=2, ensure_ascii=False)
        with open(citations_path, "w", encoding="utf-8") as f:
            json.dump(citations_data, f, indent=2, ensure_ascii=False)
        with open(raw_resp_path, "w", encoding="utf-8") as f:
            json.dump(raw_resp_data, f, indent=2, ensure_ascii=False)

        formatted_output = "\n".join([f"- [{r.title}]({r.url}): {r.snippet}" for r in response.results])
        if not formatted_output:
            formatted_output = "No search results found."

        cache_str = " (cached)" if response.from_cache else ""
        user_out = f"### Search Results ({capability}){cache_str}\n{formatted_output}"

        return SubagentResult(
            user_output=user_out,
            aal_summary={
                "task": task,
                "skill_used": self.manifest.name,
                "capability": capability,
                "provider_used": response.provider_used,
                "result_count": len(response.results),
                "from_cache": response.from_cache,
                "outcome": "success",
                "confidence": 0.9 if response.results else 0.3,
                "artifacts": [results_path, citations_path, raw_resp_path]
            },
            memory_payload=[],
            artifacts=[
                {"name": "search_results.json", "path": results_path},
                {"name": "citations.json", "path": citations_path},
                {"name": "raw_provider_response.json", "path": raw_resp_path}
            ]
        )
