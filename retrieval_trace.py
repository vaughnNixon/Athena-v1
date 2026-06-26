"""
retrieval_trace.py — Athena Retrieval Trace

A lightweight, immutable record of a single retrieval pipeline execution.
Captured inline during retrieve_memories_staged() and stored on the agent.

Design constraints:
  - No DB reads or writes.
  - No effect on scores, skip marks, thresholds, or learning.
  - Pure observability layer.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RetrievalTrace:
    """
    Execution record for a single call to retrieve_memories_staged().

    Fields
    ------
    query : str
        The raw user query that was passed to retrieval.
    intent : str
        The classified query intent (e.g. "projects", "technical", "general").
    threshold : float
        The confidence threshold that was active at the start of retrieval
        (after any adaptive adjustment).
    threshold_adjusted : bool
        True if the threshold was lowered automatically because query_statistics
        reported accuracy < 0.8 for the intent category.
    adjusted_threshold : float | None
        The final threshold used after adjustment. None if no adjustment occurred.
    force_desperation : bool
        True if the query contained wrong/incorrect signal that bypassed
        normal stages and went straight to desperation mode.

    stages_attempted : list[str]
        Ordered list of stage names that were actually executed, e.g.:
        ["active_search", "passive_search"]
    stage_fired : str
        The stage that produced the final result, e.g. "active_search".
        "none" if no result was found.

    candidate_counts : dict[str, int]
        Number of candidate chunks scored at each stage.
        Skipped stages are absent from this dict.
    final_confidence : float
        The confidence score of the best chunk returned.
    top_chunk_ids : list[str]
        Chunk IDs of the returned chunks, in score-descending order.
    top_chunk_scores : dict[str, float]
        chunk_id -> composite score for each returned chunk.
    skip_marks_applied : dict[str, float]
        chunk_id -> skip_score for every chunk that had a non-zero skip mark
        applied during scoring. Empty dict if no skip marks were active.

    stage_timings : dict[str, int | str]
        Per-stage wall-clock duration in milliseconds.
        Executed stages have an integer value (ms elapsed).
        Stages that were skipped have the string value "skipped".
        Keys: "classification", "active_search", "passive_search",
              "semantic_search", "desperation"
    total_duration_ms : int
        Total wall-clock time from function entry to return, in milliseconds.

    timestamp : int
        Unix timestamp (seconds) when this trace was created.
    """

    # Query context
    query: str
    intent: str
    threshold: float
    threshold_adjusted: bool
    adjusted_threshold: Optional[float]
    force_desperation: bool

    # Execution record
    stages_attempted: list = field(default_factory=list)
    stage_fired: str = "none"

    # Per-stage stats
    candidate_counts: dict = field(default_factory=dict)

    # Result details
    final_confidence: float = 0.0
    top_chunk_ids: list = field(default_factory=list)
    top_chunk_scores: dict = field(default_factory=dict)
    skip_marks_applied: dict = field(default_factory=dict)

    # Timing
    stage_timings: dict = field(default_factory=dict)
    total_duration_ms: int = 0

    # Metadata
    timestamp: int = 0

    def finalize(
        self,
        stage_fired: str,
        matched_chunks: list,
        fn_start_ns: float,
    ) -> None:
        """
        Called once at every return point in retrieve_memories_staged() to
        populate the result-level fields.

        Parameters
        ----------
        stage_fired : str
            The stage name that produced the final result.
        matched_chunks : list[dict]
            The supplied_chunks list from the result dict.
        fn_start_ns : float
            The perf_counter() value recorded at function entry (nanoseconds).
        """
        import time as _time

        self.stage_fired = stage_fired
        self.final_confidence = (
            matched_chunks[0]["score"] if matched_chunks else 0.0
        )
        self.top_chunk_ids = [c["chunk_id"] for c in matched_chunks]
        self.top_chunk_scores = {
            c["chunk_id"]: round(c["score"], 4) for c in matched_chunks
        }
        # Mark all stages not yet in stage_timings as skipped
        for stage_key in [
            "classification",
            "active_search",
            "passive_search",
            "semantic_search",
            "desperation",
        ]:
            if stage_key not in self.stage_timings:
                self.stage_timings[stage_key] = "skipped"

        elapsed_ns = _time.perf_counter() - fn_start_ns
        self.total_duration_ms = int(elapsed_ns * 1000)
        self.timestamp = int(_time.time())

    def record_stage_timing(self, stage_key: str, elapsed_ns: float) -> None:
        """Record the duration of a single stage in milliseconds."""
        self.stage_timings[stage_key] = max(1, int(elapsed_ns * 1000))

    def record_skip_marks(self, skip_map: dict, scored_chunks: list) -> None:
        """
        Record only skip marks that were actually applied to chunks that
        appeared in the scored list (skip_score > 0.0).
        """
        for chunk in scored_chunks:
            cid = chunk["chunk_id"]
            sk = skip_map.get(cid, 0.0)
            if sk > 0.0:
                self.skip_marks_applied[cid] = round(sk, 4)
