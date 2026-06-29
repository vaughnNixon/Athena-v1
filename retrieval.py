import sqlite3
import time
import json
import logging
import struct
from pathlib import Path
import config
import memory_engine
from retrieval_trace import RetrievalTrace

logger = logging.getLogger("athena.retrieval")

def apply_lazy_decay_to_fact(cursor, row_id, old_importance, decay_rate, updated_at, now):
    elapsed_seconds = now - updated_at
    if elapsed_seconds < 86400:
        return old_importance, 0
        
    decay_days = elapsed_seconds / 86400.0
    decay_amount = decay_rate * decay_days
    new_importance = max(0.0, float(old_importance) - decay_amount)
    archived = 1 if new_importance < 1.0 else 0
    new_importance_int = int(round(new_importance))
    
    cursor.execute("""
        UPDATE facts SET 
            importance = ?, 
            archived = ?, 
            updated_at = ?
        WHERE id = ?
    """, (new_importance_int, archived, now, row_id))
    return new_importance_int, archived

def get_word_set(text: str) -> set:
    normalized = memory_engine.normalize_fact_text(text)
    return set(normalized.split())

def calculate_overlap_score(query_words: set, fact_words: set) -> float:
    if not query_words:
        return 0.0
    overlap = query_words.intersection(fact_words)
    return len(overlap) / len(query_words)

def retrieve_relevant_memories(query: str, scope_ids: list, limit: int = 5) -> str:
    cfg = config.load_config()
    mem_cfg = cfg.get("memory", {})
    weights = mem_cfg.get("weights", {"keyword": 1.0, "importance": 1.0, "confidence": 1.0})
    w_k = float(weights.get("keyword", 1.0))
    w_i = float(weights.get("importance", 1.0))
    w_c = float(weights.get("confidence", 1.0))
    
    scopes = scope_ids or ["global"]
    query_words = get_word_set(query)
    now = int(time.time())
    
    conn = memory_engine.get_db_connection()
    scored_facts = []
    try:
        cursor = conn.cursor()
        # Query all non-archived facts
        cursor.execute("""
            SELECT id, fact, category, importance, confidence, decay_rate, scope_ids, mention_count, updated_at
            FROM facts WHERE archived = 0
        """)
        rows = cursor.fetchall()
        
        for row in rows:
            row_id, fact_text, category, importance, confidence, decay_rate, scopes_json, mention_count, updated_at = row
            
            # 1. Apply lazy decay check
            importance, archived = apply_lazy_decay_to_fact(
                cursor, row_id, importance, decay_rate, updated_at, now
            )
            if archived:
                continue
                
            # 2. Score
            fact_words = get_word_set(fact_text)
            kw_score = calculate_overlap_score(query_words, fact_words)
            
            # Normalise importance to 0-1 scale
            imp_norm = float(importance) / 10.0
            
            # Recency penalty (lower penalty for more recent facts)
            # Days elapsed / 30 days scale
            days_elapsed = (now - updated_at) / 86400.0
            recency_penalty = min(0.5, days_elapsed / 30.0) # Cap penalty at 0.5
            
            # Scope bonus
            try:
                fact_scopes = json.loads(scopes_json)
            except Exception:
                fact_scopes = []
            has_scope_overlap = any(s in fact_scopes for s in scopes)
            scope_bonus = 0.5 if has_scope_overlap else 0.0
            
            composite_score = (kw_score * w_k) + (imp_norm * w_i) + (confidence * w_c) - recency_penalty + scope_bonus
            
            scored_facts.append({
                "id": row_id,
                "fact": fact_text,
                "category": category,
                "confidence": confidence,
                "score": composite_score,
                "query_overlap": kw_score
            })
            
        # Commit decay updates
        conn.commit()
    except Exception as exc:
        logger.error("Failed to retrieve memories: %s", exc)
        return ""
    finally:
        conn.close()
        
    if not scored_facts:
        return ""
        
    # Sort descending by composite score
    scored_facts.sort(key=lambda x: x["score"], reverse=True)
    top_matches = scored_facts[:limit]
    
    # Self-improving query evolution: reinforce the retrieved facts
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            for match in top_matches:
                # Evolve keywords by adding query terms to the category if overlap was strong (> 0.2)
                if match["query_overlap"] > 0.2:
                    current_cat = match["category"]
                    new_keywords = [w for w in query_words if w not in get_word_set(match["fact"])][:3]
                    if new_keywords:
                        updated_cat = current_cat
                        for kw in new_keywords:
                            if kw not in current_cat.split(","):
                                updated_cat = f"{updated_cat},{kw}"
                        
                        # Increment mention, boost confidence by 0.05, and update time
                        new_conf = min(1.0, match["confidence"] + 0.05)
                        conn.execute("""
                            UPDATE facts SET 
                                category = ?, 
                                confidence = ?, 
                                mention_count = mention_count + 1, 
                                updated_at = ?
                            WHERE id = ?
                        """, (updated_cat, new_conf, now, match["id"]))
    except Exception as exc:
        logger.error("Failed to reinforce retrieved query terms: %s", exc)
    finally:
        conn.close()
        
    # Format [ATHENA MEMORY] block
    lines = ["[ATHENA MEMORY]"]
    for match in top_matches:
        lines.append(f"• {match['fact']}")
        
    formatted_block = "\n".join(lines)
    
    # Headroom check: Limit injection size to ~500 tokens (roughly 2000 chars)
    # If the block is too large, drop lower scoring facts until it fits.
    while len(formatted_block) > 2000 and len(top_matches) > 1:
        top_matches.pop()
        lines = ["[ATHENA MEMORY]"]
        for match in top_matches:
            lines.append(f"• {match['fact']}")
        formatted_block = "\n".join(lines)
        
    return formatted_block

