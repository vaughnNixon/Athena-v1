import pytest
import memory_engine
import projects_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(projects_manager, "get_projects_dir", lambda: tmp_path / "projects")
    memory_engine.initialize_db()
    yield

def test_record_project():
    path = projects_manager.record_project(
        title="Athena AI Core",
        overview="Memory-first AI assistant framework.",
        skills_used=["Python", "SQLite"],
        status="active"
    )
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Athena AI Core" in content
    assert "Memory-first AI assistant" in content

def test_get_relevant_projects_context():
    projects_manager.record_project(
        title="Wise Maxwell",
        overview="Compounding cognitive system.",
        skills_used=["Python"]
    )
    
    ctx = projects_manager.get_relevant_projects_context("Tell me about project Wise Maxwell")
    assert "PROJECT CONTEXT" in ctx
    assert "Wise Maxwell" in ctx
    
    ctx_none = projects_manager.get_relevant_projects_context("Let's cook dinner")
    assert ctx_none == ""
