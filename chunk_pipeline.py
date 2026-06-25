import re
import json
import time
import logging
import providers
import memory_engine

logger = logging.getLogger("athena.chunk_pipeline")

LLM_ENRICH_SYSTEM_PROMPT = (
    "You are a precise long-term memory enrichment engine.\n"
    "Your objective: Analyze the provided conversation chunk and return a JSON object with memory metadata.\n"
    "You must respond with a valid raw JSON object ONLY, containing precisely three keys:\n"
    "1. 'caveman_text': a compressed, telegraphic summary of the conversation chunk. Preserve names, numbers, technologies, and projects. Remove fillers, articles, and unnecessary words.\n"
    "2. 'keywords': a JSON list of 5 to 10 meaningful keywords (entities, technical terms, project names, important nouns/verbs) extracted from the text.\n"
    "3. 'annotations': a JSON object with optional keys: 'entities' (list), 'projects' (list), 'technologies' (list), 'themes' (list).\n\n"
    "Example format:\n"
    "{\n"
    "  \"caveman_text\": \"user prefer python backend. oauth rotation implemented.\",\n"
    "  \"keywords\": [\"python\", \"backend\", \"oauth\", \"rotation\"],\n"
    "  \"annotations\": {\n"
    "    \"entities\": [\"user\"],\n"
    "    \"projects\": [\"oauth rotation\"],\n"
    "    \"technologies\": [\"python\"],\n"
    "    \"themes\": [\"backend development\"]\n"
    "  }\n"
    "}"
)

def detect_sentences(text: str) -> list[str]:
    """
    Splits text into sentences, avoiding splits on decimals, URLs, version numbers,
    and common abbreviations.
    """
    if not text or not text.strip():
        return []
        
    abbreviations = {
        "e.g.", "i.e.", "vs.", "etc.", "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", 
        "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "oct.", "nov.", "dec.",
        "co.", "inc.", "ltd.", "approx.", "vs", "ca", "a.m.", "p.m."
    }
    
    sentences = []
    current_idx = 0
    pattern = re.compile(r'([.!?])(?:\s+|\Z)')
    
    for match in pattern.finditer(text):
        punctuation = match.group(1)
        end_pos = match.end()
        punc_pos = match.start(1)
        
        preceding_text = text[current_idx:punc_pos]
        
        should_split = True
        if punctuation == '.':
            # Extract last token
            last_space = max(preceding_text.rfind(' '), preceding_text.rfind('\n'), preceding_text.rfind('\t'))
            last_token = preceding_text[last_space+1:] if last_space != -1 else preceding_text
            
            token_with_dot = (last_token + punctuation).lower()
            # 1. Check abbreviation list
            if token_with_dot in abbreviations or last_token.lower() in abbreviations:
                should_split = False
            # 2. Check single letter initials (e.g. "John D. Smith")
            elif len(last_token) == 1 and last_token.isalpha() and last_token.isupper():
                should_split = False
            # 3. Check decimal/version number
            else:
                post_text = text[punc_pos+1:]
                next_char = post_text.strip()[:1]
                if last_token and last_token[-1].isdigit() and next_char.isdigit():
                    should_split = False
                    
        if should_split:
            sentence = text[current_idx:punc_pos+1].strip()
            if sentence:
                sentences.append(sentence)
            current_idx = end_pos
            
    remaining = text[current_idx:].strip()
    if remaining:
        sentences.append(remaining)
        
    return sentences

def split_long_sentence(text: str, limit: int = 16000) -> list[str]:
    """
    Splits a single extremely long sentence into pieces <= limit.
    Tries to split at semicolons, dashes, or commas (in that order of preference).
    """
    if len(text) <= limit:
        return [text]
        
    # We will search for a punctuation split point in the range [0, limit]
    # We prefer semicolons, then dashes, then commas.
    for char in [';', '-', ',']:
        idx = text[:limit].rfind(char)
        if idx != -1:
            left = text[:idx+1]
            right = text[idx+1:]
            return [left] + split_long_sentence(right, limit)
            
    # Hard split if no punctuation is found
    left = text[:limit]
    right = text[limit:]
    return [left] + split_long_sentence(right, limit)

