import pytest
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import subagents

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    subagents.initialize_tasks_table()
    yield
    # Cleanup db
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

class MockAgent:
    def __init__(self, project_id: str, session_id: str):
        self.project_id = project_id
        self.session_id = session_id
        
    def run_one_turn(self, user_message: str, system_message: str = None) -> str:
        return "Subagent results: all checks passed."

def test_spawn_and_retrieve_tasks():
    # Spawn subagent task
    task_id = subagents.spawn_subagent(
        role="Tester",
        goal="Verify subagent thread architecture.",
        project_id="test_proj",
        agent_class=MockAgent
    )
    
    assert task_id != ""
    
    # Wait briefly for thread to complete
    time.sleep(0.5)
    
    # Verify task database state
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role, goal, status, progress FROM tasks WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    
    assert row is not None
    assert row[0] == "Tester"
    assert row[1] == "Verify subagent thread architecture."
    assert row[2] == "Completed"
    assert "Subagent results" in row[3]
    
    # Check that finding is registered in facts database as a reflection
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT fact, category, importance FROM facts WHERE category = 'subagent_finding'")
    fact_row = cursor.fetchone()
    conn.close()
    
    assert fact_row is not None
    assert "Verify subagent thread architecture" in fact_row[0]
    assert fact_row[2] == 7 # Reflect importance