def classify_query_intent(query: str) -> str:
    """
    Stage 0: Classifies user query intent using token rules.
    """
    q = query.lower()
    words = get_word_set(query)
    
    # Check for correction indicators
    correction_phrases = [
        "that's incorrect", "thats incorrect", "that is incorrect", "try again",
        "not what i meant", "you missed", "that's wrong", "thats wrong",
        "that is wrong", "you're wrong", "you are wrong", "not correct",
        "that's not right", "thats not right"
    ]
    correction_words = {"wrong", "incorrect"}
    if any(phrase in q for phrase in correction_phrases) or any(w in words for w in correction_words):
        return "correction"
        
    if any(w in words for w in ["prefer", "like", "dislike", "favorite", "love", "hate", "wish", "want"]):
        return "preferences"
    if any(w in words for w in ["project", "repo", "codebase", "repository", "software", "app", "application", "build"]):
        return "projects"
    if "last week" in q or any(w in words for w in ["when", "before", "after", "yesterday", "today", "tomorrow", "date", "time", "year", "month"]):
        return "timeline"
    if any(w in words for w in ["who", "name", "user", "he", "she", "person", "friend", "colleague", "nixon"]):
        return "people"
    if any(w in words for w in ["task", "todo", "done", "action", "plan", "milestone", "goal"]):
        return "tasks"
    if any(w in words for w in ["api", "database", "sqlite", "python", "javascript", "code", "error", "bug", "stack", "deploy"]):
        return "technical"
    if any(w in words for w in ["remember", "recall", "past", "history", "previous", "talked", "said", "discussed"]):
        return "past_events"
        
    return "general"

def vector_to_blob(vector: list) -> bytes:
    return struct.pack(f"{len(vector)}f", *[float(x) for x in vector])

def blob_to_vector(blob: bytes) -> list:
    return list(struct.unpack(f"{len(blob)//4}f", blob))

def cosine_similarity(v1: list, v2: list) -> float:
    if len(v1) != len(v2) or not v1 or not v2:
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = sum(a * a for a in v1) ** 0.5
    norm_v2 = sum(a * a for a in v2) ** 0.5
    if norm_v1 == 0.0 or norm_v2 == 0.0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)

