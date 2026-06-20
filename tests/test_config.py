import pytest
import os
import tempfile
import shutil
from pathlib import Path
import yaml

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config

@pytest.fixture(autouse=True)
def cleanup():
    yield
    # Cleanup temp home dir
    home = Path(temp_dir)
    if home.exists():
        for item in home.glob("**/*"):
            if item.is_file():
                try:
                    os.remove(item)
                except Exception:
                    pass
            elif item.is_dir():
                try:
                    shutil.rmtree(item)
                except Exception:
                    pass

def test_ensure_athena_dirs():
    config.ensure_athena_dirs()
    home = config.get_athena_home()
    assert (home / "knowledge").exists()
    assert (home / "skills" / "caveman").exists()
    assert (home / "logs").exists()
    assert (home / "config.yaml").exists()
    assert (home / ".env").exists()

def test_load_and_save_config():
    config.ensure_athena_dirs()
    cfg = config.load_config()
    assert cfg.get("provider") == "gemini"
    
    # Modify config and save
    cfg["provider"] = "openai"
    cfg["model"] = "gpt-4o"
    config.save_config(cfg)
    
    # Reload config
    reloaded = config.load_config()
    assert reloaded.get("provider") == "openai"
    assert reloaded.get("model") == "gpt-4o"

def test_load_env():
    config.ensure_athena_dirs()
    env_file = config.get_athena_home() / ".env"
    env_file.write_text("COPILOT_GITHUB_TOKEN=test_token_123\n# Comment line\nINVALID_LINE\nSOME_VAR=\"hello_world\"\n", encoding="utf-8")
    
    env_vars = config.load_env()
    assert env_vars.get("COPILOT_GITHUB_TOKEN") == "test_token_123"
    assert env_vars.get("SOME_VAR") == "hello_world"
    assert "INVALID_LINE" not in env_vars

def test_fetch_caveman_skills_fallback():
    # Calling fetch_caveman_skills with network failures should create local fallback file
    # Ensure directory exists but block GitHub URLs by forcing urllib/DNS/connection error (not needed, we just verify it writes fallbacks if GitHub call fails)
    # We can mock urlopen to raise exception
    import urllib.request
    from unittest.mock import patch
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = Exception("Network Down")
        config.fetch_caveman_skills(force=True)
        
    skills_dir = config.get_athena_home() / "skills" / "caveman"
    assert (skills_dir / "SKILL.md").exists()
    assert "Fallback" in (skills_dir / "SKILL.md").read_text()
