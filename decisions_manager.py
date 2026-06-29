import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine
import memory_gating

logger = logging.getLogger("athena.decisions_manager")

def get_decisions_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    decisions_dir = workspace_root / "knowledge" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    return decisions_dir

def record_decision(
    title: str, 
    decision: str, 
    why: str, 
    alternatives: list = None, 
    owner: str = "User & Athena", 
    revisit_trigger: str = "Revisit if architectural requirements or scale change significantly.", 
    ai_recall: str = "When this topic appears again, start from this decision and explain what has changed since it was made."
) -> Path:
    """
    Records a decision into SQLite (machine language) and materializes a clean Markdown record for human review.
    """
    if alternatives is None:
        alternatives = ["Status quo"]
        
    date_str = datetime.now().strftime("%Y-%m-%d")
    clean_title = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    filename = f"{date_str}-{clean_title}.md"
    filepath = get_decisions_dir() / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)

    
    # 1. Store in SQLite machine memory via facts
    fact_summary = f"DECISION [{title}]: {decision} (Why: {why})"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)

    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "important_decision", 9, 0.95, now, now))
    conn.close()
    
    # 2. Materialize Markdown record for human review
    alt_formatted = "\n".join([f"- {a}" for a in alternatives])
    md_content = f"""# {date_str} - {title}

## Properties
- type: decision
- status: decided
- owner: {owner}

## Decision
{decision}

## Why
{why}

## Alternatives Considered
{alt_formatted}

## Revisit Trigger
{revisit_trigger}

## AI Recall
{ai_recall}
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded decision at %s and updated SQLite facts.", filepath)
    return filepath

def get_relevant_decisions_context(user_message: str) -> str:
    """
    Scans user message for topics matching recorded decisions.
    Returns decision context if mentioned, zero tokens if not matched.
    """
    d_dir = get_decisions_dir()
    files = list(d_dir.glob("*.md"))
    if not files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    for f in files:
        # Check if key tokens in filename match user query
        tokens = f.stem.split("-")[3:] # Skip date parts
        if any(t for t in tokens if len(t) > 3 and t in msg_lower):
            try:
                content = f.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- RECORDED DECISION ({f.stem}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read decision file %s: %s", f, e)
                
    if matched_contexts:
        return "\n\n".join(matched_contexts)
    return ""
