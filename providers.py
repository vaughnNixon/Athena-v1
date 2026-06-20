import os
import logging
import openai
import config
import copilot_auth
import openai_auth

logger = logging.getLogger("athena.providers")

# Consecutive failure track
_failure_counts = {}
_fallback_chain = ["gemini", "openrouter", "openai", "groq", "nvidia", "github-copilot"]

def get_fallback_provider(current_provider: str, available_providers: list) -> str:
    try:
        idx = _fallback_chain.index(current_provider)
        # Search forward in the chain
        for candidate in _fallback_chain[idx+1:] + _fallback_chain[:idx]:
            if candidate in available_providers:
                return candidate
    except ValueError:
        pass
    return available_providers[0] if available_providers else ""

def get_client_for_provider(provider_name: str) -> tuple[openai.OpenAI, str]:
    cfg = config.load_config()
    env = config.load_env()
    
    # 1. Resolve API key / token
    api_key = ""
    base_url = None
    default_headers = None
    
    # Determine model
    prov_cfg = cfg.get("providers", {}).get(provider_name, {})
    model = prov_cfg.get("model", "")
    
    if provider_name == "github-copilot":
        raw_token, token_src = copilot_auth.resolve_copilot_token()
        if not raw_token:
            raise ValueError("No GitHub Copilot token resolved.")
        api_token = copilot_auth.get_copilot_api_token(raw_token)
        api_key = api_token
        base_url = "https://api.githubcopilot.com"
        default_headers = copilot_auth.copilot_request_headers()
        if not model:
            model = "gpt-4o"  # default Copilot chat model
    else:
        # Standard OpenAI compatible provider keys
        env_keys = {
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "groq": "GROQ_API_KEY",
            "nvidia": "NVIDIA_API_KEY"
        }
        
        cfg_key = prov_cfg.get("api_key", "")
        env_key = env.get(env_keys.get(provider_name, ""), "") or os.environ.get(env_keys.get(provider_name, ""), "")
        api_key = cfg_key or env_key
        
        auth_type = prov_cfg.get("auth_type", "api")
        if not api_key and not (provider_name == "openai" and auth_type == "oauth"):
            raise ValueError(f"No API key configured for provider: {provider_name}")
            
        if provider_name == "gemini":
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            if not model:
                model = "gemini-1.5-flash"
        elif provider_name == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
            if not model:
                model = "google/gemini-flash-1.5-8b"
        elif provider_name == "openai":
            auth_type = prov_cfg.get("auth_type", "api")
            if auth_type == "oauth":
                access_token, account_id = openai_auth.get_chatgpt_access_token()
                if not access_token:
                    raise ValueError("No ChatGPT Pro/Plus OAuth token resolved.")
                
                import codex_transport
                if not model:
                    model = "gpt-5.5"
                client = codex_transport.CodexClient(access_token, account_id)
                return client, model
            else:
                base_url = "https://api.openai.com/v1"
                if not model:
                    model = "gpt-4o-mini"
        elif provider_name == "groq":
            base_url = "https://api.groq.com/openai/v1"
            if not model:
                model = "llama-3.1-8b-instant"
        elif provider_name == "nvidia":
            base_url = "https://integrate.api.nvidia.com/v1"
            if not model:
                model = "meta/llama3-70b-instruct"
                
    client = openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=default_headers
    )
    return client, model

def get_routing_client(skip_providers: list = None) -> tuple[openai.OpenAI, str, str]:
    """Resolves active client, handling failures and rotation.
    
    Returns (client, model_name, provider_name).
    """
    cfg = config.load_config()
    active_provider = cfg.get("provider", "gemini")
    
    # Check what providers actually have credentials
    available = []
    env = config.load_env()
    
    providers_keys = {
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "nvidia": "NVIDIA_API_KEY"
    }
    
    for prov, env_key in providers_keys.items():
        prov_cfg = cfg.get("providers", {}).get(prov, {})
        cfg_key = prov_cfg.get("api_key", "")
        env_key_val = env.get(env_key, "") or os.environ.get(env_key, "")
        if cfg_key or env_key_val:
            available.append(prov)
        elif prov == "openai" and prov_cfg.get("auth_type") == "oauth" and env.get("CHATGPT_REFRESH_TOKEN"):
            available.append(prov)
            
    copilot_token, _ = copilot_auth.resolve_copilot_token()
    if copilot_token:
        available.append("github-copilot")
        
    if skip_providers:
        available = [p for p in available if p not in skip_providers]
        
    if not available:
        if skip_providers:
            raise ValueError("No alternative providers are configured/available.")
        else:
            raise ValueError("No providers are configured. Please run 'athena onboard' to add credentials.")
        
    if skip_providers and active_provider in skip_providers:
        fallback = get_fallback_provider(active_provider, available)
        if fallback:
            active_provider = fallback
            
    # Rotate active provider if failure count is exceeded
    if _failure_counts.get(active_provider, 0) >= 3:
        fallback = get_fallback_provider(active_provider, available)
        if fallback and fallback != active_provider:
            logger.warning("Provider '%s' has failed 3 times. Rotating to fallback: '%s'", active_provider, fallback)
            cfg["provider"] = fallback
            prov_cfg = cfg.get("providers", {}).get(fallback, {})
            cfg["model"] = prov_cfg.get("model", "")
            config.save_config(cfg)
            active_provider = fallback
            
    # Try getting the client. If it fails due to config error, try next immediately.
    try:
        client, model = get_client_for_provider(active_provider)
        return client, model, active_provider
    except Exception as exc:
        logger.error("Failed to build client for '%s': %s. Trying fallback immediately.", active_provider, exc)
        _failure_counts[active_provider] = 3
        # Recurse once to route fallback
        return get_routing_client(skip_providers=skip_providers)

def record_success(provider_name: str):
    _failure_counts[provider_name] = 0

def record_failure(provider_name: str):
    _failure_counts[provider_name] = _failure_counts.get(provider_name, 0) + 1
    logger.warning("Recorded failure for provider '%s' (Count: %d/3)", provider_name, _failure_counts[provider_name])
