import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from types import SimpleNamespace

logger = logging.getLogger("athena.providers_manager")

_fallback_chain = ["gemini", "openrouter", "openai-api", "groq", "nvidia", "github-copilot"]

def get_fallback_provider(current_provider: str, available_providers: List[str]) -> str:
    try:
        idx = _fallback_chain.index(current_provider)
        for candidate in _fallback_chain[idx+1:] + _fallback_chain[:idx]:
            if candidate in available_providers:
                return candidate
    except ValueError:
        pass
    for p in available_providers:
        if p != current_provider:
            return p
    return available_providers[0] if available_providers else ""

class Provider:
    def __init__(self, id: str, name: str, type: str, base_url: str, default_model: str, enabled: bool = True, api_keys: List[str] = None, key_stats: Dict[str, dict] = None, stats: Dict[str, Any] = None):
        self.id = id
        self.name = name
        self.type = type
        self.base_url = base_url
        self.default_model = default_model
        self.enabled = enabled
        self.api_keys = api_keys or []
        self.key_stats = key_stats or {}
        self.stats = stats or {
            "successful_requests": 0,
            "failed_requests": 0,
            "consecutive_failures": 0,
            "last_success": None,
            "last_failure": None
        }
        
        # Ensure key_stats dictionary matches api_keys list
        for key in self.api_keys:
            if key not in self.key_stats:
                self.key_stats[key] = {
                    "failures": 0,
                    "successes": 0,
                    "last_success": None,
                    "last_failure": None
                }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "enabled": self.enabled,
            "api_keys": self.api_keys,
            "key_stats": self.key_stats,
            "stats": self.stats
        }

class RoutingClientWrapper:
    def __init__(self, provider_id: str, key: str, client: Any, model: str):
        self.provider_id = provider_id
        self.key = key
        self.client = client
        self.model = model
        self.chat = self.Chat(self)

    def __getattr__(self, name):
        return getattr(self.client, name)

    class Chat:
        def __init__(self, parent):
            self.completions = self.Completions(parent)

        class Completions:
            def __init__(self, parent):
                self.parent = parent

            def create(self, *args, **kwargs):
                if "model" not in kwargs or not kwargs["model"]:
                    kwargs["model"] = self.parent.model
                try:
                    res = self.parent.client.chat.completions.create(*args, **kwargs)
                    get_manager().record_key_success(self.parent.provider_id, self.parent.key)
                    return res
                except Exception as exc:
                    get_manager().record_key_failure(self.parent.provider_id, self.parent.key, exc)
                    raise exc

class ProvidersManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ProvidersManager, cls).__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        import config
        self.filepath = config.get_athena_home() / "providers.json"
        self.providers: Dict[str, Provider] = {}
        self.active_provider_id: Optional[str] = None
        self.active_model_override: Optional[str] = None
        self._initialized = True
        self.migrate_legacy_config()
        self.load_providers()

    def get_providers_file_path(self) -> Path:
        return self.filepath

    def load_providers(self):
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            self.active_provider_id = data.get("active_provider_id")
            self.active_model_override = data.get("active_model_override")
            self.providers.clear()
            for p_dict in data.get("providers", []):
                p = Provider(
                    id=p_dict["id"],
                    name=p_dict["name"],
                    type=p_dict["type"],
                    base_url=p_dict["base_url"],
                    default_model=p_dict["default_model"],
                    enabled=p_dict.get("enabled", True),
                    api_keys=p_dict.get("api_keys"),
                    key_stats=p_dict.get("key_stats"),
                    stats=p_dict.get("stats")
                )
                self.providers[p.id] = p
        except Exception as exc:
            logger.error("Failed to load providers.json: %s", exc)

    def save_providers(self):
        try:
            data = {
                "active_provider_id": self.active_provider_id,
                "active_model_override": self.active_model_override,
                "providers": [p.to_dict() for p in self.providers.values()]
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error("Failed to save providers.json: %s", exc)

    def migrate_legacy_config(self):
        if self.filepath.exists():
            return
            
        import config
        config.ensure_athena_dirs()
        cfg = config.load_config()
        env = config.load_env()
        
        legacy_active = cfg.get("provider", "gemini")
        
        providers_keys = {
            "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3-flash", "gemini"),
            "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemini-flash-1.5-8b", "openai_compatible"),
            "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.1-8b-instant", "openai_compatible"),
            "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "meta/llama3-70b-instruct", "openai_compatible")
        }
        
        for pid, (env_var, url, model, ptype) in providers_keys.items():
            prov_cfg = cfg.get("providers", {}).get(pid, {})
            cfg_key = prov_cfg.get("api_key", "")
            env_key = env.get(env_var, "") or os.environ.get(env_var, "")
            key = cfg_key or env_key
            
            keys = [key] if key else []
            self.providers[pid] = Provider(
                id=pid,
                name=pid.capitalize(),
                type=ptype,
                base_url=url,
                default_model=prov_cfg.get("model") or model,
                api_keys=keys
            )
            
        # Migrate OpenAI API and OAuth
        openai_cfg = cfg.get("providers", {}).get("openai", {})
        auth_type = openai_cfg.get("auth_type", "api")
        
        openai_key = openai_cfg.get("api_key", "") or env.get("OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        self.providers["openai-api"] = Provider(
            id="openai-api",
            name="OpenAI API",
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            default_model=openai_cfg.get("model") or "gpt-4o-mini",
            api_keys=[openai_key] if openai_key else []
        )
        
        self.providers["github-copilot"] = Provider(
            id="github-copilot",
            name="GitHub Copilot",
            type="github_copilot",
            base_url="https://api.githubcopilot.com",
            default_model="gpt-4o",
            api_keys=[]
        )
        
        if legacy_active == "openai":
            self.active_provider_id = "openai-api"
        else:
            self.active_provider_id = legacy_active
            
        self.save_providers()

    def add_provider(self, name: str, type: str, base_url: str, default_model: str, api_keys: List[str] = None) -> Provider:
        provider_id = name.strip().lower().replace(" ", "-")
        # Ensure unique ID
        counter = 1
        original_id = provider_id
        while provider_id in self.providers:
            provider_id = f"{original_id}-{counter}"
            counter += 1
            
        p = Provider(
            id=provider_id,
            name=name,
            type=type,
            base_url=base_url,
            default_model=default_model,
            api_keys=api_keys
        )
        self.providers[provider_id] = p
        self.save_providers()
        return p

    def remove_provider(self, provider_id: str) -> bool:
        if provider_id in self.providers:
            del self.providers[provider_id]
            if self.active_provider_id == provider_id:
                self.active_provider_id = None
            self.save_providers()
            return True
        return False

    def enable_provider(self, provider_id: str, enabled: bool = True) -> bool:
        p = self.providers.get(provider_id)
        if p:
            p.enabled = enabled
            self.save_providers()
            return True
        return False

    def get_healthiest_provider(self, skip_providers: List[str] = None) -> Optional[Provider]:
        skip_providers = skip_providers or []
        candidates = [p for p in self.providers.values() if p.enabled and p.id not in skip_providers]
        if not candidates:
            return None

        # Check if overridden active provider is healthy (Circuit Breaker: consecutive_failures must be < 2)
        if self.active_provider_id and self.active_provider_id not in skip_providers:
            active_p = self.providers.get(self.active_provider_id)
            if active_p and active_p.enabled and active_p.stats.get("consecutive_failures", 0) < 2:
                if active_p.type == "github_copilot":
                    return active_p
                else:
                    if any(stats["failures"] < 3 for stats in active_p.key_stats.values()) or not active_p.api_keys:
                        return active_p

        # Find best candidate by scoring
        best_candidate = None
        best_score = -999999
        for p in candidates:
            if p.type == "github_copilot":
                consecutive_failures = p.stats.get("consecutive_failures", 0)
                healthy_keys = 1 if consecutive_failures < 3 else 0
                total_consecutive_failures = consecutive_failures
            else:
                healthy_keys = sum(1 for stats in p.key_stats.values() if stats["failures"] < 3)
                total_consecutive_failures = sum(stats["failures"] for stats in p.key_stats.values())

            score = healthy_keys - total_consecutive_failures
            if score > best_score:
                best_score = score
                best_candidate = p
                
        return best_candidate

    def get_active_key(self, provider_id: str, skip_keys: List[str] = None) -> Optional[str]:
        self.load_providers()
        p = self.providers.get(provider_id)
        if not p or not p.api_keys:
            return None
        skip_keys = skip_keys or []
        # Find first non-exhausted key
        for key in p.api_keys:
            if key in skip_keys:
                continue
            stats = p.key_stats.get(key, {})
            if stats.get("failures", 0) < 3:
                return key
        return None

    def reset_all_failures(self):
        for p in self.providers.values():
            p.stats["consecutive_failures"] = 0
            p.stats["failed_requests"] = 0
            for k in p.key_stats:
                p.key_stats[k]["failures"] = 0
        self.save_providers()


    def record_key_success(self, provider_id: str, key: Optional[str]):
        p = self.providers.get(provider_id)
        if not p:
            return
        p.stats["successful_requests"] += 1
        p.stats["consecutive_failures"] = 0
        p.stats["last_success"] = time.time()
        
        if key and key in p.key_stats:
            p.key_stats[key]["failures"] = 0
            p.key_stats[key]["successes"] += 1
            p.key_stats[key]["last_success"] = time.time()
        self.save_providers()

    def record_key_failure(self, provider_id: str, key: Optional[str], error: Any = None):
        p = self.providers.get(provider_id)
        if not p:
            return
        p.stats["failed_requests"] += 1
        p.stats["consecutive_failures"] = p.stats.get("consecutive_failures", 0) + 1
        p.stats["last_failure"] = time.time()
        
        if key and key in p.key_stats:
            p.key_stats[key]["failures"] += 1
            p.key_stats[key]["last_failure"] = time.time()
        elif key:
            p.key_stats[key] = {
                "failures": 1,
                "successes": 0,
                "last_success": None,
                "last_failure": time.time()
            }
        self.save_providers()

def get_manager() -> ProvidersManager:
    return ProvidersManager()