def generate_embedding(text: str) -> list:
    """
    Generates embedding vector for the text using the current routed client.
    Only attempts providers that support the embeddings API.
    """
    import providers
    skip_providers = []
    skip_keys = {}
    
    # Providers known not to support /embeddings endpoint
    _NON_EMBEDDING_PROVIDERS = {"github-copilot", "groq", "nvidia"}
    
    while True:
        try:
            client, model, provider = providers.get_routing_client(
                skip_providers=skip_providers,
                skip_keys=skip_keys
            )
        except Exception as exc:
            logger.error("No providers left for embedding generation: %s", exc)
            raise exc
        
        # Skip providers that don't support embeddings
        if provider in _NON_EMBEDDING_PROVIDERS:
            skip_providers.append(provider)
            logger.info("Skipping non-embedding provider '%s'.", provider)
            continue
            
        try:
            embed_model = model
            if "gemini" in provider:
                embed_model = "text-embedding-004"
            elif "openai" in provider:
                embed_model = "text-embedding-3-small"
                
            response = client.embeddings.create(
                model=embed_model,
                input=[text]
            )
            vector = response.data[0].embedding
            providers.record_success(provider)
            return vector
        except Exception as exc:
            logger.warning("Embedding generation failed on provider %s: %s. Retrying...", provider, exc)
            active_key = getattr(client, "key", None)
            if active_key:
                skip_keys.setdefault(provider, []).append(active_key)
            else:
                skip_providers.append(provider)

