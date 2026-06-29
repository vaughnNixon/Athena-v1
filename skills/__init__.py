from pathlib import Path
ATHENA_API_VERSION = 1

from .manifest import SkillManifest
from .context import SkillContext
from .policies import SkillPolicyEngine, PolicyViolationError, PERM_NETWORK_HTTP, PERM_FILESYSTEM_READ, PERM_FILESYSTEM_WRITE, PERM_SHELL_EXECUTE, PERM_STORAGE_ARTIFACTS
from .base_skill import BaseSkill
from .registry import get_registry, IncompatibleSkillError, CapabilityRegistry
from .loader import RuntimeSkillLoader

def register(skill: BaseSkill) -> None:
    get_registry().register(skill)

def get(name: str) -> BaseSkill | None:
    return get_registry().get(name)

def get_by_capability(capability: str) -> BaseSkill | None:
    return get_registry().get_by_capability(capability)

def clear() -> None:
    get_registry().clear()

def initialize_skills() -> None:
    loader = RuntimeSkillLoader()
    skills_dir = Path(__file__).parent
    loader.scan_and_load(skills_dir)

initialize_skills()
