import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine

logger = logging.getLogger("athena.insights_manager")

def get_insights_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    i_dir = workspace_root / "knowledge" / "insights"
    i_dir.mkdir(parents=True, exist_ok=True)
    return i_dir

def record_insight(title: str, content: str, category_type: str = "insight") -> Path:
    """Records a personal insight, framework, or quote into knowledge/insights/ and SQLite facts."""
    i_dir = get_insights_dir()
    clean_title = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    filepath = i_dir / f"{clean_title}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Store in SQLite machine facts
    fact_summary = f"INSIGHT [{title}] ({category_type}): {content[:200]}"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)
    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "insight", 8, 0.95, now, now))
    conn.close()
    
    # 2. Materialize Markdown record
    md_content = f"""# {title}

## Properties
- type: {category_type}
- date_added: {datetime.now().strftime("%Y-%m-%d")}

## Content & Wisdom
{content}
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded personal insight at %s", filepath)
    return filepath

def get_relevant_insights_context(user_message: str) -> str:
    """
    Scans user query for mentions of advice, principles, quotes, frameworks, or insight topics.
    Returns matched personal insight context on demand. Zero token cost if no match.
    """
    i_dir = get_insights_dir()
    all_files = list(i_dir.glob("*.md"))
    if not all_files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    triggers = ["quote", "framework", "principle", "insight", "wisdom", "advice", "model", "mindset"]
    is_insight_query = any(w in msg_lower for w in triggers)
    
    for f in all_files:
        stem_clean = f.stem.lower().replace("-", " ")
        if is_insight_query or f.stem in msg_lower or stem_clean in msg_lower:
            try:
                content = f.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- PERSONAL WISDOM & FRAMEWORK ({f.stem.title()}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read insight file %s: %s", f, e)
                
    if matched_contexts:
        return "\n\n".join(matched_contexts[:2])
    return ""
