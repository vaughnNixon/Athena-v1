import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine

logger = logging.getLogger("athena.business_manager")

def get_business_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    b_dir = workspace_root / "knowledge" / "business"
    b_dir.mkdir(parents=True, exist_ok=True)
    (b_dir / "companies").mkdir(parents=True, exist_ok=True)
    (b_dir / "ideas").mkdir(parents=True, exist_ok=True)
    (b_dir / "decisions").mkdir(parents=True, exist_ok=True)
    return b_dir

def record_company(
    name: str, 
    industry: str, 
    summary: str, 
    why_it_matters: str, 
    relationship: str = "Potential vendor / partner",
    status: str = "active"
) -> Path:
    """Records a company profile in knowledge/business/companies/ and SQLite facts."""
    b_dir = get_business_dir()
    clean_name = re.sub(r"[^\w\s-]", "", name).strip().lower().replace(" ", "-")
    filepath = b_dir / "companies" / f"{clean_name}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Store in SQLite machine facts
    fact_summary = f"COMPANY [{name}] ({industry}): {summary} (Why it matters: {why_it_matters})"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)
    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "business", 7, 0.9, now, now))
    conn.close()
    
    # 2. Materialize Markdown record
    md_content = f"""# {name}

## Properties
- type: company
- industry: {industry}
- status: {status}

## Company Summary
{summary}

## Why This Company Matters
{why_it_matters}

## Current Relationship
{relationship}

## AI Recall
When asked about this company, return:
1. What it is
2. Why it matters
3. Related projects or decisions
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded company profile at %s", filepath)
    return filepath

def get_relevant_business_context(user_message: str) -> str:
    """
    Scans user query for mentions of tracked business items (companies, ideas, business decisions).
    Returns matched context on demand. Zero token cost if no match.
    """
    b_dir = get_business_dir()
    all_files = list(b_dir.rglob("*.md"))
    if not all_files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    for f in all_files:
        stem_clean = f.stem.lower().replace("-", " ")
        # Match if filename stem or clean title is in user message
        if re.search(rf"\b{re.escape(f.stem.lower())}\b", msg_lower) or stem_clean in msg_lower:
            try:
                content = f.read_text(encoding="utf-8").strip()
                matched_contexts.append(f"--- BUSINESS CONTEXT ({f.stem}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read business file %s: %s", f, e)
                
    if matched_contexts:
        return "\n\n".join(matched_contexts)
    return ""
