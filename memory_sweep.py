import json
import time
import logging
import config
import memory_engine

logger = logging.getLogger("athena.memory_sweep")

class ScoringPolicy:
    """
    Abstract base class for chunk lifecycle scoring policies.
    """
    def score_chunk(self, chunk: dict) -> float:
        raise NotImplementedError("ScoringPolicy must implement score_chunk()")

class ChronologicalScoringPolicy(ScoringPolicy):
    """
    Scoring Policy Version 1:
    Scores chunks purely by sequence number, prioritizing newer chunks.
    """
    def score_chunk(self, chunk: dict) -> float:
        # Default chronological score is the sequence number
        return float(chunk.get("sequence_number", 0))

def run_memory_sweep(policy: ScoringPolicy = None):
    """
    Idempotent memory sweep engine (Lifecycle Engine Version 1).
    Uses a pluggable ScoringPolicy to score and prioritize memory chunks.
    
    1. Fetches all chunks from the database.
    2. Runs each chunk through the ScoringPolicy to compute its score.
    3. Sorts chunks by score descending (highest priority/newest first).
    4. Traverses sorted chunks, accumulating token_estimate until budget is reached.
    5. Assigns tiers:
       - 'active': Chunks fully inside the budget.
       - 'mixed': The single chunk that spans/crosses the budget limit. Contains boundary annotation.
       - 'passive': Chunks fully outside/older than the budget.
    6. Commits updates inside a transaction (only if tier or annotations changed).
    """
    logger.info("Starting memory lifecycle sweep...")
    if policy is None:
        policy = ChronologicalScoringPolicy()
        
    cfg = config.load_config()
    mem_cfg = cfg.get("memory", {})
    active_token_budget = mem_cfg.get("active_token_budget", 50000)
    
    memory_engine.initialize_db()
    conn = memory_engine.get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chunk_id, sequence_number, tier, token_estimate, metadata 
            FROM chunks
        """)
        rows = cursor.fetchall()
        
        if not rows:
            logger.info("No chunks found in database. Sweep complete.")
            return
            
        # Map rows to dictionary list
        chunks = []
        for row in rows:
            chunk_id, seq_num, tier, token_est, metadata_json = row
            try:
                meta = json.loads(metadata_json) if metadata_json else {}
            except Exception:
                meta = {}
            chunks.append({
                "chunk_id": chunk_id,
                "sequence_number": seq_num,
                "tier": tier,
                "token_estimate": token_est,
                "metadata": meta,
                "metadata_raw": metadata_json
            })
            
        # 1. Score each chunk using the active policy
        for c in chunks:
            c["score"] = policy.score_chunk(c)
            
        # 2. Sort chunks by score descending (highest priority first)
        # For ChronologicalScoringPolicy, this sorts newest chunks first.
        # Tie-breaker: sequence number descending
        chunks.sort(key=lambda x: (x["score"], x["sequence_number"]), reverse=True)
        
        # 3. Partition chunks based on token budget
        accumulated_tokens = 0
        updates = []
        
        for c in chunks:
            chunk_id = c["chunk_id"]
            current_tier = c["tier"]
            token_est = c["token_estimate"]
            meta = c["metadata"]
            metadata_raw = c["metadata_raw"]
            
            # Ensure standard reserved fields exist
            for key in ["workspace", "project", "skill", "annotation"]:
                if key not in meta:
                    meta[key] = None
                    
            target_tier = None
            target_annotation = meta.get("annotation")
            
            if accumulated_tokens < active_token_budget:
                if accumulated_tokens + token_est <= active_token_budget:
                    target_tier = "active"
                    accumulated_tokens += token_est
                    # Clear boundary annotation if it was mixed
                    if current_tier == "mixed":
                        target_annotation = None
                else:
                    # Boundary chunk that spans the limit
                    target_tier = "mixed"
                    accumulated_tokens += token_est
                    target_annotation = (
                        f"Spans Active/Passive boundary (accumulated tokens: {accumulated_tokens} / "
                        f"budget: {active_token_budget})."
                    )
            else:
                target_tier = "passive"
                if current_tier == "mixed":
                    target_annotation = None
                    
            meta["annotation"] = target_annotation
            updated_meta_json = json.dumps(meta)
            
            # Idempotency check: only update if tier or metadata annotation changed
            if current_tier != target_tier or metadata_raw != updated_meta_json:
                updates.append((target_tier, updated_meta_json, chunk_id))
                
        # 4. Commit updates inside a single transaction
        if updates:
            with conn:
                cursor.executemany("""
                    UPDATE chunks 
                    SET tier = ?, metadata = ?, updated_at = ? 
                    WHERE chunk_id = ?
                """, [(t, m, int(time.time()), cid) for t, m, cid in updates])
            logger.info("Lifecycle sweep complete. Updated %d chunks to maintain Active budget.", len(updates))
        else:
            logger.info("Lifecycle sweep complete. No memory updates needed (idempotent).")
            
    except Exception as exc:
        logger.error("Memory sweep failed: %s", exc)
        raise exc
    finally:
        conn.close()
