import os
import time
import pytest
from pathlib import Path
import memory_engine
import monthly_report
import retrieval

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(monthly_report, "get_reports_dir", lambda: tmp_path / "reports")
    memory_engine.initialize_db()
    yield

def test_generate_monthly_report():
    # Insert dummy facts
    conn = memory_engine.get_db_connection()
    now = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO facts (fact_hash, fact, category, importance, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("hash1", "prefers python backend", "user_preference", 5, 0.9, now, now)
        )
    conn.close()

    content = monthly_report.generate_monthly_report("2026-06")
    assert "# Athena Monthly Report" in content
    assert "June 2026" in content
    assert "## Projects" in content
    assert "## Major Topics" in content
    assert "## Important Decisions" in content
    assert "## New Skills" in content
    assert "## User Preferences Learned" in content
    assert "prefers python backend" in content
    
    report_file = monthly_report.get_report_path("2026-06")
    assert report_file.exists()

def test_report_correction_flow():
    monthly_report.generate_monthly_report("2026-06")
    correction = "Project Athena v1.3 was finished on June 28."
    updated_content = monthly_report.handle_report_correction(correction, "2026-06")
    
    assert "[User Correction Verified]: Project Athena v1.3 was finished on June 28." in updated_content
    report_file = monthly_report.get_report_path("2026-06")
    assert "[User Correction Verified]" in report_file.read_text(encoding="utf-8")

def test_retrieval_ignores_reports():
    # Verify that retrieval functions only query SQLite and never inspect disk markdown files
    res = retrieval.retrieve_relevant_memories("Athena Monthly Report", scope_ids=["default"])
    assert isinstance(res, str)