def build_chronological_chunks(messages: list[dict], target_chunk_size: int = 16000) -> list[dict]:
    """
    Groups conversation messages chronologically into chunks of ~16,000 characters.
    Avoids splitting inside a sentence.
    """
    # 1. Split all messages into sentence units, keeping track of role and timestamp
    sentence_units = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("timestamp") or msg.get("created_at") or int(time.time())
        
        sentences = detect_sentences(content)
        for s in sentences:
            # Handle long sentences
            split_s = split_long_sentence(s, target_chunk_size)
            for part in split_s:
                sentence_units.append({
                    "role": role,
                    "text": part,
                    "ts": ts
                })
                
    if not sentence_units:
        return []
        
    chunks = []
    current_chunk_sentences = []
    
    def get_formatted_length(s_list) -> int:
        if not s_list:
            return 0
        length = 0
        current_role = None
        for s in s_list:
            role = s["role"]
            text = s["text"]
            if current_role is None:
                length += len(f"{role.capitalize()}: {text}")
            elif current_role == role:
                length += len(f" {text}")
            else:
                length += len(f"\n{role.capitalize()}: {text}")
            current_role = role
        return length

    for unit in sentence_units:
        # Check if adding this unit to the current chunk exceeds target_chunk_size
        temp_list = current_chunk_sentences + [unit]
        formatted_len = get_formatted_length(temp_list)
        
        if formatted_len <= target_chunk_size:
            current_chunk_sentences.append(unit)
        else:
            if current_chunk_sentences:
                # Close current chunk
                chunks.append(current_chunk_sentences)
                current_chunk_sentences = [unit]
            else:
                # Fallback for single huge unit
                chunks.append([unit])
                current_chunk_sentences = []
                
    if current_chunk_sentences:
        chunks.append(current_chunk_sentences)
        
    # Format the chunks into raw text and aggregate timestamps
    formatted_chunks = []
    for chunk_s in chunks:
        raw_lines = []
        current_role = None
        for s in chunk_s:
            role = s["role"]
            text = s["text"]
            role_cap = role.capitalize()
            if current_role is None:
                raw_lines.append(f"{role_cap}: {text}")
            elif current_role == role:
                raw_lines[-1] = f"{raw_lines[-1]} {text}"
            else:
                raw_lines.append(f"{role_cap}: {text}")
            current_role = role
            
        raw_text = "\n".join(raw_lines)
        start_ts = chunk_s[0]["ts"]
        end_ts = chunk_s[-1]["ts"]
        
        formatted_chunks.append({
            "raw_text": raw_text,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "char_count": len(raw_text)
        })
        
    # Merge tiny chunks (< 100 characters)
    merged_chunks = []
    for c in formatted_chunks:
        if c["char_count"] < 100:
            if merged_chunks:
                prev = merged_chunks[-1]
                prev["raw_text"] = f"{prev['raw_text']}\n{c['raw_text']}"
                prev["end_ts"] = c["end_ts"]
                prev["char_count"] = len(prev["raw_text"])
            else:
                merged_chunks.append(c)
        else:
            if merged_chunks and merged_chunks[-1]["char_count"] < 100:
                tiny = merged_chunks[-1]
                tiny["raw_text"] = f"{tiny['raw_text']}\n{c['raw_text']}"
                tiny["end_ts"] = c["end_ts"]
                tiny["char_count"] = len(tiny["raw_text"])
            else:
                merged_chunks.append(c)
                
    return merged_chunks

def deterministic_caveman(text: str) -> str:
    """
    Simple fallback routine converting conversation to telegraphic caveman memory.
    """
    words = text.split()
    fillers = {
        "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "in", "on", 
        "at", "for", "with", "by", "about", "into", "through", "during", "before", 
        "after", "above", "below", "from", "up", "down", "off", "over", "under", 
        "again", "further", "then", "once", "here", "there", "when", "where", "why", 
        "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", 
        "such", "own", "same", "so", "than", "too", "very", "can", "will", "just", 
        "should", "now", "i", "me", "my", "myself", "we", "our", "ours", "ourselves", 
        "you", "your", "yours", "yourself", "yourselves", "he", "him", "his", "himself", 
        "she", "her", "hers", "herself", "it", "its", "itself", "they", "them", "their", 
        "theirs", "themselves", "what", "which", "who", "whom", "this", "that", "these", 
        "those", "am", "been", "being", "have", "has", "had", "having", "do", "does", 
        "did", "doing", "but", "if", "or", "because", "as", "until", "while", "against"
    }
    caveman_words = []
    for w in words:
        clean_w = w.lower().strip(".,!?;:()[]{}'\"")
        if clean_w not in fillers and clean_w:
            caveman_words.append(clean_w)
    return " ".join(caveman_words) if caveman_words else text.lower()

def deterministic_keywords(text: str) -> list[str]:
    """
    Deterministic fallback to extract top keywords based on term frequency.
    """
    words = re.findall(r'\b\w{3,}\b', text.lower())
    stop_words = {
        "the", "and", "but", "for", "with", "this", "that", "these", "those", "have", "has",
        "had", "are", "was", "were", "been", "will", "would", "should", "could", "from", "out",
        "about", "into", "their", "there", "here", "them", "then", "some", "other", "than",
        "only", "very", "just", "more", "most", "about", "your", "what", "when", "where",
        "which", "user", "agent", "assistant", "system", "please", "thanks", "thank"
    }
    filtered = [w for w in words if w not in stop_words and not w.isdigit()]
    
    freq = {}
    for w in filtered:
        freq[w] = freq.get(w, 0) + 1
        
    sorted_kws = sorted(freq.keys(), key=lambda x: (-freq[x], x))
    return sorted_kws[:10]

