import json
import logging
import time
import uuid
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple
from types import SimpleNamespace
import httpx
import openai_auth

logger = logging.getLogger("athena.codex_transport")

class CodexError(Exception):
    """Base error for Codex transport operations."""
    pass

class AuthError(CodexError):
    """Authentication or authorization failures (401/403)."""
    pass

class RateLimitError(CodexError):
    """Rate limit exceeded (429)."""
    pass

class BadRequestError(CodexError):
    """Bad request format or invalid parameters (400)."""
    pass

class ServerError(CodexError):
    """Internal server errors (5xx)."""
    pass

def _classify_responses_issuer(
    *,
    is_xai_responses: bool = False,
    is_github_responses: bool = False,
    is_codex_backend: bool = False,
    base_url: Optional[str] = None,
) -> str:
    if is_xai_responses:
        return "xai_responses"
    if is_github_responses:
        return "github_responses"
    if is_codex_backend:
        return "codex_backend"
    if base_url:
        return f"other:{base_url}"
    return "other"

def _deterministic_call_id(fn_name: str, arguments: str, index: int = 0) -> str:
    seed = f"{fn_name}:{arguments}:{index}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"call_{digest}"

def _split_responses_tool_id(raw_id: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(raw_id, str):
        return None, None
    value = raw_id.strip()
    if not value:
        return None, None
    if "|" in value:
        parts = value.split("|", 1)
        call_id = parts[0].strip() or None
        response_item_id = parts[1].strip() or None
        return call_id, response_item_id
    if value.startswith("fc_"):
        return None, value
    return value, None

def _derive_responses_function_call_id(
    call_id: str,
    response_item_id: Optional[str] = None,
) -> str:
    if isinstance(response_item_id, str):
        candidate = response_item_id.strip()
        if candidate.startswith("fc_"):
            return candidate

    source = (call_id or "").strip()
    if source.startswith("fc_"):
        return source
    if source.startswith("call_") and len(source) > len("call_"):
        return f"fc_{source[len('call_'):]}"

    sanitized = re.sub(r"[^A-Za-z0-9_-]", "", source)
    if sanitized.startswith("fc_"):
        return sanitized
    if sanitized.startswith("call_") and len(sanitized) > len("call_"):
        return f"fc_{sanitized[len('call_'):]}"
    if sanitized:
        return f"fc_{sanitized[:48]}"

    seed = source or str(response_item_id or "") or uuid.uuid4().hex
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
    return f"fc_{digest}"

def _normalize_responses_message_status(value: Any, *, default: str = "completed") -> str:
    if isinstance(value, str):
        status = value.strip().lower().replace("-", "_").replace(" ", "_")
        if status in {"completed", "incomplete", "in_progress"}:
            return status
    return default

def _chat_content_to_responses_parts(content: Any, *, role: str = "user") -> List[Dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if not isinstance(content, list):
        return []
    converted: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                converted.append({"type": text_type, "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "").strip().lower()
        if ptype in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                converted.append({"type": text_type, "text": text})
            continue
        if ptype in {"image_url", "input_image"}:
            image_ref = part.get("image_url")
            detail = part.get("detail")
            if isinstance(image_ref, dict):
                url = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url = image_ref
            if not isinstance(url, str) or not url:
                continue
            image_part: Dict[str, Any] = {"type": "input_image", "image_url": url}
            if isinstance(detail, str) and detail.strip():
                image_part["detail"] = detail.strip()
            converted.append(image_part)
    return converted

def chat_messages_to_responses_input(
    messages: List[Dict[str, Any]],
    *,
    replay_encrypted_reasoning: bool = True,
    current_issuer_kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen_item_ids: set = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue

        if role in {"user", "assistant"}:
            content = msg.get("content", "")
            if isinstance(content, list):
                content_parts = _chat_content_to_responses_parts(content, role=role)
                text_type = "output_text" if role == "assistant" else "input_text"
                content_text = "".join(
                    p.get("text", "") for p in content_parts if p.get("type") == text_type
                )
            else:
                content_parts = []
                content_text = str(content) if content is not None else ""

            if role == "assistant":
                codex_reasoning = (
                    msg.get("codex_reasoning_items")
                    if replay_encrypted_reasoning
                    else None
                )
                has_codex_reasoning = False
                if isinstance(codex_reasoning, list):
                    for ri in codex_reasoning:
                        if isinstance(ri, dict) and ri.get("encrypted_content"):
                            item_id = ri.get("id")
                            if item_id and item_id in seen_item_ids:
                                continue
                            
                            item_issuer = ri.get("_issuer_kind")
                            if (
                                current_issuer_kind is not None
                                and item_issuer is not None
                                and item_issuer != current_issuer_kind
                            ):
                                logger.warning(
                                    "Dropping reasoning item minted by %s while "
                                    "calling %s — encrypted_content is sealed to issuer.",
                                    item_issuer, current_issuer_kind,
                                )
                                continue
                            
                            replay_item = {
                                k: v for k, v in ri.items()
                                if k not in ("id", "_issuer_kind")
                            }
                            items.append(replay_item)
                            if item_id:
                                seen_item_ids.add(item_id)
                            has_codex_reasoning = True

                codex_message_items = msg.get("codex_message_items")
                replayed_message_items = 0
                if isinstance(codex_message_items, list):
                    for raw_item in codex_message_items:
                        if not isinstance(raw_item, dict):
                            continue
                        if raw_item.get("type") != "message" or raw_item.get("role") != "assistant":
                            continue
                        raw_content_parts = raw_item.get("content")
                        if not isinstance(raw_content_parts, list):
                            continue

                        normalized_content_parts = []
                        for part in raw_content_parts:
                            if not isinstance(part, dict):
                                continue
                            part_type = str(part.get("type") or "").strip()
                            if part_type not in {"output_text", "text"}:
                                continue
                            text = part.get("text", "")
                            if text is None:
                                text = ""
                            if not isinstance(text, str):
                                text = str(text)
                            normalized_content_parts.append({"type": "output_text", "text": text})

                        if not normalized_content_parts:
                            continue

                        replay_item = {
                            "type": "message",
                            "role": "assistant",
                            "status": _normalize_responses_message_status(raw_item.get("status")),
                            "content": normalized_content_parts,
                        }
                        item_id = raw_item.get("id")
                        if isinstance(item_id, str) and item_id.strip():
                            replay_item["id"] = item_id.strip()
                        phase = raw_item.get("phase")
                        if isinstance(phase, str) and phase.strip():
                            replay_item["phase"] = phase.strip()
                        items.append(replay_item)
                        replayed_message_items += 1

                if replayed_message_items > 0:
                    pass
                elif content_parts:
                    items.append({"role": "assistant", "content": content_parts})
                elif content_text.strip():
                    items.append({"role": "assistant", "content": content_text})
                elif has_codex_reasoning:
                    items.append({"role": "assistant", "content": ""})

                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function", {})
                        fn_name = fn.get("name")
                        if not isinstance(fn_name, str) or not fn_name.strip():
                            continue

                        embedded_call_id, embedded_response_item_id = _split_responses_tool_id(
                            tc.get("id")
                        )
                        call_id = tc.get("call_id")
                        if not isinstance(call_id, str) or not call_id.strip():
                            call_id = embedded_call_id
                        if not isinstance(call_id, str) or not call_id.strip():
                            if (
                                isinstance(embedded_response_item_id, str)
                                and embedded_response_item_id.startswith("fc_")
                                and len(embedded_response_item_id) > len("fc_")
                            ):
                                call_id = f"call_{embedded_response_item_id[len('fc_'):]}"
                            else:
                                _raw_args = str(fn.get("arguments", "{}"))
                                call_id = _deterministic_call_id(fn_name, _raw_args, len(items))
                        call_id = call_id.strip()

                        arguments = fn.get("arguments", "{}")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        elif not isinstance(arguments, str):
                            arguments = str(arguments)
                        arguments = arguments.strip() or "{}"

                        items.append({
                            "type": "function_call",
                            "call_id": call_id,
                            "name": fn_name,
                            "arguments": arguments,
                        })
                continue

            if content_parts:
                items.append({"role": role, "content": content_parts})
            else:
                items.append({"role": role, "content": content_text})
            continue

        if role == "tool":
            raw_tool_call_id = msg.get("tool_call_id")
            call_id, _ = _split_responses_tool_id(raw_tool_call_id)
            if not isinstance(call_id, str) or not call_id.strip():
                if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip():
                    call_id = raw_tool_call_id.strip()
            if not isinstance(call_id, str) or not call_id.strip():
                continue

            tool_content = msg.get("content")
            output_value: Any
            if isinstance(tool_content, list):
                converted = _chat_content_to_responses_parts(
                    tool_content, role="user",
                )
                if converted:
                    output_value = converted
                else:
                    output_value = ""
            else:
                output_value = str(tool_content or "")

            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output_value,
            })

    return items

def responses_tools(tools: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None

    converted: List[Dict[str, Any]] = []
    for item in tools:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description", ""),
            "strict": False,
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return converted or None

def safe_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key, default)
    except AttributeError:
        return default

def _extract_responses_message_text(item: Any) -> str:
    content = safe_get(item, "content")
    if not isinstance(content, list):
        return ""

    chunks: List[str] = []
    for part in content:
        ptype = safe_get(part, "type")
        if ptype not in {"output_text", "text"}:
            continue
        text = safe_get(part, "text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks).strip()

def _extract_responses_reasoning_text(item: Any) -> str:
    summary = safe_get(item, "summary")
    if isinstance(summary, list):
        chunks: List[str] = []
        for part in summary:
            text = safe_get(part, "text")
            if isinstance(text, str) and text:
                chunks.append(text)
        if chunks:
            return "\n".join(chunks).strip()
    text = safe_get(item, "text")
    if isinstance(text, str) and text:
        return text.strip()
    return ""

def _format_responses_error(error_obj: Any, response_status: str) -> str:
    code: Any = None
    message: Any = None
    if isinstance(error_obj, dict):
        code = error_obj.get("code")
        message = error_obj.get("message")
    elif error_obj is not None:
        code = safe_get(error_obj, "code")
        message = safe_get(error_obj, "message")

    code_str = str(code).strip() if isinstance(code, str) else (str(code).strip() if code else "")
    message_str = str(message).strip() if isinstance(message, str) else (str(message).strip() if message else "")

    if code_str and message_str:
        return f"{code_str}: {message_str}"
    if message_str:
        return message_str
    if code_str:
        return code_str
    if error_obj:
        return str(error_obj)
    return f"Responses API returned status '{response_status}'"

class AssistantMessage:
    def __init__(self, content: str, tool_calls: List[Any], reasoning: Optional[str] = None,
                 codex_reasoning_items: Optional[List[Dict[str, Any]]] = None,
                 codex_message_items: Optional[List[Dict[str, Any]]] = None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning = reasoning
        self.reasoning_content = None
        self.reasoning_details = None
        self.codex_reasoning_items = codex_reasoning_items
        self.codex_message_items = codex_message_items

class SimpleToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.call_id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=arguments)

def _normalize_codex_response(
    response: Any,
    *,
    issuer_kind: Optional[str] = None,
) -> Tuple[AssistantMessage, str]:
    output = safe_get(response, "output")
    if not isinstance(output, list) or not output:
        out_text = safe_get(response, "output_text")
        if isinstance(out_text, str) and out_text.strip():
            output = [{
                "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": out_text.strip()}],
            }]
            if isinstance(response, dict):
                response["output"] = output
            else:
                response.output = output
        else:
            raise RuntimeError("Responses API returned no output items")

    response_status = safe_get(response, "status")
    if isinstance(response_status, str):
        response_status = response_status.strip().lower()
    else:
        response_status = None

    if response_status in {"failed", "cancelled"}:
        error_obj = safe_get(response, "error")
        error_msg = _format_responses_error(error_obj, response_status)
        raise RuntimeError(error_msg)

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    reasoning_items_raw: List[Dict[str, Any]] = []
    message_items_raw: List[Dict[str, Any]] = []
    tool_calls: List[Any] = []
    has_incomplete_items = response_status in {"queued", "in_progress", "incomplete"}
    saw_streaming_or_item_incomplete = response_status in {"queued", "in_progress"}
    saw_commentary_phase = False
    saw_final_answer_phase = False
    saw_reasoning_item = False

    for item in output:
        item_type = safe_get(item, "type")
        item_status = safe_get(item, "status")
        if isinstance(item_status, str):
            item_status = item_status.strip().lower()
        else:
            item_status = None

        if item_status in {"queued", "in_progress", "incomplete"}:
            has_incomplete_items = True
            saw_streaming_or_item_incomplete = True

        if item_type == "message":
            item_phase = safe_get(item, "phase")
            normalized_phase = None
            if isinstance(item_phase, str):
                normalized_phase = item_phase.strip().lower()
                if normalized_phase in {"commentary", "analysis"}:
                    saw_commentary_phase = True
                elif normalized_phase in {"final_answer", "final"}:
                    saw_final_answer_phase = True
            message_text = _extract_responses_message_text(item)
            if message_text:
                content_parts.append(message_text)
                raw_message_item: Dict[str, Any] = {
                    "type": "message",
                    "role": "assistant",
                    "status": _normalize_responses_message_status(item_status),
                    "content": [{"type": "output_text", "text": message_text}],
                }
                item_id = safe_get(item, "id")
                if isinstance(item_id, str) and item_id:
                    raw_message_item["id"] = item_id
                if normalized_phase:
                    raw_message_item["phase"] = normalized_phase
                message_items_raw.append(raw_message_item)
        elif item_type == "reasoning":
            saw_reasoning_item = True
            reasoning_text = _extract_responses_reasoning_text(item)
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
            encrypted = safe_get(item, "encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                raw_item = {"type": "reasoning", "encrypted_content": encrypted}
                if issuer_kind:
                    raw_item["_issuer_kind"] = issuer_kind
                item_id = safe_get(item, "id")
                if isinstance(item_id, str) and item_id.startswith("rs_tmp_"):
                    continue
                if isinstance(item_id, str) and item_id:
                    raw_item["id"] = item_id
                summary = safe_get(item, "summary")
                if isinstance(summary, list):
                    raw_summary = []
                    for part in summary:
                        text = safe_get(part, "text")
                        if isinstance(text, str):
                            raw_summary.append({"type": "summary_text", "text": text})
                    raw_item["summary"] = raw_summary
                reasoning_items_raw.append(raw_item)
        elif item_type in {"function_call", "custom_tool_call"}:
            if item_status in {"queued", "in_progress", "incomplete"}:
                continue
            fn_name = safe_get(item, "name") or ""
            if item_type == "custom_tool_call":
                arguments = safe_get(item, "input", "{}")
            else:
                arguments = safe_get(item, "arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            raw_call_id = safe_get(item, "call_id")
            raw_item_id = safe_get(item, "id")
            embedded_call_id, _ = _split_responses_tool_id(raw_item_id)
            call_id = raw_call_id if isinstance(raw_call_id, str) and raw_call_id.strip() else embedded_call_id
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = _deterministic_call_id(fn_name, arguments, len(tool_calls))
            call_id = call_id.strip()
            
            tool_calls.append(SimpleToolCall(
                call_id=call_id,
                name=fn_name,
                arguments=arguments
            ))

    final_text = "\n".join([p for p in content_parts if p]).strip()
    if not final_text and hasattr(response, "output_text"):
        out_text = getattr(response, "output_text", "")
        if isinstance(out_text, str):
            final_text = out_text.strip()
    elif not final_text and isinstance(response, dict) and "output_text" in response:
        out_text = response.get("output_text", "")
        if isinstance(out_text, str):
            final_text = out_text.strip()

    assistant_message = AssistantMessage(
        content=final_text,
        tool_calls=tool_calls,
        reasoning="\n\n".join(reasoning_parts).strip() if reasoning_parts else None,
        codex_reasoning_items=reasoning_items_raw or None,
        codex_message_items=message_items_raw or None,
    )

    if tool_calls:
        finish_reason = "tool_calls"
    elif saw_streaming_or_item_incomplete:
        finish_reason = "incomplete"
    elif (has_incomplete_items or saw_commentary_phase) and not saw_final_answer_phase:
        finish_reason = "incomplete"
    elif (reasoning_items_raw or reasoning_parts or saw_reasoning_item) and not final_text:
        finish_reason = "incomplete"
    else:
        finish_reason = "stop"
    return assistant_message, finish_reason

def _consume_raw_sse_stream(stream_lines: Any, model: str) -> dict:
    collected_output_items = []
    collected_text_deltas = []
    collected_reasoning_deltas = []
    has_tool_calls = False
    terminal_status = "completed"
    terminal_usage = None
    terminal_response_id = None
    terminal_incomplete_details = None
    terminal_error = None
    saw_terminal = False

    current_event_type = ""
    for line in stream_lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            current_event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                event_data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = current_event_type or event_data.get("type", "")
            
            if event_type == "error":
                message = event_data.get("message", "Stream emitted error event")
                raise CodexError(message)

            if "output_text.delta" in event_type or event_type == "response.output_text.delta":
                delta_text = event_data.get("delta", "")
                if delta_text:
                    collected_text_deltas.append(delta_text)
                continue

            if "function_call" in event_type:
                has_tool_calls = True

            if "reasoning" in event_type and "delta" in event_type:
                reasoning_text = event_data.get("delta", "")
                if reasoning_text:
                    collected_reasoning_deltas.append(reasoning_text)
                continue

            if event_type == "response.output_item.done":
                done_item = event_data.get("item")
                if done_item is not None:
                    collected_output_items.append(done_item)
                continue

            if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                saw_terminal = True
                resp_obj = event_data.get("response")
                if resp_obj:
                    terminal_usage = resp_obj.get("usage")
                    terminal_response_id = resp_obj.get("id")
                    terminal_status = resp_obj.get("status", terminal_status)
                    if event_type == "response.incomplete":
                        terminal_incomplete_details = resp_obj.get("incomplete_details")
                    if event_type == "response.failed":
                        terminal_error = resp_obj.get("error")
                
                if event_type == "response.completed":
                    terminal_status = "completed"
                elif event_type == "response.incomplete":
                    terminal_status = "incomplete"
                elif event_type == "response.failed":
                    terminal_status = "failed"
                break

    if collected_output_items:
        output = collected_output_items
    elif collected_text_deltas and not has_tool_calls:
        assembled = "".join(collected_text_deltas)
        output = [{
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": assembled}],
        }]
    else:
        output = []

    if not saw_terminal and not output:
        raise RuntimeError("Codex Responses stream did not emit a terminal response")

    assembled_text = "".join(collected_text_deltas)

    final = {
        "output": output,
        "output_text": assembled_text,
        "usage": terminal_usage,
        "status": terminal_status,
        "id": terminal_response_id,
        "model": model,
        "incomplete_details": terminal_incomplete_details,
        "error": terminal_error,
    }
    return final

def codex_responses_call(
    access_token: str,
    account_id: Optional[str],
    model: str,
    instructions: str,
    input_items: List[Dict[str, Any]],
    session_id: str,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
) -> Tuple[AssistantMessage, str]:
    url = "https://chatgpt.com/backend-api/codex/responses"
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": "opencode",
        "session-id": session_id,
        "User-Agent": "opencode/1.0.0 (windows; U; Windows NT 10.0; en-US)",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
        
    body: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if tools is not None:
        body["tools"] = tools
        
    logger.info("Sending POST request to %s, model=%s", url, model)
    try:
        with httpx.Client(timeout=60.0) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code in (401, 403):
                    raise AuthError(f"HTTP {resp.status_code}: {resp.read().decode('utf-8')}")
                elif resp.status_code == 429:
                    raise RateLimitError(f"HTTP 429: {resp.read().decode('utf-8')}")
                elif resp.status_code == 400:
                    raise BadRequestError(f"HTTP 400: {resp.read().decode('utf-8')}")
                elif resp.status_code >= 500:
                    raise ServerError(f"HTTP {resp.status_code}: {resp.read().decode('utf-8')}")
                elif resp.status_code != 200:
                    raise CodexError(f"HTTP {resp.status_code}: {resp.read().decode('utf-8')}")
                
                response_json = _consume_raw_sse_stream(resp.iter_lines(), model)
                issuer = _classify_responses_issuer(is_codex_backend=True)
                return _normalize_codex_response(response_json, issuer_kind=issuer)
            
    except httpx.RequestError as exc:
        raise ServerError(f"Connection error: {exc}") from exc

class CodexChatCompletions:
    def __init__(self, access_token: str, account_id: Optional[str]):
        self.access_token = access_token
        self.account_id = account_id

    def create(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> Any:
        instructions = ""
        for m in messages:
            if m.get("role") == "system":
                instructions = m.get("content", "")
                
        input_items = chat_messages_to_responses_input(messages)
        converted_tools = responses_tools(tools)
        session_id = str(uuid.uuid4())
        
        try:
            assistant_message, finish_reason = codex_responses_call(
                access_token=self.access_token,
                account_id=self.account_id,
                model=model,
                instructions=instructions,
                input_items=input_items,
                session_id=session_id,
                tools=converted_tools,
                temperature=temperature,
            )
        except AuthError as exc:
            logger.warning("AuthError during Codex call. Attempting to force-refresh token and retry: %s", exc)
            new_access, new_account = openai_auth.get_chatgpt_access_token(force_refresh=True)
            if new_access:
                self.access_token = new_access
                self.account_id = new_account
                assistant_message, finish_reason = codex_responses_call(
                    access_token=self.access_token,
                    account_id=self.account_id,
                    model=model,
                    instructions=instructions,
                    input_items=input_items,
                    session_id=session_id,
                    tools=converted_tools,
                    temperature=temperature,
                )
            else:
                raise exc
                
        class MockChoice:
            def __init__(self, message: Any, finish_reason: str):
                self.message = message
                self.finish_reason = finish_reason

        return SimpleNamespace(
            choices=[MockChoice(assistant_message, finish_reason)]
        )

class CodexChat:
    def __init__(self, access_token: str, account_id: Optional[str]):
        self.completions = CodexChatCompletions(access_token, account_id)

class CodexClient:
    def __init__(self, access_token: str, account_id: Optional[str]):
        self.chat = CodexChat(access_token, account_id)

