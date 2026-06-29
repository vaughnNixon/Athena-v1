import pytest
import memory_engine
import schedule_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(schedule_manager, "get_schedule_dir", lambda: tmp_path / "schedule")
    memory_engine.initialize_db()
    yield

def test_record_meeting_and_task():
    m_path = schedule_manager.record_meeting(
        title="Project Sync",
        date="2026-07-01",
        attendees=["Alice", "Bob"],
        summary="Discussed roadmap.",
        decisions=["Ship v1 next week"]
    )
    assert m_path.exists()
    assert "Discussed roadmap" in m_path.read_text(encoding="utf-8")
    
    t_path = schedule_manager.record_task(
        title="Math Homework",
        due_date="2026-07-02",
        task_type="assignment",
        description="Complete calculus questions."
    )
    assert t_path.exists()
    assert "Complete calculus questions" in t_path.read_text(encoding="utf-8")

def test_get_relevant_schedule_context():
    schedule_manager.record_task(
        title="Physics Lab Report",
        due_date="2026-07-10",
        task_type="assignment",
        description="Write lab report on optics."
    )
    
    ctx = schedule_manager.get_relevant_schedule_context("What assignments do I have due?")
    assert "TASK / ASSIGNMENT" in ctx
    assert "Physics Lab Report" in ctx
    
    ctx_none = schedule_manager.get_relevant_schedule_context("Let's cook dinner")
    assert ctx_none == ""
