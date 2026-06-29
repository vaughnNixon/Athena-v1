import os
import re
import time
import logging
from datetime import datetime
from pathlib import Path
import memory_engine

logger = logging.getLogger("athena.schedule_manager")

def get_schedule_dir() -> Path:
    workspace_root = Path(__file__).parent.resolve()
    s_dir = workspace_root / "knowledge"
    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / "meetings").mkdir(parents=True, exist_ok=True)
    (s_dir / "tasks").mkdir(parents=True, exist_ok=True)
    return s_dir

def record_meeting(
    title: str, 
    date: str, 
    attendees: list = None, 
    summary: str = "", 
    decisions: list = None, 
    commitments: list = None,
    status: str = "completed"
) -> Path:
    """Records a meeting note in knowledge/meetings/ and SQLite facts."""
    if attendees is None:
        attendees = []
    if decisions is None:
        decisions = []
    if commitments is None:
        commitments = []
        
    b_dir = get_schedule_dir()
    clean_title = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    filepath = b_dir / "meetings" / f"{date}-{clean_title}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Store in SQLite machine facts
    fact_summary = f"MEETING [{date} - {title}]: {summary} (Attendees: {', '.join(attendees)})"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)
    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "meeting", 7, 0.9, now, now))
    conn.close()
    
    # 2. Materialize Markdown record
    att_formatted = "\n".join([f"- {a}" for a in attendees]) if attendees else "- None"
    dec_formatted = "\n".join([f"- {d}" for d in decisions]) if decisions else "- None"
    com_formatted = "\n".join([f"- [ ] {c}" for c in commitments]) if commitments else "- None"
    
    md_content = f"""# {date} - {title}

## Properties
- type: meeting
- date: {date}
- status: {status}

## Attendees
{att_formatted}

## Summary
{summary}

## Decisions
{dec_formatted}

## Commitments & Tasks
{com_formatted}
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded meeting note at %s", filepath)
    return filepath

def record_task(
    title: str, 
    due_date: str, 
    task_type: str = "assignment", 
    priority: str = "medium", 
    description: str = "", 
    action_items: list = None,
    status: str = "pending"
) -> Path:
    """Records a task or assignment in knowledge/tasks/ and SQLite facts."""
    if action_items is None:
        action_items = []
        
    b_dir = get_schedule_dir()
    clean_title = re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-")
    filepath = b_dir / "tasks" / f"{due_date}-{clean_title}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Store in SQLite machine facts
    fact_summary = f"TASK/ASSIGNMENT [{title}] (Due: {due_date}, Type: {task_type}, Priority: {priority}): {description}"
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    fact_hash = memory_engine.compute_fact_hash(fact_summary)
    
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fact_hash, fact_summary, "task", 8, 0.95, now, now))
    conn.close()
    
    # 2. Materialize Markdown record
    act_formatted = "\n".join([f"- [ ] {item}" for item in action_items]) if action_items else "- [ ] Complete task"
    
    md_content = f"""# {title}

## Properties
- type: {task_type}
- due_date: {due_date}
- status: {status}
- priority: {priority}

## Description
{description}

## Action Items
{act_formatted}
"""
    filepath.write_text(md_content, encoding="utf-8")
    logger.info("Recorded task/assignment at %s", filepath)
    return filepath

def get_relevant_schedule_context(user_message: str) -> str:
    """
    Scans user query for mentions of tasks, assignments, meetings, or schedule queries.
    Returns matched schedule/task context on demand. Zero token cost if no match.
    """
    s_dir = get_schedule_dir()
    all_files = list((s_dir / "meetings").glob("*.md")) + list((s_dir / "tasks").glob("*.md"))
    if not all_files:
        return ""
        
    msg_lower = user_message.lower()
    matched_contexts = []
    
    # Trigger keywords for general schedule/task queries
    general_triggers = ["task", "assignment", "meeting", "schedule", "todo", "due", "deadline", "reminder"]
    is_general_query = any(w in msg_lower for w in general_triggers)
    
    for f in all_files:
        stem_clean = f.stem.lower().replace("-", " ")
        if is_general_query or any(t for t in f.stem.split("-")[3:] if len(t) > 3 and t in msg_lower):
            try:
                content = f.read_text(encoding="utf-8").strip()
                category_label = "MEETING RECORD" if "meetings" in str(f) else "TASK / ASSIGNMENT"
                matched_contexts.append(f"--- {category_label} ({f.stem}) ---\n{content}")
            except Exception as e:
                logger.warning("Failed to read schedule file %s: %s", f, e)
                
    if matched_contexts:
        # Limit to top 3 items to preserve token efficiency
        return "\n\n".join(matched_contexts[:3])
    return ""
