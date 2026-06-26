"""
tests/test_retrieval_trace.py

Unit tests for the Retrieval Trace observability system.

Tests verify:
  1. Trace is always returned in the result dict.
  2. stage_fired in trace matches retrieval_stage in result.
  3. stages_attempted reflects the actual execution order.
  4. skip_marks_applied correctly records non-zero skip marks.
  5. threshold_adjusted and adjusted_threshold are set when query_statistics triggers a drop.
  6. stage_timings has integer values for attempted stages and "skipped" for others.
  7. Running traced retrieval has zero DB side effects.
"""

import pytest
import os
import tempfile
import time
import json
from unittest.mock import patch

# ── Hermetic environment ──────────────────────────────────────────────────────
temp_dir = tempfile.mkdtemp()
os.environ["ATHENA_HOME"] = temp_dir

import config
import memory_engine
import retrieval
from retrieval_trace import RetrievalTrace

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    """Reinitialise DB before every test, remove it after."""
    config.ensure_athena_dirs()
    memory_engine.initialize_db()
    yield
    db_path = memory_engine.get_db_path()
    if db_path.exists():
        try:
            os.remove(db_path)
        except Exception:
            pass


def _insert_chunk(cursor, conn, seq, tier, raw_text, caveman_text,
                  keywords=None, metadata=None):
    """Helper: insert a chunk and its keywords, returns chunk_id."""
    now = int(time.time())
    meta_json = json.dumps(metadata or {})
    cursor.execute("""
        INSERT INTO chunks
            (sequence_number, tier, raw_text, caveman_text,
             start_ts, end_ts, char_count, token_estimate,
             metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (seq, tier, raw_text, caveman_text,
          now, now + 1, len(raw_text), len(raw_text) // 4,
          meta_json, now, now))
    chunk_id = cursor.lastrowid
    for kw in (keywords or []):
        cursor.execute(
            "INSERT OR IGNORE INTO chunk_keywords (chunk_id, keyword) VALUES (?, ?)",
            (chunk_id, kw)
        )
    conn.commit()
    return chunk_id


# ── Test 1: trace is always returned ─────────────────────────────────────────

def test_trace_always_returned():
    """The 'trace' key must be present in every result dict — even with no chunks."""
    result = retrieval.retrieve_memories_staged("something completely unknown")
    assert "trace" in result, "Expected 'trace' key in result dict"
    assert isinstance(result["trace"], RetrievalTrace), \
        "Expected trace to be a RetrievalTrace instance"


# ── Test 2: stage_fired matches retrieval_stage ───────────────────────────────

def test_trace_stage_fired_matches_result():
    """trace.stage_fired must match result['retrieval_stage']."""
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()
    _insert_chunk(
        cursor, conn, seq=1, tier="active",
        raw_text="Nixon prefers Python over JavaScript.",
        caveman_text="Nixon prefer python",
        keywords=["nixon", "prefer", "python", "javascript"],
        metadata={"projects": ["python"]}
    )
    conn.close()

    result = retrieval.retrieve_memories_staged("what does Nixon prefer")
    trace = result["trace"]

    # The stage recorded in trace must match the top-level key
    assert trace.stage_fired == result["retrieval_stage"], (
        f"trace.stage_fired={trace.stage_fired!r} "
        f"!= result['retrieval_stage']={result['retrieval_stage']!r}"
    )


# ── Test 3: stages_attempted order ───────────────────────────────────────────

def test_trace_stages_attempted_order():
    """
    With no chunks at all, retrieval falls through active → passive → desperation.
    stages_attempted must include those three in that order.
    """
    result = retrieval.retrieve_memories_staged("tell me about the project")
    trace = result["trace"]

    # At minimum active and passive should have been attempted
    assert "active_search" in trace.stages_attempted
    assert "passive_search" in trace.stages_attempted

    active_idx = trace.stages_attempted.index("active_search")
    passive_idx = trace.stages_attempted.index("passive_search")
    assert active_idx < passive_idx, \
        "active_search should be attempted before passive_search"


# ── Test 4: skip marks recorded correctly ─────────────────────────────────────

def test_trace_skip_marks_recorded():
    """
    When a chunk has a skip mark > 0 in the DB for the matched intent,
    it should appear in trace.skip_marks_applied.
    """
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()

    chunk_id = _insert_chunk(
        cursor, conn, seq=1, tier="active",
        raw_text="Nixon is working on Athena memory engine.",
        caveman_text="nixon athena memory",
        keywords=["nixon", "athena", "memory", "engine"],
    )
    conn.close()

    # Determine query intent and insert a skip mark for that chunk
    query = "what is Nixon building"
    intent = retrieval.classify_query_intent(query)

    conn2 = memory_engine.get_db_connection()
    conn2.execute(
        """INSERT OR REPLACE INTO skip_marks
               (chunk_id, query_type, skip_score, feedback_count, last_updated_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (chunk_id, intent, 0.4, 1, int(time.time()))
    )
    conn2.commit()
    conn2.close()

    result = retrieval.retrieve_memories_staged(query)
    trace = result["trace"]

    # The chunk had skip_score=0.4, so it must appear in skip_marks_applied
    assert str(chunk_id) in trace.skip_marks_applied or chunk_id in trace.skip_marks_applied, \
        f"Expected chunk_id {chunk_id} in skip_marks_applied, got: {trace.skip_marks_applied}"


# ── Test 5: threshold adjustment recorded ────────────────────────────────────

def test_trace_threshold_adjustment_recorded():
    """
    When query_statistics reports accuracy < 0.8 for the intent,
    trace.threshold_adjusted must be True and adjusted_threshold < default.
    """
    query = "what python projects did we start"
    intent = retrieval.classify_query_intent(query)

    # Insert a low-accuracy stats record
    conn = memory_engine.get_db_connection()
    conn.execute(
        """INSERT OR REPLACE INTO query_statistics
               (query_type, total_queries, corrected_queries, accuracy, last_updated_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (intent, 10, 4, 0.6, int(time.time()))   # accuracy < 0.8
    )
    conn.commit()
    conn.close()

    result = retrieval.retrieve_memories_staged(query)
    trace = result["trace"]

    assert trace.threshold_adjusted is True, \
        "Expected threshold_adjusted=True when query_statistics accuracy < 0.8"
    assert trace.adjusted_threshold is not None, \
        "Expected adjusted_threshold to be set"

    cfg = config.load_config()
    default_threshold = float(cfg.get("memory", {}).get("keyword_confidence_threshold", 0.6))
    assert trace.adjusted_threshold < default_threshold, \
        f"adjusted_threshold ({trace.adjusted_threshold}) should be below default ({default_threshold})"


# ── Test 6: timing fields are populated ──────────────────────────────────────

def test_trace_timing_fields_present():
    """
    stage_timings must have integer values for stages that ran
    and the string "skipped" for stages that were bypassed.
    total_duration_ms must be a positive integer.
    """
    result = retrieval.retrieve_memories_staged("hello there")
    trace = result["trace"]

    # classification always runs
    assert trace.stage_timings.get("classification") not in (None, "skipped"), \
        "classification should always have a timing value"
    assert isinstance(trace.stage_timings["classification"], int), \
        "classification timing should be an int (ms)"
    assert trace.stage_timings["classification"] >= 1

    # Any stage marked as skipped must have the string "skipped"
    all_stage_keys = {"classification", "active_search", "passive_search",
                      "semantic_search", "desperation"}
    for key in all_stage_keys:
        val = trace.stage_timings.get(key)
        assert val is not None, f"stage_timings missing key '{key}'"
        assert isinstance(val, (int, str)), \
            f"stage_timings['{key}'] should be int or 'skipped', got {type(val)}"
        if isinstance(val, str):
            assert val == "skipped", \
                f"Only acceptable string value is 'skipped', got '{val}'"

    # Total duration must be a positive integer
    assert isinstance(trace.total_duration_ms, int)
    assert trace.total_duration_ms >= 1, \
        f"total_duration_ms should be >= 1, got {trace.total_duration_ms}"


# ── Test 7: no DB side effects ────────────────────────────────────────────────

def test_trace_no_side_effects():
    """
    Running retrieve_memories_staged (which always generates a trace) must not
    modify any rows in skip_marks, query_statistics, facts, or chunks.
    """
    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()

    # Insert a chunk and a skip mark baseline
    chunk_id = _insert_chunk(
        cursor, conn, seq=1, tier="active",
        raw_text="Athena stores long-term memories in SQLite.",
        caveman_text="athena stores memories sqlite",
        keywords=["athena", "memory", "sqlite"],
    )

    query = "how does memory storage work"
    intent = retrieval.classify_query_intent(query)
    conn.execute(
        """INSERT OR REPLACE INTO skip_marks
               (chunk_id, query_type, skip_score, feedback_count, last_updated_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (chunk_id, intent, 0.2, 1, int(time.time()))
    )
    conn.execute(
        """INSERT OR REPLACE INTO query_statistics
               (query_type, total_queries, corrected_queries, accuracy, last_updated_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (intent, 5, 1, 0.8, int(time.time()))
    )
    conn.commit()

    # Snapshot before
    cursor.execute("SELECT skip_score, feedback_count FROM skip_marks WHERE chunk_id = ?", (chunk_id,))
    before_skip = cursor.fetchone()
    cursor.execute("SELECT total_queries, corrected_queries FROM query_statistics WHERE query_type = ?", (intent,))
    before_stats = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM chunks")
    before_chunk_count = cursor.fetchone()[0]
    conn.close()

    # Run retrieval (trace is generated automatically)
    retrieval.retrieve_memories_staged(query)

    # Snapshot after
    conn2 = memory_engine.get_db_connection()
    cursor2 = conn2.cursor()
    cursor2.execute("SELECT skip_score, feedback_count FROM skip_marks WHERE chunk_id = ?", (chunk_id,))
    after_skip = cursor2.fetchone()
    cursor2.execute("SELECT total_queries, corrected_queries FROM query_statistics WHERE query_type = ?", (intent,))
    after_stats = cursor2.fetchone()
    cursor2.execute("SELECT COUNT(*) FROM chunks")
    after_chunk_count = cursor2.fetchone()[0]
    conn2.close()

    assert before_skip == after_skip, \
        f"skip_marks was modified by trace: {before_skip} → {after_skip}"
    assert before_stats == after_stats, \
        f"query_statistics was modified by trace: {before_stats} → {after_stats}"
    assert before_chunk_count == after_chunk_count, \
        f"chunks table was modified by trace: {before_chunk_count} → {after_chunk_count}"
