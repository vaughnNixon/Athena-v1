import os
import time
import json
import logging
import sqlite3
import config
import memory_engine
import providers
import retrieval

logger = logging.getLogger("athena.learning_engine")

# Global timestamp to enforce rate limit
_last_learning_ts = 0.0

def learn_from_feedback(
    user_query: str,
    user_correction: str,
    last_retrieval_info: dict,
    prev_response_text: str
) -> dict:
    """
    Executes the Prompt 5 adaptive learning pipeline.
    
    Arguments:
        user_query: The previous query that returned the wrong answer.
        user_correction: The current user message containing correction text.
        last_retrieval_info: Info dict from the previous turn's retrieval.
        prev_response_text: The summary/content of the previous incorrect response.
        
    Returns:
        A dict with:
            - success: bool
            - explanation: str
            - useful_chunk_ids: list[int]
            - penalized_chunk_ids: list[int]
    """
    global _last_learning_ts
    
    cfg = config.load_config()
    mem_cfg = cfg.get("memory", {})
    enabled = bool(mem_cfg.get("adaptive_learning_enabled", True))
    
    if not enabled:
        return {
            "success": False,
            "explanation": "Adaptive learning is disabled in config.",
            "useful_chunk_ids": [],
            "penalized_chunk_ids": []
        }
        
    now = time.time()
    
    # 1. Anti-gaming checks (Stage 5)
    is_testing = os.environ.get("ATHENA_TESTING") == "1"
    
    if not is_testing:
        # Rate limiting: max one learning event every 5 seconds
        if now - _last_learning_ts < 5.0:
            return {
                "success": False,
                "explanation": "Rate limit: learning events must be at least 5 seconds apart.",
                "useful_chunk_ids": [],
                "penalized_chunk_ids": []
            }
            
        # Rejection of duplicate corrections within last 5 minutes
        conn = memory_engine.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_correction_text FROM feedback_log 
                WHERE timestamp > ? 
                ORDER BY feedback_id DESC LIMIT 1
            """, (int(now) - 300,))
            row = cursor.fetchone()
            if row and row[0].strip().lower() == user_correction.strip().lower():
                return {
                    "success": False,
                    "explanation": "Duplicate correction ignored.",
                    "useful_chunk_ids": [],
                    "penalized_chunk_ids": []
                }
        except Exception as exc:
            logger.warning("Failed duplicate check: %s", exc)
        finally:
            conn.close()
            
        # Rejection of stale corrections (older than 7 days)
        prev_ts = last_retrieval_info.get("timestamp", 0)
        if prev_ts > 0 and (now - prev_ts) > 7 * 24 * 3600:
            return {
                "success": False,
                "explanation": "Stale correction: previous query is older than 7 days.",
                "useful_chunk_ids": [],
                "penalized_chunk_ids": []
            }

    _last_learning_ts = now
    
    # 2. Desperation Retrieval (Stage 2)
    # Trigger desperation retrieval by appending wrong memory triggers
    desperation_query = f"incorrect memory {user_query}"
    logger.info("Running desperation retrieval for: %s", desperation_query)
    desp_result = retrieval.retrieve_memories_staged(desperation_query)
    desp_chunks = desp_result.get("supplied_chunks", [])
    desp_ids = desp_result.get("matched_chunk_ids", [])
    
    if not desp_chunks:
        explanation = "Desperation retrieval returned no candidate chunks. No skip marks updated."
        _log_feedback_and_stats(
            user_query=user_query,
            prev_response_text=prev_response_text,
            user_correction=user_correction,
            retrieval_stage=last_retrieval_info.get("retrieval_stage", "none"),
            matched_ids=last_retrieval_info.get("matched_chunk_ids", []),
            desp_ids=[],
            explanation=explanation
        )
        return {
            "success": True,
            "explanation": explanation,
            "useful_chunk_ids": [],
            "penalized_chunk_ids": []
        }
        
    # 3. Chunk Selection (Stage 3)
    # Stage A: Deterministic Candidate Ranking
    useful_ids = []
    correction_words = retrieval.get_word_set(user_correction)
    stop_words = {"a", "an", "the", "and", "or", "but", "if", "then", "else", "i", "you", "he", "she", "it", "we", "they", "my", "your", "his", "her", "its", "our", "their", "me", "him", "them", "us", "wrong", "no", "incorrect", "meant", "correction", "try", "again", "that", "this", "these", "those", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "to", "for", "of", "in", "on", "at", "by", "with", "about", "as"}
    search_terms = correction_words - stop_words
    
    threshold = float(mem_cfg.get("learning_confidence_threshold", 0.8))
    
    scored_candidates = []
    if search_terms:
        for chunk in desp_chunks:
            chunk_text = f"{chunk.get('raw_text', '')} {chunk.get('caveman_text', '')}".lower()
            chunk_words = retrieval.get_word_set(chunk_text)
            overlap = search_terms.intersection(chunk_words)
            score = len(overlap) / len(search_terms) if len(search_terms) > 0 else 0.0
            scored_candidates.append((chunk["chunk_id"], score))
            
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # If one chunk clearly exceeds threshold and has a clear gap (>= 0.2), accept immediately
        if scored_candidates and scored_candidates[0][1] >= threshold:
            if len(scored_candidates) == 1 or (scored_candidates[0][1] - scored_candidates[1][1] >= 0.2):
                useful_ids = [scored_candidates[0][0]]
                logger.info("Deterministic Stage A matched Chunk ID %d with score %.2f. Bypassing LLM call.", useful_ids[0], scored_candidates[0][1])

    # Stage B: LLM Arbitration (if deterministic ranking is ambiguous or below threshold)
    if not useful_ids:
        chunks_list_str = "\n".join([f"ID {c['chunk_id']}: {c['raw_text']}" for c in desp_chunks])
        llm_prompt = (
            "You are the adaptive learning engine for Athena v1.1.\n"
            f"The user query was: '{user_query}'\n"
            "Athena answered incorrectly.\n"
            f"The user correction is: '{user_correction}'\n"
            "Below are candidate chunks retrieved from desperation memory. Identify which chunk(s) contain the correct information to resolve the user correction.\n\n"
            f"Chunks:\n{chunks_list_str}\n\n"
            "Instructions:\n"
            "- Return the output as a valid raw JSON object with a single key 'useful_chunk_ids' containing a list of integer chunk IDs.\n"
            "- If none of the chunks are relevant, return: {\"useful_chunk_ids\": []}\n"
            "- Example: {\"useful_chunk_ids\": [3, 5]}"
        )
        
        skip_providers = []
        skip_keys = {}
        while True:
            try:
                client, model, provider = providers.get_routing_client(
                    skip_providers=skip_providers,
                    skip_keys=skip_keys
                )
            except Exception as exc:
                logger.warning("No healthy LLM provider for learning engine: %s. Using fallback.", exc)
                break
                
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a JSON-only query resolution assistant."},
                        {"role": "user", "content": llm_prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0
                )
                content = response.choices[0].message.content
                providers.record_success(provider)
                data = json.loads(content)
                useful_ids = [int(cid) for cid in data.get("useful_chunk_ids", [])]
                break
            except Exception as exc:
                logger.warning("LLM learning call failed on provider %s: %s. Retrying...", provider, exc)
                active_key = getattr(client, "key", None)
                if active_key:
                    skip_keys.setdefault(provider, []).append(active_key)
                else:
                    skip_providers.append(provider)
                
    # 4. Skip Mark Updates (Stage 3)
    # Useful chunks: decrease skip_score
    # Penalize previously matched irrelevant chunks: increase skip_score
    matched_ids = last_retrieval_info.get("matched_chunk_ids", [])
    penalized_ids = [cid for cid in matched_ids if cid not in useful_ids]
    
    conn = memory_engine.get_db_connection()
    updates = []
    query_type = retrieval.classify_query_intent(user_query)
    
    try:
        with conn:
            now_ts = int(time.time())
            
            # A. Reward useful chunks
            for cid in useful_ids:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT skip_score, feedback_count FROM skip_marks 
                    WHERE chunk_id = ? AND query_type = ?
                """, (cid, query_type))
                row = cursor.fetchone()
                if row:
                    old_score, count = row
                    new_score = max(0.0, old_score - 0.2)
                    new_count = count + 1
                    conn.execute("""
                        UPDATE skip_marks SET skip_score = ?, feedback_count = ?, last_updated_ts = ?
                        WHERE chunk_id = ? AND query_type = ?
                    """, (new_score, new_count, now_ts, cid, query_type))
                    updates.append(f"Chunk ID {cid} (useful): skip_score decreased from {old_score:.1f} to {new_score:.1f}")
                else:
                    conn.execute("""
                        INSERT INTO skip_marks (chunk_id, query_type, skip_score, feedback_count, last_updated_ts)
                        VALUES (?, ?, 0.0, 1, ?)
                    """, (cid, query_type, now_ts))
                    updates.append(f"Chunk ID {cid} (useful): initialized skip_score to 0.0")
                    
            # B. Penalize matched irrelevant chunks
            for cid in penalized_ids:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT skip_score, feedback_count FROM skip_marks 
                    WHERE chunk_id = ? AND query_type = ?
                """, (cid, query_type))
                row = cursor.fetchone()
                if row:
                    old_score, count = row
                    new_score = min(1.0, old_score + 0.2)
                    new_count = count + 1
                    conn.execute("""
                        UPDATE skip_marks SET skip_score = ?, feedback_count = ?, last_updated_ts = ?
                        WHERE chunk_id = ? AND query_type = ?
                    """, (new_score, new_count, now_ts, cid, query_type))
                    updates.append(f"Chunk ID {cid} (irrelevant): skip_score increased from {old_score:.1f} to {new_score:.1f}")
                else:
                    conn.execute("""
                        INSERT INTO skip_marks (chunk_id, query_type, skip_score, feedback_count, last_updated_ts)
                        VALUES (?, ?, 0.2, 1, ?)
                    """, (cid, query_type, now_ts))
                    updates.append(f"Chunk ID {cid} (irrelevant): initialized skip_score to 0.2")
                    
    except Exception as exc:
        logger.error("Failed to update skip marks: %s", exc)
    finally:
        conn.close()
        
    explanation = "; ".join(updates) if updates else "No skip marks were modified."
    
    # 5. Log Feedback and update Stats
    _log_feedback_and_stats(
        user_query=user_query,
        prev_response_text=prev_response_text,
        user_correction=user_correction,
        retrieval_stage=last_retrieval_info.get("retrieval_stage", "none"),
        matched_ids=matched_ids,
        desp_ids=desp_ids,
        explanation=explanation
    )
    
    return {
        "success": True,
        "explanation": explanation,
        "useful_chunk_ids": useful_ids,
        "penalized_chunk_ids": penalized_ids
    }

def _log_feedback_and_stats(
    user_query: str,
    prev_response_text: str,
    user_correction: str,
    retrieval_stage: str,
    matched_ids: list,
    desp_ids: list,
    explanation: str
):
    """Stage 1, 4 & 6: Appends feedback to DB and updates statistics."""
    conn = memory_engine.get_db_connection()
    query_type = retrieval.classify_query_intent(user_query)
    now_ts = int(time.time())
    
    try:
        with conn:
            # Stage 1 & 6: Feedback Log append
            conn.execute("""
                INSERT INTO feedback_log (
                    user_query, athena_answer_summary, user_correction_text,
                    chunks_used_in_answer, chunks_used_in_desperation, was_helpful,
                    retrieval_stage, matched_chunk_ids, desperation_chunk_ids, explanation, timestamp
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """, (
                user_query, prev_response_text, user_correction,
                json.dumps(matched_ids), json.dumps(desp_ids),
                retrieval_stage, json.dumps(matched_ids), json.dumps(desp_ids),
                explanation, now_ts
            ))
            
            # Stage 4: Update query statistics
            cursor = conn.cursor()
            cursor.execute("SELECT total_queries, corrected_queries FROM query_statistics WHERE query_type = ?", (query_type,))
            row = cursor.fetchone()
            if row:
                total, corrected = row
                new_corrected = corrected + 1
                new_accuracy = (total - new_corrected) / total if total > 0 else 1.0
                conn.execute("""
                    UPDATE query_statistics SET corrected_queries = ?, accuracy = ?, last_updated_ts = ?
                    WHERE query_type = ?
                """, (new_corrected, new_accuracy, now_ts, query_type))
            else:
                conn.execute("""
                    INSERT INTO query_statistics (query_type, total_queries, corrected_queries, accuracy, last_updated_ts)
                    VALUES (?, 1, 1, 0.0, ?)
                """, (query_type, now_ts))
                
    except Exception as exc:
        logger.error("Failed to log feedback or stats: %s", exc)
    finally:
        conn.close()

def increment_query_count(query_type: str):
    """Stage 4: Increments total queries count when a retrieval happens."""
    conn = memory_engine.get_db_connection()
    now_ts = int(time.time())
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("SELECT total_queries, corrected_queries FROM query_statistics WHERE query_type = ?", (query_type,))
            row = cursor.fetchone()
            if row:
                total, corrected = row
                new_total = total + 1
                new_accuracy = (new_total - corrected) / new_total if new_total > 0 else 1.0
                conn.execute("""
                    UPDATE query_statistics SET total_queries = ?, accuracy = ?, last_updated_ts = ?
                    WHERE query_type = ?
                """, (new_total, new_accuracy, now_ts, query_type))
            else:
                conn.execute("""
                    INSERT INTO query_statistics (query_type, total_queries, corrected_queries, accuracy, last_updated_ts)
                    VALUES (?, 1, 0, 1.0, ?)
                """, (query_type, now_ts))
    except Exception as exc:
        logger.error("Failed to increment query count: %s", exc)
    finally:
        conn.close()

def reset_skip_marks():
    """Stage 7: Resets all skip marks."""
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM skip_marks")
            logger.info("Skip marks reset completed.")
    except Exception as exc:
        logger.error("Failed to reset skip marks: %s", exc)
    finally:
        conn.close()

def reset_query_statistics():
    """Stage 7: Resets all query statistics."""
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM query_statistics")
            logger.info("Query statistics reset completed.")
    except Exception as exc:
        logger.error("Failed to reset query statistics: %s", exc)
    finally:
        conn.close()
