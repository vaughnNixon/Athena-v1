import sqlite3
import time
import uuid
import threading
import logging
from memory_engine import get_db_connection

logger = logging.getLogger("athena.subagents")

def initialize_tasks_table():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'In Progress',
                    progress TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
    except Exception as exc:
        logger.error("Failed to create tasks table: %s", exc)
    finally:
        conn.close()

def update_task_status(task_id: str, status: str, progress: str):
    initialize_tasks_table()
    conn = get_db_connection()
    now = int(time.time())
    try:
        with conn:
            conn.execute("""
                UPDATE tasks SET status = ?, progress = ?, updated_at = ?
                WHERE task_id = ?
            """, (status, progress, now, task_id))
    except Exception as exc:
        logger.error("Failed to update task %s: %s", task_id, exc)
    finally:
        conn.close()

def get_active_tasks(project_id: str) -> list:
    initialize_tasks_table()
    conn = get_db_connection()
    tasks = []
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT task_id, role, goal, status, progress FROM tasks 
            WHERE project_id = ? AND status = 'In Progress'
        """, (project_id,))
        rows = cursor.fetchall()
        for r in rows:
            tasks.append({
                "task_id": r[0],
                "role": r[1],
                "goal": r[2],
                "status": r[3],
                "progress": r[4]
            })
    except Exception as exc:
        logger.error("Failed to fetch active tasks: %s", exc)
    finally:
        conn.close()
    return tasks

def spawn_subagent(role: str, goal: str, project_id: str, agent_class) -> str:
    """Spawns an isolated subagent worker run in a background thread."""
    initialize_tasks_table()
    task_id = str(uuid.uuid4())[:8]
    now = int(time.time())
    
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO tasks (task_id, project_id, role, goal, status, progress, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'In Progress', 'Initialized', ?, ?)
            """, (task_id, project_id, role, goal, now, now))
    except Exception as exc:
        logger.error("Failed to insert subagent task: %s", exc)
        return ""
    finally:
        conn.close()
        
    # Start thread
    thread = threading.Thread(
        target=_subagent_runner_thread, 
        args=(task_id, role, goal, project_id, agent_class),
        daemon=True
    )
    thread.start()
    return task_id

def _subagent_runner_thread(task_id: str, role: str, goal: str, project_id: str, agent_class):
    logger.info("Subagent thread started: Role=%s, TaskID=%s", role, task_id)
    update_task_status(task_id, "In Progress", "Agent woke up and starting work")
    
    try:
        # Instantiate agent
        agent = agent_class(project_id=project_id, session_id=f"sub_{task_id}")
        
        # System prompt modifier for the subagent
        sub_system_prompt = (
            f"You are the {role} subagent. Your goal: {goal}.\n"
            "Analyze project history, call tools, and deliver findings concisely.\n"
            "Strictly use sparse telegraphic caveman style response format."
        )
        
        # Run one-off execution loop
        result = agent.run_one_turn(
            user_message=f"Solve task: {goal}", 
            system_message=sub_system_prompt
        )
        
        # Register final results as reflection
        import memory_engine
        memory_engine.insert_or_reinforce_fact(
            fact=f"Subagent {role} resolved goal: '{goal}'. Finding: {result}",
            category="subagent_finding",
            importance=7,
            confidence=0.9,
            scope_ids=[project_id]
        )
        
        update_task_status(task_id, "Completed", f"Subagent findings registered. Result: {result[:50]}...")
        logger.info("Subagent thread completed: Role=%s, TaskID=%s", role, task_id)
    except Exception as exc:
        logger.error("Subagent thread %s crashed: %s", task_id, exc)
        update_task_status(task_id, "Failed", f"Subagent error: {exc}")