def fallback_enrich_chunk(chunk_text: str) -> dict:
    """
    Performs deterministic/offline chunk enrichment when LLM is unavailable.
    """
    caveman = deterministic_caveman(chunk_text)
    keywords = deterministic_keywords(chunk_text)
    
    metadata = {
        "workspace": None,
        "project": None,
        "skill": None,
        "annotation": None,
        "enrichment_type": "fallback"
    }
    
    return {
        "caveman_text": caveman,
        "keywords": keywords,
        "metadata": metadata
    }

def enrich_chunk_with_llm(chunk_text: str) -> dict:
    """
    Enriches a chunk with LLM-generated caveman text, keywords, and metadata.
    Includes full provider rotation and failover logic.
    """
    skip_providers = []
    skip_keys = {}
    
    prompt = (
        "Analyze this conversation chunk and extract memory metadata. Respond only with JSON.\n\n"
        f"Conversation Chunk:\n{chunk_text}"
    )
    
    while True:
        try:
            client, model, provider = providers.get_routing_client(
                skip_providers=skip_providers,
                skip_keys=skip_keys
            )
        except Exception as exc:
            logger.warning("No healthy providers available for LLM enrichment: %s. Falling back.", exc)
            break
            
        logger.info("Enriching chunk via provider=%s model=%s", provider, model)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": LLM_ENRICH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.0
            )
            content = response.choices[0].message.content
            providers.record_success(provider)
            
            try:
                data = json.loads(content)
                caveman = data.get("caveman_text", "")
                keywords = data.get("keywords", [])
                annotations = data.get("annotations", {})
                if not caveman or not isinstance(keywords, list):
                    raise ValueError("JSON missing caveman_text or keywords list")
            except Exception as parse_exc:
                logger.warning("LLM output parsing failed: %s. Output: %r", parse_exc, content)
                raise parse_exc
                
            meta = {
                "workspace": None,
                "project": None,
                "skill": None,
                "annotation": None,
                "enrichment_type": "llm",
                "llm_provider": provider,
                "llm_model": model
            }
            if isinstance(annotations, dict):
                meta.update(annotations)
                
            return {
                "caveman_text": caveman.strip(),
                "keywords": [str(k).strip() for k in keywords if str(k).strip()],
                "metadata": meta
            }
            
        except Exception as exc:
            logger.warning("Enrichment failed on provider %s: %s. Retrying with failover...", provider, exc)
            active_key = getattr(client, "key", None)
            if active_key:
                skip_keys.setdefault(provider, []).append(active_key)
            else:
                skip_providers.append(provider)
                
    return fallback_enrich_chunk(chunk_text)

def process_conversation_to_chunks(messages: list[dict], target_chunk_size: int = 16000) -> list[int]:
    """
    Splits, merges, enriches, and stores a completed conversation as chronological database chunks.
    Returns the list of generated chunk IDs.
    """
    if not messages:
        return []
        
    memory_engine.initialize_db()
    
    raw_chunks = build_chronological_chunks(messages, target_chunk_size)
    if not raw_chunks:
        return []
        
    chunk_ids = []
    conn = memory_engine.get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            
            for chunk_data in raw_chunks:
                raw_text = chunk_data["raw_text"]
                start_ts = chunk_data["start_ts"]
                end_ts = chunk_data["end_ts"]
                char_count = chunk_data["char_count"]
                token_estimate = char_count // 4
                
                enrichment = enrich_chunk_with_llm(raw_text)
                
                caveman_text = enrichment["caveman_text"]
                keywords = enrichment["keywords"]
                metadata_dict = enrichment["metadata"]
                
                cursor.execute("SELECT IFNULL(MAX(sequence_number), 0) FROM chunks")
                next_seq = cursor.fetchone()[0] + 1
                
                now_ts = int(time.time())
                
                cursor.execute("""
                    INSERT INTO chunks (
                        sequence_number, tier, raw_text, caveman_text, start_ts, end_ts,
                        char_count, token_estimate, metadata, created_at, updated_at
                    ) VALUES (?, 'unclassified', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    next_seq, raw_text, caveman_text, start_ts, end_ts,
                    char_count, token_estimate, json.dumps(metadata_dict), now_ts, now_ts
                ))
                chunk_id = cursor.lastrowid
                chunk_ids.append(chunk_id)
                
                normalized_kws = set()
                for kw in keywords:
                    clean_kw = kw.strip().lower()
                    if clean_kw:
                        normalized_kws.add(clean_kw)
                        
                for kw in sorted(list(normalized_kws)):
                    cursor.execute("""
                        INSERT OR IGNORE INTO chunk_keywords (chunk_id, keyword)
                        VALUES (?, ?)
                    """, (chunk_id, kw))
                    
        logger.info("Successfully processed and saved %d chunks from conversation history.", len(chunk_ids))
    except Exception as exc:
        logger.error("Failed to process conversation to chunks: %s", exc)
        raise exc
    finally:
        conn.close()
        
    return chunk_ids