def retrieve_memories_staged(query: str, scope_ids: list = None) -> dict:
    """
    Executes the staged retrieval pipeline to fetch chunks.

    Always generates a RetrievalTrace capturing the full execution record
    (stages attempted, candidate counts, confidence, timing, skip marks).
    The trace is returned under the "trace" key in the result dict and stored
    by the caller on agent.last_retrieval_trace. It has zero side effects.
    """
    fn_start = time.perf_counter()

    cfg = config.load_config()
    mem_cfg = cfg.get("memory", {})
    threshold = float(mem_cfg.get("keyword_confidence_threshold", 0.6))
    original_threshold = threshold
    emb_enabled = bool(mem_cfg.get("embedding_enabled", False))
    emb_top_k = int(mem_cfg.get("embedding_top_k", 3))
    desperation_enabled = bool(mem_cfg.get("desperation_enabled", True))

    query_words = get_word_set(query)

    # --------------- Stage 0: Classification --------------------------------
    t0 = time.perf_counter()
    intent = classify_query_intent(query)

    # Check for user error/wrong signal to force desperation mode
    wrong_terms = ["wrong", "incorrect", "not true", "fabricate", "hallucinate", "incorrect memory", "wrong memory"]
    force_desperation = any(term in query.lower() for term in wrong_terms)

    conn = memory_engine.get_db_connection()
    cursor = conn.cursor()

    # Adaptive threshold adjustment based on query statistics
    threshold_adjusted = False
    adjusted_threshold = None
    try:
        cursor.execute("SELECT accuracy FROM query_statistics WHERE query_type = ?", (intent,))
        stats_row = cursor.fetchone()
        if stats_row and stats_row[0] < 0.8:
            threshold = max(0.2, threshold - 0.1)
            threshold_adjusted = True
            adjusted_threshold = threshold
    except Exception as stats_exc:
        logger.warning("Failed to query query_statistics for threshold adjustment: %s", stats_exc)

    classification_ms = time.perf_counter() - t0

    # Build the trace object (populated inline as stages execute)
    trace = RetrievalTrace(
        query=query,
        intent=intent,
        threshold=threshold,
        threshold_adjusted=threshold_adjusted,
        adjusted_threshold=adjusted_threshold,
        force_desperation=force_desperation,
    )
    trace.record_stage_timing("classification", classification_ms)

    # --------------- Inner helper -------------------------------------------
    def score_chunks(target_tiers: list) -> tuple:
        """
        Score chunks from the given tiers against the query.

        Returns
        -------
        tuple[list[dict], dict[str, float]]
            (scored_and_sorted_chunks, skip_map)
            skip_map is returned so the caller can record which skip marks
            were applied, without re-querying the DB.
        """
        placeholders = ",".join("?" for _ in target_tiers)
        cursor.execute(f"""
            SELECT chunk_id, sequence_number, tier, raw_text, caveman_text, metadata
            FROM chunks
            WHERE tier IN ({placeholders})
        """, target_tiers)
        rows = cursor.fetchall()

        # Load skip marks for the current intent
        skip_map = {}
        try:
            cursor.execute("SELECT chunk_id, skip_score FROM skip_marks WHERE query_type = ?", (intent,))
            for cid, score in cursor.fetchall():
                skip_map[cid] = float(score)
        except Exception as skip_exc:
            logger.warning("Failed to load skip marks: %s", skip_exc)

        scored = []
        cursor.execute("SELECT IFNULL(MAX(sequence_number), 0) FROM chunks")
        max_seq = cursor.fetchone()[0]

        for row in rows:
            chunk_id, seq_num, tier, raw_text, caveman_text, metadata_json = row
            try:
                meta = json.loads(metadata_json) if metadata_json else {}
            except Exception:
                meta = {}

            cursor.execute("SELECT keyword FROM chunk_keywords WHERE chunk_id = ?", (chunk_id,))
            chunk_kws = set(r[0] for r in cursor.fetchall())
            kw_overlap = calculate_overlap_score(query_words, chunk_kws)

            meta_words = set()
            for key in ["projects", "technologies", "themes", "legacy_category"]:
                val = meta.get(key)
                if isinstance(val, list):
                    for v in val:
                        meta_words.update(get_word_set(v))
                elif isinstance(val, str):
                    meta_words.update(get_word_set(val))
            meta_score = calculate_overlap_score(query_words, meta_words)

            entities = set()
            val_ent = meta.get("entities")
            if isinstance(val_ent, list):
                for e in val_ent:
                    entities.update(get_word_set(e))
            entity_score = calculate_overlap_score(query_words, entities)

            recency_boost = 0.1 * (seq_num / max_seq) if max_seq > 0 else 0.0

            intent_boost = 0.0
            if intent in ["projects", "technical", "tasks"]:
                if meta.get("projects") or meta.get("technologies"):
                    intent_boost = 0.05

            composite_score = kw_overlap * 0.5 + meta_score * 0.2 + entity_score * 0.1 + recency_boost + intent_boost

            # Apply learning skip marks penalty: composite_score * (1.0 - skip_score)
            skip_score = skip_map.get(chunk_id, 0.0)
            penalty_factor = max(0.0, 1.0 - skip_score)
            final_score = composite_score * penalty_factor

            scored.append({
                "chunk_id": chunk_id,
                "sequence_number": seq_num,
                "tier": tier,
                "raw_text": raw_text,
                "caveman_text": caveman_text,
                "score": min(1.0, final_score)
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored, skip_map

    # --------------- Stage execution ----------------------------------------
    try:
        if not force_desperation:
            # Stage 1: Active & Mixed chunks
            t1 = time.perf_counter()
            active_results, active_skip_map = score_chunks(["active", "mixed"])
            trace.stages_attempted.append("active_search")
            trace.candidate_counts["active_search"] = len(active_results)
            trace.record_stage_timing("active_search", time.perf_counter() - t1)
            trace.record_skip_marks(active_skip_map, active_results)

            if active_results and active_results[0]["score"] >= threshold:
                matches = [r for r in active_results if r["score"] >= threshold]
                trace.finalize("active_search", matches, fn_start)
                return {
                    "matched_chunk_ids": [r["chunk_id"] for r in matches],
                    "confidence_score": active_results[0]["score"],
                    "retrieval_stage": "active_search",
                    "supplied_chunks": matches,
                    "trace": trace,
                }

            # Stage 2: Passive chunks
            t2 = time.perf_counter()
            passive_results, passive_skip_map = score_chunks(["passive"])
            trace.stages_attempted.append("passive_search")
            trace.candidate_counts["passive_search"] = len(passive_results)
            trace.record_stage_timing("passive_search", time.perf_counter() - t2)
            trace.record_skip_marks(passive_skip_map, passive_results)

            if passive_results and passive_results[0]["score"] >= threshold:
                matches = [r for r in passive_results if r["score"] >= threshold]
                trace.finalize("passive_search", matches, fn_start)
                return {
                    "matched_chunk_ids": [r["chunk_id"] for r in matches],
                    "confidence_score": passive_results[0]["score"],
                    "retrieval_stage": "passive_search",
                    "supplied_chunks": matches,
                    "trace": trace,
                }

            # Stage 3: Semantic Retrieval (cosine similarity on chunk_embeddings)
            if emb_enabled:
                try:
                    t3 = time.perf_counter()
                    query_emb = generate_embedding(query)

                    cursor.execute("SELECT chunk_id, raw_text, tier, sequence_number, caveman_text FROM chunks WHERE tier != 'unclassified'")
                    rows = cursor.fetchall()

                    semantic_matches = []
                    import providers
                    try:
                        _, active_model, active_provider = providers.get_routing_client()
                    except Exception:
                        active_model, active_provider = "default", "default"

                    for chunk_id, raw_text, tier, seq_num, caveman_text in rows:
                        cursor.execute("""
                            SELECT embedding FROM chunk_embeddings
                            WHERE chunk_id = ? AND provider = ? AND model = ?
                        """, (chunk_id, active_provider, active_model))
                        eb_row = cursor.fetchone()

                        if eb_row:
                            chunk_vector = blob_to_vector(eb_row[0])
                        else:
                            chunk_vector = generate_embedding(raw_text)
                            blob_data = vector_to_blob(chunk_vector)
                            cursor.execute("""
                                INSERT OR IGNORE INTO chunk_embeddings (chunk_id, provider, model, dimensions, embedding, created_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (chunk_id, active_provider, active_model, len(chunk_vector), blob_data, int(time.time())))
                            conn.commit()

                        sim = cosine_similarity(query_emb, chunk_vector)
                        semantic_matches.append({
                            "chunk_id": chunk_id,
                            "sequence_number": seq_num,
                            "tier": tier,
                            "raw_text": raw_text,
                            "caveman_text": caveman_text,
                            "score": sim
                        })

                    semantic_matches.sort(key=lambda x: x["score"], reverse=True)
                    trace.stages_attempted.append("semantic_search")
                    trace.candidate_counts["semantic_search"] = len(semantic_matches)
                    trace.record_stage_timing("semantic_search", time.perf_counter() - t3)

                    if semantic_matches and semantic_matches[0]["score"] >= threshold:
                        matches = [r for r in semantic_matches[:emb_top_k] if r["score"] >= threshold]
                        trace.finalize("semantic_search", matches, fn_start)
                        return {
                            "matched_chunk_ids": [r["chunk_id"] for r in matches],
                            "confidence_score": semantic_matches[0]["score"],
                            "retrieval_stage": "semantic_search",
                            "supplied_chunks": matches,
                            "trace": trace,
                        }
                except Exception as sem_exc:
                    logger.warning("Semantic retrieval execution failed: %s. Skipping to next stage.", sem_exc)

        # Stage 4: Desperation Mode
        if desperation_enabled or force_desperation:
            t4 = time.perf_counter()
            all_results, desp_skip_map = score_chunks(["active", "mixed", "passive", "unclassified"])
            trace.stages_attempted.append("desperation")
            trace.candidate_counts["desperation"] = len(all_results)
            trace.record_stage_timing("desperation", time.perf_counter() - t4)
            trace.record_skip_marks(desp_skip_map, all_results[:3])

            if all_results:
                matches = all_results[:3]
                examined_ids = [r["chunk_id"] for r in matches]
                logger.info("Desperation Mode triggered. Examined chunks: %r", examined_ids)
                trace.finalize("desperation_mode", matches, fn_start)
                return {
                    "matched_chunk_ids": examined_ids,
                    "confidence_score": all_results[0]["score"],
                    "retrieval_stage": "desperation_mode",
                    "supplied_chunks": matches,
                    "trace": trace,
                }

    except Exception as exc:
        logger.error("Staged retrieval failed: %s", exc)
    finally:
        conn.close()

    trace.finalize("none", [], fn_start)
    return {
        "matched_chunk_ids": [],
        "confidence_score": 0.0,
        "retrieval_stage": "none",
        "supplied_chunks": [],
        "message": "I couldn't find a reliable memory for that request.",
        "trace": trace,
    }

