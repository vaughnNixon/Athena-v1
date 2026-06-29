import os
import sys
import yaml
import urllib.request
import logging
from pathlib import Path

logger = logging.getLogger("athena.config")

DEFAULT_CONFIG_YAML = """# Athena v1 Config
provider: "gemini"
model: "gemini-3-flash"
memory:
  decay_rate: 0.05
  importance_range: [1, 10]
  weights:
    keyword: 1.0
    importance: 1.0
    confidence: 1.0
  active_token_budget: 50000
  demotion_frequency: "daily"
  orphan_threshold: 10000
  keyword_confidence_threshold: 0.6
  embedding_enabled: false
  embedding_provider: "gemini"
  embedding_top_k: 3
  desperation_enabled: true
  adaptive_learning_enabled: true
  learning_confidence_threshold: 0.8
  compression:
    caveman_threshold_tokens: 1000
    headroom_max_injection_tokens: 500
session_continuity:
  enabled: true
  idle_trigger_minutes: 60
  provider_pressure_threshold: 0.80
  context_pressure_warn: 0.85
  context_pressure_new_chat: 0.95
  session_ttl_hours: 24
  archive_retention_hours: 72
  topic_decay_interval_minutes: 5
  topic_dormant_threshold: 0.40
  topic_inactive_threshold: 0.15
maintenance_provider:
  enabled: false
  provider: ""
  model: ""
providers:
  gemini:
    api_key: ""
    model: "gemini-3-flash"
  openrouter:
    api_key: ""
    model: "google/gemini-flash-1.5-8b"
  openai:
    api_key: ""
    model: "gpt-4o-mini"
  groq:
    api_key: ""
    model: "llama-3.1-8b-instant"
  nvidia:
    api_key: ""
    model: "meta/llama3-70b-instruct"
"""

def get_athena_home() -> Path:
    home_dir = os.environ.get("ATHENA_HOME")
    if home_dir:
        return Path(home_dir).expanduser().resolve()
    return Path.home() / ".athena"

def ensure_athena_dirs():
    home = get_athena_home()
    (home / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "caveman").mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    
    config_file = home / "config.yaml"
    if not config_file.exists():
        config_file.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        
    env_file = home / ".env"
    if not env_file.exists():
        env_file.write_text("# Athena Environment Credentials\n", encoding="utf-8")

def load_config() -> dict:
    ensure_athena_dirs()
    config_file = get_athena_home() / "config.yaml"
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error("Failed to load config.yaml: %s", exc)
        return {}

def save_config(config_dict: dict):
    ensure_athena_dirs()
    config_file = get_athena_home() / "config.yaml"
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_dict, f, sort_keys=False, allow_unicode=True)
    except Exception as exc:
        logger.error("Failed to save config.yaml: %s", exc)

def load_env() -> dict:
    ensure_athena_dirs()
    env_file = get_athena_home() / ".env"
    env_vars = {}
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    k, _, v = stripped.partition("=")
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as exc:
            logger.error("Failed to read .env: %s", exc)
    return env_vars

def fetch_caveman_skills(force: bool = False):
    import threading
    ensure_athena_dirs()
    skills_dir = get_athena_home() / "skills" / "caveman"
    
    files_to_fetch = {
        "SKILL.md": "https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman/SKILL.md",
        "caveman-commit.md": "https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman-commit/SKILL.md",
        "caveman-review.md": "https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman-review/SKILL.md"
    }

    def _do_fetch():
        for filename, url in files_to_fetch.items():
            filepath = skills_dir / filename
            if filepath.exists() and not force:
                continue
            try:
                logger.info("Fetching Caveman skill: %s", filename)
                req = urllib.request.Request(
                    url, 
                    headers={"User-Agent": "Mozilla/5.0 (AthenaAgent/1.0)"}
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    content = response.read().decode("utf-8")
                    filepath.write_text(content, encoding="utf-8")
            except Exception as exc:
                logger.warning("Could not fetch Caveman skill %s from GitHub: %s. Using local fallback.", filename, exc)
                if not filepath.exists():
                    filepath.write_text(
                        f"# Caveman Skill Fallback: {filename}\n"
                        "Instructions: Adopt a highly compressed, sparse, telegraphic prose style. "
                        "Remove greetings and conversational padding. Do not write full prose sentences.",
                        encoding="utf-8"
                    )

    if "pytest" in sys.modules:
        _do_fetch()
    else:
        threading.Thread(target=_do_fetch, daemon=True).start()
