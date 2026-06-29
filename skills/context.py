import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass
class SkillContext:
    task_id: str
    capability: str
    artifacts_dir: Path
    services: Any                       # Reference to ServiceProvidersManager
    llm_router: Any                     # Managed access to LLM providers module
    memory_reader: Any                  # Read-only access to staged memory retrieval module
    logger: logging.Logger
    metadata: Optional[dict] = None     # Additional turn execution metadata
