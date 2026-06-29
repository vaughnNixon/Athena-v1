import pytest
import memory_engine
import decisions_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(decisions_manager, "get_decisions_dir", lambda: tmp_path / "decisions")
    memory_engine.initialize_db()
    yield

def test_record_decision():
    path = decisions_manager.record_decision(
        title="Use SQLite For Core Memory",
        decision="Store all memory events in SQLite database.",
        why="High performance and zero token waste.",
        alternatives=["Raw markdown memory"]
    )
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Use SQLite For Core Memory" in content
    assert "Raw markdown memory" in content
    assert "AI Recall" in content

def test_get_relevant_decisions_context():
    decisions_manager.record_decision(
        title="Local Git Push Restrictions",
        decision="Never auto-push.",
        why="User oversight."
    )
    
    ctx = decisions_manager.get_relevant_decisions_context("What is our push policy?")
    assert "RECORDED DECISION" in ctx
    assert "Local Git Push Restrictions" in ctx
    
    ctx_none = decisions_manager.get_relevant_decisions_context("Let's cook lunch")
    assert ctx_none == ""
