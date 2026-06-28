import time
import pytest
from unittest.mock import MagicMock, patch

from session_continuity import SessionContinuityLayer, PROVIDER_CONTEXT_WINDOWS
import summarizer
import background_queue

@pytest.fixture
def clean_scl(tmp_path, monkeypatch):
    monkeypatch.setenv("ATHENA_HOME", str(tmp_path))
    scl = SessionContinuityLayer(session_id="test_sess_1", project_id="test_proj")
    return scl

def test_session_create_and_load(clean_scl):
    ctx = clean_scl.get_session_context()
    assert ctx["summary"] == ""
    assert ctx["summary_version"] == 0
    assert ctx["summary_marker"] == 0

def test_topic_extraction_heuristic(clean_scl):
    text = "We are discussing Python and Database indexing for Athena agent."
    topics = clean_scl._extract_topics(text)
    assert len(topics) >= 2
    assert "Python" in topics or "Database" in topics or "Athena" in topics

def test_topic_decay_respects_pinned(clean_scl, monkeypatch):
    clean_scl.update_after_turn("User asked about thesis", "Assistant talked about thesis project", 2)
    clean_scl.pin_topic("thesis")

    # Simulate 20 minutes elapsed
    with patch("time.time", return_value=time.time() + 1200):
        clean_scl._apply_topic_decay()

    topics = clean_scl.get_active_topics(top_n=10)
    thesis_t = next((t for t in topics if t["topic"].lower() == "thesis"), None)
    assert thesis_t is not None
    assert thesis_t["priority"] == "PINNED"
    assert thesis_t["status"] == "ACTIVE"

def test_topic_resurface(clean_scl):
    clean_scl.update_after_turn("Let's talk about Quantum Computing", "Quantum physics details", 2)
    # Force set to DORMANT
    from session_continuity import get_db_connection
    with get_db_connection() as conn:
        conn.execute("UPDATE topics SET status = 'DORMANT' WHERE session_id = 'test_sess_1'")
        conn.commit()

    clean_scl.update_after_turn("Back to Quantum Computing", "Quantum physics is cool", 4)
    topics = clean_scl.get_active_topics()
    qc = next((t for t in topics if "quantum" in t["topic"].lower()), None)
    assert qc is not None
    assert qc["status"] == "ACTIVE"
    assert qc["resurfaced"] == 1

def test_summary_marker_incremental(clean_scl):
    assert clean_scl.get_summary_marker() == 0
    clean_scl._write_session("project:athena", 1, 10, 0.1)
    assert clean_scl.get_summary_marker() == 10

def test_epoch_slot_write_no_overwrite(clean_scl):
    clean_scl._write_session("summary v1", 1, 5, 0.2)
    ctx1 = clean_scl.get_session_context()
    assert ctx1["summary"] == "summary v1"

    clean_scl._write_session("summary v2", 2, 10, 0.4)
    ctx2 = clean_scl.get_session_context()
    assert ctx2["summary"] == "summary v2"
    assert ctx2["summary_version"] == 2

def test_total_prompt_pressure(clean_scl):
    messages = [
        {"role": "system", "content": "System prompt " * 100},
        {"role": "user", "content": "User prompt " * 200}
    ]
    pressure = clean_scl.compute_context_pressure(messages, provider_id="groq")
    assert 0.0 < pressure <= 1.0

def test_provider_window_lookup(clean_scl):
    assert PROVIDER_CONTEXT_WINDOWS["groq"] == 32768
    assert PROVIDER_CONTEXT_WINDOWS["gemini"] == 1000000

def test_ltm_gate_skips_retrieval(clean_scl):
    clean_scl.update_after_turn("I am building Python microservices", "Python microservices design", 2)
    # Topic "Python" has high score
    should_retrieve = clean_scl.should_retrieve_long_term("tell me about Python microservices")
    assert should_retrieve is False

def test_hybrid_confidence_scoring():
    summary = "project:athena\nskill:web_search:complete"
    conf = summarizer.compute_hybrid_confidence(0.8, summary, 0, 10, [{"topic": "athena"}])
    assert 0.5 <= conf <= 1.0

def test_aal_summary_format_validation():
    malformed_summary = "this is just free prose without key value colons"
    det_score = summarizer.run_deterministic_checks(malformed_summary, 0, 10, [])
    assert det_score < 0.8

def test_session_archive_not_delete(clean_scl):
    clean_scl.archive_session()
    from session_continuity import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute("SELECT status FROM sessions WHERE session_id = 'test_sess_1'").fetchone()
        assert row["status"] == "ARCHIVED"

def test_archive_retention_delete(clean_scl, monkeypatch):
    clean_scl.archive_session()
    # Simulate retention window passed (73 hours)
    with patch("time.time", return_value=time.time() + (73 * 3600)):
        clean_scl.sweep_expired_sessions()

    from session_continuity import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute("SELECT session_id FROM sessions WHERE session_id = 'test_sess_1'").fetchone()
        assert row is None

def test_rotation_snapshot_created(clean_scl):
    snap_id = clean_scl.create_rotation_snapshot(
        trigger="quota", identity="sys", recent_context=[], provider_from="groq"
    )
    assert snap_id.startswith("snap_")
    latest = clean_scl.load_latest_snapshot()
    assert latest is not None
    assert latest["snapshot_id"] == snap_id
    assert latest["provider_from"] == "groq"

def test_new_chat_handoff(clean_scl):
    clean_scl._write_session("project:athena\nstatus:active", 3, 15, 0.1)
    exported = clean_scl.export_for_new_chat()
    assert exported["summary"] == "project:athena\nstatus:active"
    assert exported["summary_version"] == 3
    assert exported["summary_marker"] == 15

def test_maintenance_provider_fallback(monkeypatch):
    bq = background_queue.BackgroundQueue()
    with patch("providers.get_routing_client", return_value=("mock_client", "mock_model", "mock_provider")):
        client, model, prov = bq.get_maintenance_client()
        assert model == "mock_model"
        assert prov == "mock_provider"
