import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine

logger = logging.getLogger("athena.daily_manager")

def get_daily_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    d_dir = workspace_root / "knowledge" / "daily"
    d_dir.mkdir(parents=True, exist_ok=True)
    return d_dir

def generate_daily_note(date_str: str = None) -> str:
    """Generates and writes a clean Markdown daily journal note for humans based on SQLite records."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
        
    d_dir = get_daily_dir()
    filepath = d_dir / f"{date_str}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Query SQLite for facts and chunks created today
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        try:
            dt_start = datetime.strptime(date_str, "%Y-%m-%d")
            ts_start = int(dt_start.timestamp())
            ts_end = ts_start + 86400
        except Exception:
            ts_start = 0
            ts_end = int(time.time()) + 86400
            
        cursor.execute("SELECT fact, category FROM facts WHERE created_at >= ? AND created_at < ?", (ts_start, ts_end))
        facts_today = cursor.fetchall()
    finally:
        conn.close()
        
    # Extract topics and decisions
    decisions = [f[0] for f in facts_today if f[1] in ("important_decision", "decision")]
    tasks = [f[0] for f in facts_today if f[1] == "task"]
    general = [f[0] for f in facts_today if f[1] not in ("important_decision", "decision", "task")]
    
    # Format template components matching user screenshot
    summary_text = "Integrated core subsystems and executed workspace tasks."
    if general:
        summary_text = "; ".join(general[:3])
        
    dec_formatted = "\n".join([f"- {d}" for d in decisions]) if decisions else "- Implemented daily journal tracking layer."
    open_loops = "\n".join([f"- [ ] {t}" for t in tasks]) if tasks else "- [ ] Review tomorrow's schedule."
    
    # Check people and projects mentioned in registered directories
    import people_manager
    people = list(people_manager.list_people().keys())
    people_formatted = "\n".join([f"- People/{p.title()}" for p in people]) if people else "- None"

    
    md_content = f"""# {date_str}

## Properties
- type: daily
- date: {date_str}
- tags: daily

## Daily Summary
{summary_text}

## Decisions / Signals
{dec_formatted}

## Open Loops
{open_loops}

## People Mentioned
{people_formatted}

## Projects Touched
- Projects/Wise Maxwell (Athena v1.3)
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Generated daily note at %s", filepath)
    return md_content

def get_relevant_daily_context(user_message: str) -> str:
    """
    Scans user query for mentions of daily logs or specific dates.
    Returns matched daily context on demand. Zero token cost if no match.
    """
    d_dir = get_daily_dir()
    all_files = list(d_dir.glob("*.md"))
    if not all_files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    triggers = ["today", "daily", "yesterday", "journal"]
    is_daily_query = any(w in msg_lower for w in triggers)
    
    for f in all_files:
        if is_daily_query or f.stem in msg_lower:
            try:
                content = f.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- DAILY JOURNAL NOTE ({f.stem}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read daily note %s: %s", f, e)
                
    if matched_contexts:
        return "\n\n".join(matched_contexts[:2])
    return ""
