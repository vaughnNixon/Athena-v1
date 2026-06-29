import os
import re
import logging
from pathlib import Path
import config

logger = logging.getLogger("athena.people_manager")

def get_people_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    people_dir = workspace_root / "knowledge" / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    return people_dir

def list_people() -> dict:
    """Returns mapping of entity names (lowercase) to file paths."""
    p_dir = get_people_dir()
    result = {}
    for f in p_dir.glob("*.md"):
        name_key = f.stem.lower()
        result[name_key] = f
    return result

_REGEX_CACHE = {}

def get_compiled_regex(name: str):
    if name not in _REGEX_CACHE:
        _REGEX_CACHE[name] = re.compile(rf"\b{re.escape(name)}\b")
    return _REGEX_CACHE[name]

def get_relevant_people_context(user_message: str) -> str:
    """
    Scans user message for mentions of registered personal entities (friends, family, pets).
    If mentioned, reads their profile and returns injected context. Zero token cost if no match.
    """
    people_map = list_people()
    if not people_map:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    for name, fpath in people_map.items():
        # Word boundary match to avoid false positives (e.g. "luck" matching "lucky")
        pattern = get_compiled_regex(name)
        if pattern.search(msg_lower):
            try:
                content = fpath.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- PERSONAL ENTITY PROFILE ({name.title()}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read entity profile for %s: %s", name, e)

                
    if matched_contexts:
        return "\n\n".join(matched_contexts)
    return ""
