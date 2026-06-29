# 🏛️ Athena v1.3 — Autonomous AI Companion & Compounding Second Brain Engine

> **Defining Trait:** *"Athena remembers what others forget."*

Athena is a memory-first, autonomous AI companion, codebase architect, and compounding Second Brain executive assistant. Unlike traditional AI agents that suffer from context amnesia or burn thousands of tokens reading static files on every turn, Athena combines a high-speed machine memory database (**SQLite + AAL**) with on-demand human reporting layers.

---

## 🌟 Key Architectural Highlights

### 1. 🧠 Compounding Second Brain Engine (`second_brain.py`)
* **"Sleep" Memory Consolidation:** A background maintenance loop (triggered automatically or via `/brain`) that processes daily logs, updates entity profiles, decays old topics, and compounds knowledge over time.
* **99.5% Token Efficiency:** Uses on-demand entity routing so coding chats cost virtually zero extra memory tokens.

### 2. 🎭 Triple Persona Framework
* **`identity.md`:** Defines Athena's AI name, core roles, and architectural capabilities.
* **`soul.md`:** Defines her voice, helpful tone, core values, and celebrity filter rules.
* **`user.md`:** Stores your working style, frameworks, and developer profile.

### 3. 📂 7 Domain Knowledge Subsystems (`knowledge/`)
* 👥 **People Network (`knowledge/people/`):** Tracks direct personal relationships with smart celebrity filtering.
* 💻 **Projects Portfolio (`knowledge/projects/`):** Tracks tech stacks, skill usage, and project milestones.
* 🏛️ **Architecture Decision Records (`knowledge/decisions/`):** Records past choices, reasoning (*why*), and rejected alternatives to prevent re-debating past code decisions.
* 💼 **Business Domain (`knowledge/business/`):** Stores company profiles, market research, and business ideas separate from core code.
* 📅 **Schedule & Task Tracker (`knowledge/meetings/` & `tasks/`):** Unified tracker for meetings, deadlines, commitments, and assignments.
* 💡 **Personal Wisdom Library (`knowledge/insights/`):** Stores your favorite quotes, mental models, and personal life principles.
* 📓 **Daily Journal (`knowledge/daily/`):** Automated daily activity logs accessible via the `/daily` command.

### 4. 📊 Read-Only Monthly Reports (`reports/YYYY-MM.md`)
* Human-facing audit summaries generated on-demand (via `/report`) or at month rollover. 
* 100% read-only for Athena during normal chats to guarantee zero prompt bloat.

### 5. ⚡ 5-Stage Precision Memory Pipeline (SQLite + AAL)
* **Stage 1:** Intent & Category Classification
* **Stage 2:** Entity Domain Routing
* **Stage 3:** Active Memory Search
* **Stage 4:** Passive Memory Search
* **Stage 5:** Semantic Embedding Cached Search

---

## 🛠️ Getting Started

### Installation & Environment Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/vaughnNixon/Athena-v1.git
   cd Athena-v1
   ```

2. **Set up Python Environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure Providers:**
   Run the interactive setup wizard to configure your Gemini, OpenAI, or GitHub Copilot API credentials:
   ```bash
   python main.py setup
   ```

4. **Launch Interactive Chat Shell:**
   ```bash
   python main.py chat
   ```

---

## 💬 Interactive Slash Commands

| Command | Description |
| :--- | :--- |
| **`/brain`** | Runs the Compounding "Sleep" Memory Consolidation sweep |
| **`/daily`** | Generates and previews today's (or any date's) daily journal note |
| **`/report`** | Generates and displays your monthly audit report (`reports/YYYY-MM.md`) |
| **`/newchat`** | Archives current session and carries context over seamlessly |
| **`/topics`** | Displays active topics being tracked in the current session |
| **`/providers`** | Displays active AI model provider status and health |

---

## 🧪 Testing

Athena features comprehensive unit test coverage across all cognitive subsystems and domain managers:
```bash
python -m pytest --tb=short -q
```

---

## 🛡️ Privacy & Security Guarantees
* **Zero Secrets Tracked:** All `.env`, `config.yaml`, and API keys are strictly ignored by `.gitignore`.
* **Zero Personal Memories Tracked:** All SQLite databases (`athena_v1.db`), personal notes, and reports are excluded from public repositories.
* **Local Control:** Full user oversight on code changes and commits.
