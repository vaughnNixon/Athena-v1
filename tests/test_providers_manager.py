import pytest
import os
import tempfile
import json
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import providers
from providers_manager import ProvidersManager, Provider, get_manager

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    ProvidersManager._instance = None
    import config as cfg_mod
    filepath = cfg_mod.get_athena_home() / "providers.json"
    if filepath.exists():
        filepath.unlink()
    yield
    if filepath.exists():
        filepath.unlink()

def test_provider_serialization():
    p = Provider(
        id="test-id",
        name="Test Provider",
        type="openai_compatible",
        base_url="https://api.test.com",
        default_model="test-model",
        api_keys=["key1", "key2"]
    )
    assert p.id == "test-id"
    assert p.api_keys == ["key1", "key2"]
    assert "key1" in p.key_stats
    
    d = p.to_dict()
    assert d["id"] == "test-id"
    assert d["api_keys"] == ["key1", "key2"]
    assert "key1" in d["key_stats"]

def test_migration_from_legacy_config():
    # Save a legacy-style config
    cfg = config.load_config()
    cfg["provider"] = "gemini"
    cfg["providers"] = {
        "gemini": {"api_key": "gemini_key_123", "model": "gemini-3-flash"},
        "openrouter": {"api_key": "openrouter_key", "model": "google/gemini-flash-1.5-8b"}
    }
    config.save_config(cfg)
    
    mgr = get_manager()
    assert "gemini" in mgr.providers
    assert "openrouter" in mgr.providers
    
    gemini_p = mgr.providers["gemini"]
    assert gemini_p.api_keys == ["gemini_key_123"]
    assert gemini_p.default_model == "gemini-3-flash"
    assert mgr.active_provider_id == "gemini"

def test_add_remove_enable_providers():
    mgr = get_manager()
    
    # 1. Add
    p = mgr.add_provider(
        name="Grok AI",
        type="openai_compatible",
        base_url="https://api.x.ai/v1",
        default_model="grok-4",
        api_keys=["key1"]
    )
    assert p.id == "grok-ai"
    assert p.name == "Grok AI"
    assert p.type == "openai_compatible"
    assert p.base_url == "https://api.x.ai/v1"
    assert p.default_model == "grok-4"
    assert p.api_keys == ["key1"]
    
    assert "grok-ai" in mgr.providers
    
    # 2. Disable/Enable
    mgr.enable_provider("grok-ai", False)
    assert not mgr.providers["grok-ai"].enabled
    
    mgr.enable_provider("grok-ai", True)
    assert mgr.providers["grok-ai"].enabled
    
    # 3. Remove
    assert mgr.remove_provider("grok-ai")
    assert "grok-ai" not in mgr.providers

def test_health_scoring_and_selection():
    mgr = get_manager()
    mgr.providers.clear()
    
    # Add two mock providers
    p1 = mgr.add_provider("P1", "openai_compatible", "http://p1", "m1", ["k1"])
    p2 = mgr.add_provider("P2", "openai_compatible", "http://p2", "m2", ["k2"])
    
    # Set P2 active
    mgr.active_provider_id = "p2"
    
    # P2 should be returned if healthy
    best = mgr.get_healthiest_provider()
    assert best.id == "p2"
    
    # Fail P2 key 3 times
    mgr.record_key_failure("p2", "k2")
    mgr.record_key_failure("p2", "k2")
    mgr.record_key_failure("p2", "k2")
    
    # Now get_healthiest_provider should fall back to P1 (since P2 active is unhealthy)
    best = mgr.get_healthiest_provider()
    assert best.id == "p1"


