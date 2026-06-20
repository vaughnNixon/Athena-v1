import os
import json
import logging
from pathlib import Path
import config
import providers
import retrieval
import distillation
import athena_compression

logger = logging.getLogger("athena.agent_loop")

class AthenaAgent:
    def __init__(self, project_id: str = "default", session_id: str = "session_1"):
        self.project_id = project_id.strip()
        self.session_id = session_id.strip()
        self.history_file = config.get_athena_home() / "sessions" / f"{self.session_id}.json"
        
        # Ensure sessions folder exists
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history = self._load_history()
        self.caveman_mode = False
        
    def _load_history(self) -> list:
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Failed to load session history: %s", exc)
        return []
        
    def _save_history(self):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error("Failed to save session history: %s", exc)

    def run_one_turn(self, user_message: str, system_message: str = None) -> str:
        """Executes a single conversational turn with Caveman & Headroom compression."""
        # 1. Retrieve memories (this lazily triggers decay updates)
        memories_block = retrieval.retrieve_relevant_memories(
            query=user_message, 
            scope_ids=[self.project_id], 
            limit=5
        )
        
        # 2. Build system prompt
        if self.caveman_mode:
            base_system = (
                "Athena v1 is a memory-first agent.\n"
                "Defining trait: 'Athena remembers what others forget.'\n"
                "Athena v1 is NOT a chatbot. It is a long-term memory layer.\n"
                "Communicate only in terse, telegraphic, sparse prose (Caveman style).\n"
                "Remove all conversational fillers, intros, and greetings."
            )
        else:
            base_system = (
                "You are Athena v1, a helpful, friendly, and natural conversational AI assistant with long-term memory capabilities.\n"
                "Your defining trait is: 'Athena remembers what others forget.'\n"
                "Use the retrieved memories injected below to seamlessly personalize the conversation and remember details the user has told you in the past."
            )
        
        if system_message:
            base_system = f"{system_message}\n{base_system}"
            
        # Dynamically inject memories
        if memories_block:
            system_prompt = f"{base_system}\n\n{memories_block}"
        else:
            system_prompt = base_system
            
        # 3. Add user message to local history
        self.history.append({"role": "user", "content": user_message})
        
        # 4. Apply compression discipline
        # A. Caveman turn history summarization (triggers if history is > 1000 tokens)
        self.history = athena_compression.run_caveman_summarization(self.history, self.project_id)
        
        # B. Headroom dynamic compression (reversibly crushes logs/JSON/RAG chunks)
        compressed_history = athena_compression.compress_history_via_headroom(self.history)
        
        # Assemble message payload
        messages = [
            {"role": "system", "content": system_prompt}
        ] + compressed_history
        
        # 5. Execute API call with rotational failover
        client, model, provider = providers.get_routing_client()
        logger.info("Executing LLM call using: Provider=%s, Model=%s", provider, model)
        
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2
            )
            msg_obj = response.choices[0].message
            raw_content = getattr(msg_obj, "content", None)
            assistant_content = raw_content.strip() if raw_content else "ACK."
            providers.record_success(provider)
            
            # Append success response to history and save
            history_entry = {"role": "assistant", "content": assistant_content}
            if getattr(msg_obj, "codex_reasoning_items", None):
                history_entry["codex_reasoning_items"] = msg_obj.codex_reasoning_items
            if getattr(msg_obj, "codex_message_items", None):
                history_entry["codex_message_items"] = msg_obj.codex_message_items
                
            self.history.append(history_entry)
            self._save_history()
            
            # 6. Trigger non-blocking distillation worker
            distillation.enqueue_distillation(
                user_msg=user_message,
                agent_msg=assistant_content,
                scope_ids=[self.project_id]
            )
            
            return assistant_content
            
        except Exception as exc:
            logger.warning("LLM execution failed for provider '%s': %s. Retrying with failover...", provider, exc)
            providers.record_failure(provider)
            
            # Failover logic: fetch next available provider in pool
            try:
                client_alt, model_alt, provider_alt = providers.get_routing_client(skip_providers=[provider])
                logger.info("Retrying LLM call using fallback: Provider=%s, Model=%s", provider_alt, model_alt)
                
                messages_alt = [
                    {"role": "system", "content": system_prompt}
                ] + compressed_history
                
                response_alt = client_alt.chat.completions.create(
                    model=model_alt,
                    messages=messages_alt,
                    temperature=0.2
                )
                msg_obj_alt = response_alt.choices[0].message
                raw_content_alt = getattr(msg_obj_alt, "content", None)
                assistant_content_alt = raw_content_alt.strip() if raw_content_alt else "ACK."
                providers.record_success(provider_alt)
                
                history_entry_alt = {"role": "assistant", "content": assistant_content_alt}
                if getattr(msg_obj_alt, "codex_reasoning_items", None):
                    history_entry_alt["codex_reasoning_items"] = msg_obj_alt.codex_reasoning_items
                if getattr(msg_obj_alt, "codex_message_items", None):
                    history_entry_alt["codex_message_items"] = msg_obj_alt.codex_message_items
                    
                self.history.append(history_entry_alt)
                self._save_history()
                
                # Trigger distillation
                distillation.enqueue_distillation(
                    user_msg=user_message,
                    agent_msg=assistant_content_alt,
                    scope_ids=[self.project_id]
                )
                
                return assistant_content_alt
                
            except Exception as exc_alt:
                logger.critical("Critical failover execution failed: %s", exc_alt)
                raise RuntimeError(f"Athena API call failed on all providers: {exc_alt}") from exc_alt
