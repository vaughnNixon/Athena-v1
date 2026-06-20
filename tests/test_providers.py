import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import providers

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    # Reset failure tracking dictionary in providers
    providers._failure_counts.clear()
    yield

def test_get_fallback_provider():
    available = ["gemini", "openrouter", "openai"]
    
    # Check fallback chain rotation
    fb1 = providers.get_fallback_provider("gemini", available)
    assert fb1 == "openrouter"
    
    fb2 = providers.get_fallback_provider("openrouter", available)
    assert fb2 == "openai"
    
    fb3 = providers.get_fallback_provider("openai", available)
    assert fb3 == "gemini" # wrap around
    
    # Handle single choice
    fb_single = providers.get_fallback_provider("openai", ["openai"])
    assert fb_single == "openai"

def test_record_failures_and_rotate():
    # Save config with gemini as default provider
    cfg = config.load_config()
    cfg["provider"] = "gemini"
    cfg["providers"] = {
        "gemini": {"api_key": "gemini_key", "model": "gemini-3-flash"},
        "openai": {"api_key": "openai_key", "model": "gpt-4o-mini"}
    }
    config.save_config(cfg)
    
    # 1. Initially routing client should return gemini
    with patch("openai.OpenAI") as mock_openai_client:
        client, model, prov = providers.get_routing_client()
        assert prov == "gemini"
        assert model == "gemini-3-flash"
        
    # 2. Record 3 failures on gemini
    providers.record_failure("gemini")
    providers.record_failure("gemini")
    providers.record_failure("gemini")
    
    # 3. Next routing attempt should trigger rotation to openai
    with patch("openai.OpenAI") as mock_openai_client:
        client, model, prov = providers.get_routing_client()
        assert prov == "openai"
        assert model == "gpt-4o-mini"
        
    # Verify saved config has rotated the default provider
    cfg_reloaded = config.load_config()
    assert cfg_reloaded.get("provider") == "openai"

def test_routing_client_no_credentials():
    # Clear any credentials
    cfg = config.load_config()
    cfg["providers"] = {}
    config.save_config(cfg)
    
    # Mock resolve_copilot_token to return empty
    with patch("copilot_auth.resolve_copilot_token", return_value=("", "")):
        with pytest.raises(ValueError, match="No providers are configured"):
            providers.get_routing_client()

def test_get_client_for_provider_openai_oauth():
    cfg = config.load_config()
    cfg["providers"] = {
        "openai": {
            "auth_type": "oauth",
            "model": "gpt-5.5"
        }
    }
    config.save_config(cfg)
    
    with patch("openai_auth.get_chatgpt_access_token", return_value=("fake_access_token", "fake_acc_id")):
        import codex_transport
        client, model = providers.get_client_for_provider("openai")
        assert model == "gpt-5.5"
        assert isinstance(client, codex_transport.CodexClient)
        assert client.chat.completions.access_token == "fake_access_token"
        assert client.chat.completions.account_id == "fake_acc_id"
