import pytest
import os
import tempfile
import json
import time
from unittest.mock import MagicMock, patch

# Override home dir for hermetic testing
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import distillation

@pytest.fixture(autouse=True)
def setup_teardown():
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    # Cleanup db
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_run_distillation_success():
    # Setup mock API client response returning distilled facts in JSON format
    mock_client = MagicMock()
    mock_choice = MagicMock()
    # Mocking returning a JSON formatted facts structure
    mock_choice.message.content = json.dumps({
        "facts": [
            "Nixon prefers Python for backend projects.",
            "Database engine is SQLite."
        ]
    })
    mock_client.chat.completions.create.return_value.choices = [mock_choice]
    
    with patch("providers.get_routing_client", return_value=(mock_client, "gemini-3-flash", "gemini")):
        distillation._run_distillation(
            user_msg="I prefer using Python for backend work, since I am Nixon.",
            agent_msg="I will use SQLite for Athena database engine.",
            scope_ids=["test_proj"]
        )
        
    # Verify both facts are stored in the SQLite DB
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT fact, category, importance FROM facts ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) == 2
    assert rows[0][0] == "Nixon prefers Python for backend projects."
    assert rows[1][0] == "Database engine is SQLite."
    assert rows[0][1] == "general"
    assert rows[0][2] == 5 # Default importance for distilled facts
