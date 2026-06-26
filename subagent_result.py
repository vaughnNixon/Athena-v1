from dataclasses import dataclass

@dataclass
class SubagentResult:
    user_output: str          # What the user sees
    aal_summary: dict         # Compact structured handoff to Athena
    memory_payload: list[str] # Candidate knowledge for long-term storage
    artifacts: list[dict]     # Files, reports, CSVs — stored by path, not embedded

# Future design note (for documentation & backward compatibility planning only):
# @dataclass
# class MemoryItem:
#     text: str
#     importance: float        # 0.0–1.0, worker's estimated importance
#     confidence: float        # 0.0–1.0, worker's confidence in the observation
#     category: str            # suggested memory category (e.g. "technical", "project")
#     source: str              # what produced this item (skill name + task summary)
#
# If MemoryItem is implemented in the future, memory_payload can support list[MemoryItem | str]
# where any str element is wrapped automatically with default values:
# MemoryItem(text=item, importance=0.5, confidence=0.5, category="general", source=aal_summary.get("skill_used", "unknown"))