def test_key_rotation_logic():
    mgr = get_manager()
    p = mgr.add_provider("Grok", "openai_compatible", "http://grok", "grok-4", ["key1", "key2", "key3"])
    
    # Default active key should be key1
    assert mgr.get_active_key("grok") == "key1"
    
    # If key1 fails once, it is still selected if we don't skip it
    mgr.record_key_failure("grok", "key1")
    assert mgr.get_active_key("grok") == "key1"
    
    # If key1 is skipped during turn, get_active_key should return key2
    assert mgr.get_active_key("grok", skip_keys=["key1"]) == "key2"
    
    # If key1 fails 3 times, get_active_key should automatically return key2 without skip_keys
    mgr.record_key_failure("grok", "key1")
    mgr.record_key_failure("grok", "key1")
    assert mgr.get_active_key("grok") == "key2"
    
    # If all keys fail 3 times, get_active_key returns None
    mgr.record_key_failure("grok", "key2")
    mgr.record_key_failure("grok", "key2")
    mgr.record_key_failure("grok", "key2")
    mgr.record_key_failure("grok", "key3")
    mgr.record_key_failure("grok", "key3")
    mgr.record_key_failure("grok", "key3")
    assert mgr.get_active_key("grok") is None

def test_routing_client_failover_integration():
    mgr = get_manager()
    mgr.providers.clear()
    
    p1 = mgr.add_provider("P1", "openai_compatible", "http://p1", "m1", ["k1"])
    p2 = mgr.add_provider("P2", "openai_compatible", "http://p2", "m2", ["k2"])
    
    mgr.active_provider_id = "p1"
    
    # Record P1 key failed
    mgr.record_key_failure("p1", "k1")
    mgr.record_key_failure("p1", "k1")
    mgr.record_key_failure("p1", "k1")
    
    # If we get routing client, it should failover to P2
    with patch("openai.OpenAI") as mock_openai:
        client, model, prov_id = providers.get_routing_client()
        assert prov_id == "p2"
        assert model == "m2"

def test_self_healing_reset():
    mgr = get_manager()
    mgr.providers.clear()
    p = mgr.add_provider("P1", "openai_compatible", "http://p1", "m1", ["k1"])
    
    mgr.record_key_failure("p1", "k1")
    mgr.record_key_failure("p1", "k1")
    mgr.record_key_failure("p1", "k1")
    
    assert mgr.get_active_key("p1") is None
    
    # Routing client with exhausted keys should reset all failures and succeed
    with patch("openai.OpenAI") as mock_openai:
        client, model, prov_id = providers.get_routing_client()
        assert prov_id == "p1"
        assert mgr.get_active_key("p1") == "k1"

def test_onboarding_menu_driven_wizard():
    from main import run_onboarding
    from providers_manager import get_manager
    mgr = get_manager()
    mgr.providers.clear()
    mgr.save_providers()
    
    prompt_responses = [
        "2",          # Select Configure OpenAI-compatible Keys
        "1",          # Sub-menu Select Add new provider
        "Grok",       # Provider Name
        "http://x.ai",# Base URL
        "grok-4",     # Default Model
        "key1",       # Add key1
        "key2",       # Add key2
        "",           # Finish keys (blank entry)
        "3",          # Sub-menu Select Back to main menu
        "5"           # Main menu Select Exit Setup Wizard
    ]
    
    with patch("rich.prompt.Prompt.ask", side_effect=prompt_responses):
        run_onboarding()
        
    mgr.load_providers()
    assert "grok" in mgr.providers
    grok_p = mgr.providers["grok"]
    assert grok_p.name == "Grok"
    assert grok_p.base_url == "http://x.ai"
    assert grok_p.default_model == "grok-4"
    assert grok_p.api_keys == ["key1", "key2"]
    
    prompt_responses_2 = [
        "2",          # Select Configure OpenAI-compatible Keys
        "2",          # Sub-menu select Grok (option 2)
        "key3",       # Append key3
        "",           # Finish keys (blank entry)
        "3",          # Sub-menu Select Back to main menu
        "5"           # Main menu Select Exit Setup Wizard
    ]
    
    with patch("rich.prompt.Prompt.ask", side_effect=prompt_responses_2):
        run_onboarding()
        
    mgr.load_providers()
    grok_p = mgr.providers["grok"]
    assert grok_p.api_keys == ["key1", "key2", "key3"]

