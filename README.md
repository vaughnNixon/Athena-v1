# Athena v1.2 — Long-Term Memory Dialogue Agent with Subagent System

Athena is an intelligent, memory-first dialogue agent designed to run across multiple inference providers while maintaining persistent long-term memory, context window compression, and a fully extensible subagent execution layer with centralised memory gating.

---

## ── Architecture Overview ──

Athena has two parallel execution paths in `run_one_turn()`:

### Path A — Subagent Task Execution
For tasks that require a specialised skill (web search, code execution, writing, file reading), Athena routes through the subagent system before any conversational LLM call.

```
User Message
    ↓
Task Planner  ← queries Athena's memory for prior context first
    ↓
Determine Skill (Stage A: deterministic rules / Stage B: LLM fallback)
    ↓
Spawn Generic Stateless Worker
    ↓
Worker loads Skill → executes task
    ↓
SubagentResult { user_output, aal_summary, memory_payload, artifacts }
    ↓
Memory Gating  ← filters on outcome, confidence, length, deduplication
    ↓
chunk_pipeline.process_memory_payload()  ← approved items only
    ↓
Long-Term Memory (SQLite Chunks)
```

### Path B — Conversational LLM with Tool-Controlled Memory Retrieval
For standard conversation turns, Athena uses LLM-controlled tool calling to retrieve memory only when the model decides it needs past context.

```mermaid
graph TD
    User([User Message]) --> SystemPrompt[1. Assemble system prompt & style toggle]
    SystemPrompt --> History[2. Load history & apply headroom compression]
    History --> Route{3. Call LLM with retrieve_memories tool}

    Route -- LLM decides memory NOT needed --> Response[4a. Respond immediately]
    Route -- LLM calls retrieve_memories --> Retrieve[4b. Query chunk_keywords & chunks]

    Retrieve --> ToolResponse[5. Append memory context to conversation]
    ToolResponse --> FinalCall[6. Call LLM again with memory context]
    FinalCall --> Response

    Response --> Save[7. Save to history & trigger async Chunk Ingestion]
    Save --> DB[(SQLite Chunks & Keywords)]
    Save --> Out([Assistant Response])
```

---

## ── Running Tests ──

To run the entire hermetic test suite (**78 tests**) inside the virtual environment:

```powershell
.venv\Scripts\python.exe -m pytest
```

---

## ── Core Features ──

### 1. Subagent Execution System (v1.2)

- **SubagentResult Contract**: Every skill must return a 4-part structured object — `user_output` (what the user sees), `aal_summary` (execution handoff dict with outcome/confidence/notes), `memory_payload` (knowledge for long-term storage), and `artifacts` (files, reports, CSVs on disk).
- **Task Planner (`task_planner.py`)**: Queries Athena's memory for prior context first, then maps user messages to skills using deterministic keyword rules (Stage A) or an LLM call (Stage B). Returns `None` for conversational turns so they bypass the subagent path entirely.
- **Generic Stateless Worker (`worker.py`)**: A crash-proof execution container that loads registered skills dynamically. Converts unhandled exceptions into structured failure results instead of crashing.
- **Versioned Skill Registry (`skills/`)**: Each skill must declare a `name`, `version`, and `athena_api` integer. Skills with `athena_api != ATHENA_API_VERSION` are rejected at registration time with a clear `IncompatibleSkillError`, not at execution time.
- **Memory Gating (`memory_gating.py`)**: Athena's filter layer between worker output and long-term storage:
  - Rejects the entire payload if `outcome == "failed"` or `confidence < 0.3`
  - Drops empty strings and items shorter than 20 characters
  - Deduplicates against existing database facts (SHA-256 hash check) and chunks (text match)
  - Only accepted items enter `chunk_pipeline.process_memory_payload()`

### 2. Universal Provider Manager with Multi-Key Rotation

