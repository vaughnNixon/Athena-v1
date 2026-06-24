import os
import logging
import openai
import config
import copilot_auth

logger = logging.getLogger("athena.providers")

_failure_counts = {}
_fallback_chain = ["gemini", "openrouter", "openai-api", "groq", "nvidia", "github-copilot"]

def get_fallback_provider(current_provider: str, available_providers: list) -> str:
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

def map_legacy_provider_id(provider_name: str) -> str:
    if provider_name == "openai":
        return "openai-api"
    return provider_name

def get_client_for_provider(provider_id: str, key: str = None) -> tuple:
    provider_id = map_legacy_provider_id(provider_id)
    
    from providers_manager import get_manager
    mgr = get_manager()
    p = mgr.providers.get(provider_id)
    if not p:
        raise ValueError(f"Provider not found: {provider_id}")
        
    if not key and p.api_keys:
        key = mgr.get_active_key(provider_id)
        
    model = p.default_model
    if mgr.active_model_override and mgr.active_provider_id == provider_id:
        model = mgr.active_model_override
        
    if p.type == "github_copilot":
        raw_token, token_src = copilot_auth.resolve_copilot_token()
        if not raw_token:
            raise ValueError("No GitHub Copilot token resolved.")
        api_token = copilot_auth.get_copilot_api_token(raw_token)
        default_headers = copilot_auth.copilot_request_headers()
        client = openai.OpenAI(
            api_key=api_token,
            base_url="https://api.githubcopilot.com",
            default_headers=default_headers
        )
        return client, model
        
    elif p.type == "gemini":
        base_url = p.base_url or "https://generativelanguage.googleapis.com/v1beta/openai/"
        client = openai.OpenAI(
            api_key=key or "",
            base_url=base_url
        )
        return client, model
        
    elif p.type == "openai_compatible":
        client = openai.OpenAI(
            api_key=key or "",
            base_url=p.base_url
        )
        return client, model
        
    else:
        raise ValueError(f"Unsupported provider type: {p.type}")

def get_routing_client(skip_providers: list = None, skip_keys: dict = None) -> tuple:
    """Resolves active client, handling failures and rotation.
    
    Returns (client, model_name, provider_name).
    """
    from providers_manager import get_manager, RoutingClientWrapper
    mgr = get_manager()
    
    skip_providers = skip_providers or []
    skip_keys = skip_keys or {}
    
    mapped_skips = [map_legacy_provider_id(s) for s in skip_providers]
    
    # Check if there are any configured providers at all
    configured = [p for p in mgr.providers.values() if p.enabled and (p.api_keys or p.type == "github_copilot")]
    if not configured:
        raise ValueError("No providers are configured. Please run 'athena onboard' to add credentials.")
        
    reset_attempted = False
    
    while True:
        # Check what providers are actually available (enabled and not skipped)
        available = [p.id for p in mgr.providers.values() if p.enabled and p.id not in mapped_skips and (p.api_keys or p.type == "github_copilot")]
        
        if not available:
            if not reset_attempted:
                logger.warning("All configured providers/keys are exhausted. Resetting failure statistics to retry.")
                mgr.reset_all_failures()
                mapped_skips = []
                skip_keys.clear()
                reset_attempted = True
                continue
            else:
                raise ValueError("No alternative providers are configured/available.")
                
        p = mgr.get_healthiest_provider(skip_providers=mapped_skips)
        if not p:
            if not reset_attempted:
                logger.warning("All configured providers/keys are exhausted. Resetting failure statistics to retry.")
                mgr.reset_all_failures()
                mapped_skips = []
                skip_keys.clear()
                reset_attempted = True
                continue
            else:
                raise ValueError("No alternative providers are configured/available.")
                
        if p.type == "github_copilot":
            key = None
        else:
            key = mgr.get_active_key(p.id, skip_keys=skip_keys.get(p.id))
            if not key:
                # No keys left for this provider in this run, skip the provider and retry
                mapped_skips.append(p.id)
                continue
                
        try:
            raw_client, model = get_client_for_provider(p.id, key=key)
            wrapped_client = RoutingClientWrapper(
                provider_id=p.id,
                key=key,
                client=raw_client,
                model=model
            )
            return wrapped_client, model, p.id
        except Exception as exc:
            logger.warning("Failed to initialize client for provider %s: %s", p.id, exc)
            if key:
                skip_keys.setdefault(p.id, []).append(key)
                mgr.record_key_failure(p.id, key, exc)
            else:
                mapped_skips.append(p.id)
                mgr.record_key_failure(p.id, None, exc)


def record_success(provider_name: str):
    from providers_manager import get_manager
    mgr = get_manager()
    provider_id = map_legacy_provider_id(provider_name)
    key = mgr.get_active_key(provider_id)
    mgr.record_key_success(provider_id, key)

def record_failure(provider_name: str):
    from providers_manager import get_manager
    mgr = get_manager()
    provider_id = map_legacy_provider_id(provider_name)
    key = mgr.get_active_key(provider_id)
    mgr.record_key_failure(provider_id, key)
