import pytest
import memory_engine
import insights_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(insights_manager, "get_insights_dir", lambda: tmp_path / "insights")
    memory_engine.initialize_db()
    yield

def test_record_insight():
    path = insights_manager.record_insight(
        title="First Principles Thinking",
        content="Deconstruct problems down to fundamental truths.",
        category_type="framework"
    )
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "First Principles Thinking" in content
    assert "fundamental truths" in content

def test_get_relevant_insights_context():
    insights_manager.record_insight(
        title="Agile Iteration Model",
        content="Build small, verify often.",
        category_type="framework"
    )
    
    ctx = insights_manager.get_relevant_insights_context("What framework should we follow?")
    assert "PERSONAL WISDOM & FRAMEWORK" in ctx
    assert "Agile Iteration Model" in ctx
    
    ctx_none = insights_manager.get_relevant_insights_context("Let's install pytest plugins")
    assert ctx_none == ""
