import logging
import providers

logger = logging.getLogger("athena.compression")

try:
    import headroom._core
    from headroom.transforms.content_detector import detect_content_type as python_detect

    class RustResultMock:
        def __init__(self, content_type: str, confidence: float):
            self.content_type = content_type
            self.confidence = confidence

    def mock_detect_content_type(content: str):
        res = python_detect(content)
        return RustResultMock(res.content_type.value, res.confidence)

    headroom._core.detect_content_type = mock_detect_content_type
    logger.info("Monkey-patched headroom._core.detect_content_type successfully to use Python content_detector.")
except Exception as exc:
    logger.debug("Failed to patch headroom._core.detect_content_type (possibly headroom not installed): %s", exc)

try:
    from headroom import compress as headroom_compress
    _HEADROOM_AVAILABLE = True
except ImportError:
    logger.warning("headroom-ai library not found. Falling back to built-in Headroom compression.")
    _HEADROOM_AVAILABLE = False

def compress_history_via_headroom(messages: list) -> list:
    """Wrapper that leverages headroom-ai for dynamic RAG and tool output compaction."""
    if _HEADROOM_AVAILABLE:
        try:
            res = headroom_compress(messages, kompress_model="disabled")
            if res.messages != messages:
                return res.messages
        except Exception as exc:
            logger.error("headroom-ai compression failed: %s. Falling back.", exc)
            
    # Built-in fallback: strip unneeded JSON output or long messages
    compressed = []
    for msg in messages:
        content = msg.get("content", "")
        if msg.get("role") == "tool" and len(content) > 1000:
            # Drop redundant JSON properties and collapse whitespaces
            import re
            content_cleaned = re.sub(r"\s+", " ", content)
            # Truncate very long outputs
            if len(content_cleaned) > 2000:
                content_cleaned = content_cleaned[:2000] + "... [truncated by Athena Headroom]"
            compressed.append({"role": msg["role"], "content": content_cleaned})
        else:
            compressed.append(msg)
    return compressed

def run_caveman_summarization(messages: list, project_id: str) -> list:
    """Compresses older conversation history into short, telegraphic facts when tokens > 1000."""
    # Approximate tokens (4 chars per token)
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars < 4000: # Less than ~1000 tokens
        return messages
        
    logger.info("Turns buffer reached %d chars. Triggering Caveman compression.", total_chars)
    
    # Keep the last 4 turns untouched to preserve immediate conversational context
    history_to_compress = messages[:-4]
    active_window = messages[-4:]
    
    if not history_to_compress:
        return messages
        
    # Ask the LLM to compress history
    try:
        client, model, provider = providers.get_routing_client()
    except Exception as exc:
        logger.error("Caveman summarization aborted: client resolution error: %s", exc)
        return messages
        
    raw_history_text = ""
    for m in history_to_compress:
        raw_history_text += f"{m['role'].upper()}: {m['content']}\n"
        
    prompt = (
        "You are the Caveman compression engine. "
        "Compress the following dialogue history into a single, dense, telegraphic paragraph of facts.\n"
        "Remove all greetings, conversational filler, and syntax sugar.\n"
        "Strictly write in brief, sparse, telegraphic prose.\n\n"
        "Input History:\n"
        f"{raw_history_text}\n"
        "Output Compressed Prose:"
    )
    
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a sparse text compression engine."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0
        )
        compressed_summary = response.choices[0].message.content.strip()
        providers.record_success(provider)
        
        # Inject the compressed summary turn into the active history
        new_history = [
            {"role": "system", "content": f"[ATHENA HISTORICAL SUMMARY] {compressed_summary}"}
        ] + active_window
        
        # Also, register this compressed summary as a fact in memory engine
        import memory_engine
        memory_engine.insert_or_reinforce_fact(
            fact=f"Historical summary: {compressed_summary}",
            category="history",
            importance=4,
            confidence=0.9,
            scope_ids=[project_id]
        )
        
        return new_history
    except Exception as exc:
        logger.error("Failed to run Caveman compression: %s", exc)
        providers.record_failure(provider)
        return messages
