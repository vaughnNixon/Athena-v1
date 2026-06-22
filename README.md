# Athena v1 — Long-Term Memory Dialogue Agent

Athena v1 is an intelligent, memory-first dialogue agent layer designed to run across multiple inference providers while maintaining persistent long-term memory, context window compression discipline, and proper keyless ChatGPT Pro/Plus OAuth integration.

---

## ── Architectural Turn Flow ──

```mermaid
graph TD
    User([User Message]) --> Retrieve[1. Retrieve memories from SQLite DB]
    Retrieve --> Prompt[2. Build natural chat system prompt]
    Prompt --> Compress[3. Compress history via Headroom & Caveman]
    Compress --> Route{4. Providers Router}
    
    Route -->|OAuth| Codex[5A. Codex Client /responses]
    Route -->|API Key| Standard[5B. OpenAI-compatible /chat/completions]
    
    Codex --> SSE[6. Consume SSE Event Stream & Replay Reasoning]
    Standard --> NormalCall[7. Parse standard completion response]
    
    SSE --> Save[8. Append response to history & trigger asynchronous Fact Distillation]
    NormalCall --> Save
    
    Save --> DB[(SQLite Memory Store)]
    Save --> Out([Assistant Response])
```

---

## ── Core Features ──

### 1. Keyless ChatGPT Pro/Plus OAuth Integration
- **Direct Responses API Support**: Bypasses standard `/v1/chat/completions` and maps chat messages directly into Codex-compatible `/responses` input items.
- **Raw SSE Event Stream Parser**: Enforces `stream: true` in requests and consumes line-by-line Server-Sent Events (SSE). Extracts content deltas and item done states dynamically.
- **Reasoning Item Replay**: Tracks opaque, encrypted reasoning blocks (`codex_reasoning_items`) and re-injects them on subsequent turns to maintain chain-of-thought coherence.
- **Cross-Issuer Filters**: Strips reasoning blobs minted by different endpoints (e.g. xAI vs. Codex) if the session swaps providers, preventing `invalid_encrypted_content` HTTP 400 errors.

### 2. Universal Provider Manager with Multi-Key Rotation
- **Schema & Persistence (`providers.json`)**: Configured dynamically; supports registering custom OpenAI-compatible endpoints (Grok, OpenRouter, Together, DeepInfra) without hardcoding.
- **API Key Rotation**: Automatically rotates to the next API key inside a provider upon any failure (rate limits, timeouts, auth errors, quota exceeded).
- **Auto-Failover**: Automatically switches to the next healthiest provider in the fallback chain if all keys for a provider are exhausted.
- **Health Tracking**: Tracks request success/failure stats and consecutive failure rates per key and provider dynamically.
- **Self-Healing Statistics**: Automatically resets all failure counts as a last resort if all configured options fail, avoiding permanent lockouts from transient outages.

### 3. SQLite Memory Engine
- Persists extracted facts and reinforces them on user keyword matches.
- Implements a lazy temporal decay formula ($importance = initial\_importance \times e^{-decay\_rate \times elapsed\_turns}$) upon retrieval.
- Prevents database duplication using SHA-256 signature tracking.

### 4. Context Window Compression Discipline
- Caveman turned history summarization: Triggers automatically when conversational history exceeds 1000 tokens, condensing past turns into sparse, telegraphic prose.
- Headroom AI transforms: Compasses tool outputs and long logs using fast, native compiled token-crushers.

### 5. Interactive Setup & CLI Command Shell
- **Interactive Wizard (`main.py onboard`)**: Walks you through configuring default providers, API keys, or logging into browser/headless OAuth sessions.
- **Diagnostics (`main.py doctor`)**: Validates folder structures, permissions, and database health metrics.
- **Slash Commands**:
  - `/providers`: Display a formatted Rich table of all registered providers, defaults, key counts, enabled status, active provider, and request metrics.
  - `/provider add`: Interactive wizard to register a new provider (name, type, base URL, default model, and multiple keys).
  - `/provider remove <id>`: Deletes a provider from the configuration.
  - `/provider enable/disable <id>`: Dynamically toggles a provider's active eligibility status.
  - `/provider select <id|auto>` (shortcut: `/provider <id>`): Manual active override or resets to health-based selection (`auto`).
  - `/model select <model_id|default>` (shortcut: `/model <model_id>`): Manual model override or resets to provider defaults.
  - `/caveman`: Toggle between caveman sparse prose style and natural conversational style.
  - `/quit` / `/exit`: Cleanly exit the session.

---

## ── Onboarding & Setup ──

1. **Onboard Providers**:
   ```powershell
   .venv\Scripts\python.exe main.py onboard
   ```
   Select your preferred provider and select authentication method `browser` or `headless` to log in keylessly.

2. **Start Chatting**:
   ```powershell
   .venv\Scripts\python.exe main.py chat
   ```

3. **Check System Diagnostics**:
   ```powershell
   .venv\Scripts\python.exe main.py doctor
   ```
