import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine

logger = logging.getLogger("athena.projects_manager")

def get_projects_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    p_dir = workspace_root / "knowledge" / "projects"
    p_dir.mkdir(parents=True, exist_ok=True)
    return p_dir

def record_project(
    title: str, 
    overview: str, 
    skills_used: list = None, 
    status: str = "active", 
    milestones: list = None
) -> Path:
    """Records a project profile in knowledge/projects/ and SQLite facts."""
    if skills_used is None:
        skills_used = []
    if milestones is None:
        milestones = []
        
    p_dir = get_projects_dir()
    clean_title = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    filepath = p_dir / f"{clean_title}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Store in SQLite machine facts
    fact_summary = f"PROJECT [{title}] (Status: {status}): {overview} (Skills: {', '.join(skills_used)})"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)
    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "project_update", 8, 0.95, now, now))
    conn.close()
    
    # 2. Materialize Markdown record
    skills_formatted = "\n".join([f"- {s}" for s in skills_used]) if skills_used else "- None"
    ms_formatted = "\n".join([f"- {m}" for m in milestones]) if milestones else "- Initialized project tracking."
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    md_content = f"""# Project: {title}

- Created: {date_str}
- Last Updated: {date_str}
- Status: {status}

## Overview
{overview}

## Skills & Tech Stack Used
{skills_formatted}

## Walkthrough & Key Milestones
{ms_formatted}
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded project profile at %s", filepath)
    return filepath

def get_relevant_projects_context(user_message: str) -> str:
    """
    Scans user query for mentions of tracked projects.
    Returns matched project context on demand. Zero token cost if no match.
    """
    p_dir = get_projects_dir()
    all_files = list(p_dir.glob("*.md"))
    if not all_files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    for f in all_files:
        stem_clean = f.stem.lower().replace("-", " ")
        if f.stem in msg_lower or stem_clean in msg_lower or "project" in msg_lower:
            try:
                content = f.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- PROJECT CONTEXT ({f.stem.title()}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read project file %s: %s", f, e)
                
    if matched_contexts:
        return "\n\n".join(matched_contexts[:2])
    return ""
