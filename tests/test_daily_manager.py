import pytest
import memory_engine
import daily_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(daily_manager, "get_daily_dir", lambda: tmp_path / "daily")
    memory_engine.initialize_db()
    yield

def test_generate_daily_note():
    content = daily_manager.generate_daily_note("2026-06-29")
    assert "# 2026-06-29" in content
    assert "## Daily Summary" in content
    assert "## Decisions / Signals" in content
    assert "## Open Loops" in content
    assert "## People Mentioned" in content
    assert "## Projects Touched" in content
    
    path = daily_manager.get_daily_dir() / "2026-06-29.md"
    assert path.exists()

def test_get_relevant_daily_context():
    daily_manager.generate_daily_note("2026-06-29")
    ctx = daily_manager.get_relevant_daily_context("What did we do today in our daily log?")
    assert "DAILY JOURNAL NOTE" in ctx
    assert "2026-06-29" in ctx
    
    ctx_none = daily_manager.get_relevant_daily_context("Let's write some rust code")
    assert ctx_none == ""
