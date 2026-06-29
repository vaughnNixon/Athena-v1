import logging
from typing import Dict, Optional, List
from .base_skill import BaseSkill
from .manifest import SkillManifest

logger = logging.getLogger("athena.skills.registry")

ATHENA_API_VERSION = 1

class IncompatibleSkillError(Exception):
    """Raised when a skill's athena_api version does not match ATHENA_API_VERSION."""
    pass

class CapabilityRegistry:
    def __init__(self, api_version: int = ATHENA_API_VERSION):
        self.api_version = api_version
        self._skills: Dict[str, BaseSkill] = {}
        self._capability_index: Dict[str, BaseSkill] = {}
        self._skill_stats: Dict[str, dict] = {}

    def register(self, skill: BaseSkill) -> None:
        manifest = getattr(skill, "manifest", None)
        if not manifest:
            skill.__init__()
            manifest = skill.manifest

        manifest.validate(self.api_version)

        self._skills[manifest.name] = skill
        for cap in manifest.capabilities:
            self._capability_index[cap] = skill

        if manifest.name not in self._skill_stats:
            self._skill_stats[manifest.name] = {
                "executions": 0,
                "successes": 0,
                "failures": 0,
                "last_used": None
            }
        logger.info("Registered skill '%s' (v%s) with capabilities: %s", manifest.name, manifest.version, manifest.capabilities)

    def get(self, name: str) -> Optional[BaseSkill]:
        return self._skills.get(name)

    def get_by_capability(self, capability: str) -> Optional[BaseSkill]:
        return self._capability_index.get(capability) or self._skills.get(capability)

    def list_skills(self) -> List[SkillManifest]:
        return [s.manifest for s in self._skills.values()]

    def record_execution(self, name: str, success: bool):
        if name in self._skill_stats:
            self._skill_stats[name]["executions"] += 1
            if success:
                self._skill_stats[name]["successes"] += 1
            else:
                self._skill_stats[name]["failures"] += 1

    def clear(self) -> None:
        self._skills.clear()
        self._capability_index.clear()
        self._skill_stats.clear()

# Global Registry Instance
_global_registry = CapabilityRegistry()

def get_registry() -> CapabilityRegistry:
    return _global_registry
