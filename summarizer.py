import json
import logging
import re
from typing import Dict, Any, List

import config
from session_continuity import SessionContinuityLayer
from background_queue import BackgroundQueue, JobType

logger = logging.getLogger("athena.summarizer")

def run_deterministic_checks(summary: str, existing_marker: int, new_marker: int, active_topics: List[Dict[str, Any]]) -> float:
    score = 0.0
    if summary and summary.strip():
        score += 0.20
    # JSON validity checked caller side, so if summary reached here, valid parse occurred
    score += 0.20
    
    if new_marker > existing_marker:
        score += 0.20
        
    summary_lower = summary.lower()
    has_topic_match = False
    for t in active_topics:
        if t.get("topic", "").lower() in summary_lower:
            has_topic_match = True
            break
    if has_topic_match or not active_topics:
        score += 0.20
        
    if 30 <= len(summary.strip()) <= 1000:
        score += 0.10
        
    lines = [l.strip() for l in summary.strip().splitlines() if l.strip()]
    malformed = False
    for line in lines:
        if ":" not in line:
            malformed = True
            break
        k, _, _ = line.partition(":")
        if " " in k.strip():
            malformed = True
            break
    if not malformed and lines:
        score += 0.10
    else:
        score = max(0.0, score - 0.50)

    return min(1.0, round(score, 2))

def compute_hybrid_confidence(llm_confidence: float, summary: str, existing_marker: int, new_marker: int, active_topics: List[Dict[str, Any]]) -> float:
    det_score = run_deterministic_checks(summary, existing_marker, new_marker, active_topics)
    return round(0.6 * llm_confidence + 0.4 * det_score, 3)

def run_session_summary(session_id: str, project_id: str, history: List[Dict[str, Any]], from_marker: int) -> Dict[str, Any]:
    scl = SessionContinuityLayer(session_id, project_id)
    ctx = scl.get_session_context()
    existing_summary = ctx["summary"]
    new_marker = len(history)

    if new_marker <= from_marker:
        return {"summary": existing_summary, "confidence": 1.0}

    new_messages = history[from_marker:new_marker]
    raw_history_text = "\n".join([f"{m.get('role', '').upper()}: {m.get('content', '')}" for m in new_messages])

    prompt = (
        "You are Athena's internal summarizer. Output ONLY a raw JSON object.\n"
        "Format the summary as AAL structured lines (key:value, one per line).\n"
        "Valid keys: project, session, provider, skill, research, decision, next, topic, error\n"
        "Do NOT use free prose. Do NOT use spaces in key names.\n"
        "Merge with existing summary. Drop outdated lines.\n\n"
        f"Existing Summary:\n{existing_summary}\n\n"
        f"New Messages ({from_marker} to {new_marker}):\n{raw_history_text}\n\n"
        'Return JSON format: {"summary": "<AAL lines>", "confidence": 0.85}'
    )

    try:
        bq = BackgroundQueue()
        client, model, provider = bq.get_maintenance_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a JSON-only structured summarizer."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        summary_text = data.get("summary", "").strip()
        llm_conf = float(data.get("confidence", 0.8))

        hybrid_conf = compute_hybrid_confidence(llm_conf, summary_text, from_marker, new_marker, ctx["active_topics"])
        logger.info("Session summary computed for %s: hybrid_confidence=%.3f", session_id, hybrid_conf)

        if hybrid_conf < 0.7:
            logger.warning("Low hybrid confidence (%.3f) for session %s. Triggering compaction.", hybrid_conf, session_id)
            bq.enqueue(JobType.SUMMARY_COMPACTION, {
                "session_id": session_id,
                "current_summary": summary_text or existing_summary
            }, priority=2)
        else:
            pressure = scl.compute_context_pressure(history, provider)
            scl._write_session(summary_text, ctx["summary_version"] + 1, new_marker, pressure)

        return {"summary": summary_text, "confidence": hybrid_conf}
    except Exception as exc:
        logger.error("Failed to run session summary for %s: %s", session_id, exc)
        return {"summary": existing_summary, "confidence": 0.0}

def run_summary_compaction(session_id: str, current_summary: str) -> str:
    scl = SessionContinuityLayer(session_id)
    ctx = scl.get_session_context()

    prompt = (
        "You are Athena's summary compaction engine. Rewrite and tighten the following AAL summary.\n"
        "Keep only active key:value lines. Remove duplicate or conflicting keys.\n"
        "Output ONLY a raw JSON object with key 'summary'.\n\n"
        f"Current Summary:\n{current_summary}"
    )

    try:
        bq = BackgroundQueue()
        client, model, provider = bq.get_maintenance_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a JSON-only compaction engine."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        compacted = data.get("summary", "").strip()
        if compacted:
            scl._write_session(compacted, ctx["summary_version"] + 1, ctx["summary_marker"], 0.0)
            logger.info("Summary compaction completed for %s", session_id)
            return compacted
    except Exception as exc:
        logger.error("Failed to compact summary for %s: %s", session_id, exc)
    return current_summary
