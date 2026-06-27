import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("athena.service_providers_manager")

@dataclass
class ServiceProvider:
    id: str
    name: str
    category: str                   # e.g., "search", "llm", "image", "embedding"
    enabled: bool = True
    base_url: str = ""
    priority: int = 1
    supported_capabilities: List[str] = field(default_factory=list)
    api_keys: List[str] = field(default_factory=list)
    key_stats: Dict[str, dict] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=lambda: {
        "successful_requests": 0,
        "failed_requests": 0,
        "consecutive_failures": 0,
        "total_latency_ms": 0.0,
        "avg_latency_ms": 0.0,
        "last_success": None,
        "last_failure": None
    })

    def __post_init__(self):
        for key in self.api_keys:
            if key not in self.key_stats:
                self.key_stats[key] = {
                    "failures": 0,
                    "successes": 0,
                    "last_success": None,
                    "last_failure": None
                }

    def to_dict(self) -> dict:
        return asdict(self)


class ServiceProvidersManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ServiceProvidersManager, cls).__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        import config
        self.filepath = config.get_athena_home() / "service_providers.json"
        self.providers: Dict[str, ServiceProvider] = {}
        self.active_overrides: Dict[str, Optional[str]] = {}
        self._initialized = True
        self.load_providers()

    def load_providers(self):
        if not self.filepath.exists():
            self._seed_default_providers()
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.active_overrides = data.get("active_overrides", {})
            self.providers.clear()
            for p_dict in data.get("providers", []):
                p = ServiceProvider(
                    id=p_dict["id"],
                    name=p_dict["name"],
                    category=p_dict.get("category", "search"),
                    enabled=p_dict.get("enabled", True),
                    base_url=p_dict.get("base_url", ""),
                    priority=p_dict.get("priority", 1),
                    supported_capabilities=p_dict.get("supported_capabilities", []),
                    api_keys=p_dict.get("api_keys", []),
                    key_stats=p_dict.get("key_stats", {}),
                    stats=p_dict.get("stats", {})
                )
                self.providers[p.id] = p
        except Exception as exc:
            logger.error("Failed to load service_providers.json: %s", exc)

    def save_providers(self):
        try:
            data = {
                "active_overrides": self.active_overrides,
                "providers": [p.to_dict() for p in self.providers.values()]
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error("Failed to save service_providers.json: %s", exc)

    def _seed_default_providers(self):
        """Seed default search providers if config does not exist."""
        import config
        env = config.load_env()
        
        defaults = [
            ServiceProvider(
                id="tavily-search",
                name="Tavily Search",
                category="search",
                enabled=True,
                base_url="https://api.tavily.com",
                priority=1,
                supported_capabilities=["search.web", "search.news"],
                api_keys=[env.get("TAVILY_API_KEY", "")] if env.get("TAVILY_API_KEY") else []
            ),
            ServiceProvider(
                id="brave-search",
                name="Brave Search",
                category="search",
                enabled=True,
                base_url="https://api.search.brave.com",
                priority=2,
                supported_capabilities=["search.web", "search.news", "search.image"],
                api_keys=[env.get("BRAVE_API_KEY", "")] if env.get("BRAVE_API_KEY") else []
            ),
            ServiceProvider(
                id="serper-search",
                name="Serper Search",
                category="search",
                enabled=False,
                base_url="https://google.serper.dev",
                priority=3,
                supported_capabilities=["search.web", "search.news", "search.image"],
                api_keys=[]
            ),
            ServiceProvider(
                id="searxng-search",
                name="SearXNG",
                category="search",
                enabled=False,
                base_url="http://localhost:8080",
                priority=4,
                supported_capabilities=["search.web"],
                api_keys=[]
            )
        ]
        for p in defaults:
            self.providers[p.id] = p
        self.save_providers()

    def get_healthiest_provider(
        self,
        category: str,
        capability: str,
        skip_providers: Optional[List[str]] = None
    ) -> Optional[ServiceProvider]:
        skip_providers = skip_providers or []
        
        # Check active override for category
        override_id = self.active_overrides.get(category)
        if override_id and override_id not in skip_providers:
            op = self.providers.get(override_id)
            if op and op.enabled and op.category == category and capability in op.supported_capabilities:
                consec_fail = op.stats.get("consecutive_failures", 0)
                if consec_fail < 3:
                    return op

        candidates = [
            p for p in self.providers.values()
            if p.enabled and p.category == category
            and capability in p.supported_capabilities
            and p.id not in skip_providers
        ]
        
        if not candidates:
            return None

        best_provider = None
        best_score = -999999.0
        
        for p in candidates:
            total_reqs = p.stats.get("successful_requests", 0) + p.stats.get("failed_requests", 0)
            success_rate = (p.stats["successful_requests"] / total_reqs) if total_reqs > 0 else 1.0
            
            consec_fail = p.stats.get("consecutive_failures", 0)
            health_score = max(0.0, 1.0 - (consec_fail * 0.33))
            
            avg_lat = p.stats.get("avg_latency_ms", 1000.0) or 1000.0
            latency_score = max(0.0, 1.0 - (avg_lat / 3000.0))
            
            score = (health_score * 0.4) + (success_rate * 0.3) + (latency_score * 0.3) - (p.priority * 0.01)
            
            if score > best_score:
                best_score = score
                best_provider = p

        return best_provider

    def get_active_key(self, provider_id: str, skip_keys: Optional[List[str]] = None) -> Optional[str]:
        p = self.providers.get(provider_id)
        if not p:
            return None
        if not p.api_keys:
            return None  # Keyless provider
            
        skip_keys = skip_keys or []
        for key in p.api_keys:
            if key in skip_keys:
                continue
            stats = p.key_stats.get(key, {})
            if stats.get("failures", 0) < 3:
                return key
        return None

    def record_success(self, provider_id: str, key: Optional[str], latency_ms: float):
        p = self.providers.get(provider_id)
        if not p:
            return
        p.stats["successful_requests"] += 1
        p.stats["consecutive_failures"] = 0
        p.stats["last_success"] = time.time()
        
        tot_lat = p.stats.get("total_latency_ms", 0.0) + latency_ms
        p.stats["total_latency_ms"] = tot_lat
        p.stats["avg_latency_ms"] = tot_lat / p.stats["successful_requests"]

        if key and key in p.key_stats:
            p.key_stats[key]["failures"] = 0
            p.key_stats[key]["successes"] += 1
            p.key_stats[key]["last_success"] = time.time()
        self.save_providers()

    def record_failure(self, provider_id: str, key: Optional[str], error: Any = None):
        p = self.providers.get(provider_id)
        if not p:
            return
        p.stats["failed_requests"] += 1
        p.stats["consecutive_failures"] = p.stats.get("consecutive_failures", 0) + 1
        p.stats["last_failure"] = time.time()

        if key and key in p.key_stats:
            p.key_stats[key]["failures"] += 1
            p.key_stats[key]["last_failure"] = time.time()
        self.save_providers()

    def reset_all_failures(self):
        for p in self.providers.values():
            p.stats["consecutive_failures"] = 0
            for k in p.key_stats:
                p.key_stats[k]["failures"] = 0
        self.save_providers()


def get_service_manager() -> ServiceProvidersManager:
    return ServiceProvidersManager()