- **Schema & Persistence (`providers.json`)**: Supports registering custom OpenAI-compatible endpoints without hardcoding.
- **API Key Rotation**: Rotates to the next API key per provider on any failure.
- **Auto-Failover**: Switches to the next healthiest provider when all keys for one provider are exhausted.
- **Health Tracking & Self-Healing**: Tracks request stats per key, automatically resets on full failure to avoid permanent lockouts.

### 3. Next-Generation Chunk Memory Architecture

- **Intelligent Chunk Generation**: Segments conversations into chronological chunks. LLM enriches them with Caveman summaries, keywords, themes, and entities. Falls back to deterministic local extraction when all providers are offline.
- **Active/Passive Lifecycle Sweep**: Enforces a configurable `active_token_budget` (default `50,000` tokens). Demotes older chunks to `passive` tier chronologically. Annotates budget-boundary chunks as `mixed`.
- **Staged Retrieval Engine**:
  - **Stage 0** — Intent Classifier (rules-based whole-word matching)
  - **Stage 1 & 2** — Active & Passive keyword overlap search (sub-millisecond indexed)
  - **Stage 3** — Semantic cosine similarity search with cached `chunk_embeddings` table
  - **Stage 4 & 5** — Desperation mode + non-hallucinatory safe fallback

### 4. Adaptive Learning Engine

- Detects user corrections and routes through the learning pipeline.
- Two-stage chunk selection: deterministic Stage A ranking first, LLM arbitration (Stage B) for ambiguous cases.
- Applies skip mark penalties to penalise bad retrieval chunks and rewards useful ones.
- Maintains per-intent accuracy statistics and auto-adjusts retrieval thresholds for low-accuracy query types.

### 5. Context Compression & Style Switcher

- **Presentation Switcher (`/caveman`)**: Toggles between natural dialogue and sparse caveman prose without splitting session history.
- **Headroom AI Compression**: Compresses long tool outputs and history using fast native token-crushers.

### 6. Interactive CLI Command Shell

- **Slash Commands**:
  - `/providers` — Display all configured providers, keys, health, and routing stats
  - `/provider add/remove/enable/disable/select` — Manage provider configuration
  - `/model select` — Override active model
  - `/caveman` — Toggle presentation style
  - `/trace` — Display the full retrieval trace of the last memory query (stages, timings, chunks, skip marks)
  - `/subagent` — Display the last subagent execution summary (task, skill, outcome, memory gating results, artifacts)
  - `/rollback` / `/learning` — Reset or inspect adaptive learning skip marks and accuracy stats
  - `/quit` / `/exit` — Clean session exit

---

## ── Onboarding & Setup ──

1. **Onboard Providers**:
   ```powershell
   .venv\Scripts\python.exe main.py onboard
   ```

2. **Start Chatting**:
   ```powershell
   .venv\Scripts\python.exe main.py chat
   ```

3. **Check System Diagnostics**:
   ```powershell
   .venv\Scripts\python.exe main.py doctor
   ```

4. **Manual Memory Sweep**:
   ```powershell
   .venv\Scripts\python.exe main.py sweep
   ```

---

## ── Skill Development ──

To add a skill, create a class that extends `BaseSkill` in the `skills/` directory:

```python
from skills.base_skill import BaseSkill
from subagent_result import SubagentResult

class MySkill(BaseSkill):
    name = "my_skill"
    version = "1.0"
    athena_api = 1          # Must match ATHENA_API_VERSION = 1
    description = "Does something useful"

    def run(self, task: str, memory_context: str) -> SubagentResult:
        # ... do the work ...
        return SubagentResult(
            user_output="Done.",
            aal_summary={"task": task, "skill_used": self.name, "outcome": "success", "confidence": 0.95, "notes": ""},
            memory_payload=["key observation about this task"],
            artifacts=[]
        )
```

Then register it:
```python
import skills
from my_skill_module import MySkill
skills.register(MySkill())
```

Skills with a mismatched `athena_api` version are rejected at registration with a clear error — they will never silently fail at execution time.
