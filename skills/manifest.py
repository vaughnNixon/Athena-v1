from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class SkillManifest:
    name: str                           # Unique skill identifier (e.g. "web_search")
    version: str                        # Version string (e.g. "1.3.0")
    athena_api: int                     # API version requirement (e.g. 1)
    description: str                    # Human readable summary of capability
    author: str = "Athena Core"         # Author / organization
    capabilities: List[str] = field(default_factory=list)  # Namespaced capabilities (e.g. ["search.web"])
    permissions: List[str] = field(default_factory=list)   # Requested permissions (e.g. ["permission.network.http"])
    dependencies: List[str] = field(default_factory=list)  # Python module or binary dependencies

    def validate(self, current_api_version: int):
        if self.athena_api != current_api_version:
            from .registry import IncompatibleSkillError
            raise IncompatibleSkillError(
                f"Skill '{self.name}' requires athena_api={self.athena_api}, "
                f"but running Athena instance is api={current_api_version}."
            )
        if not self.name or not self.capabilities:
            raise ValueError(f"Skill manifest for '{self.name}' must specify a valid name and at least one capability.")
