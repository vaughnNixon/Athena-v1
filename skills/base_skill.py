from typing import Optional
from subagent_result import SubagentResult
from .manifest import SkillManifest
from .context import SkillContext

class BaseSkill:
    manifest: SkillManifest

    # Legacy attributes for backward compatibility
    name: str = ""
    version: str = "1.0"
    athena_api: int = 1
    description: str = ""

    def __init__(self, manifest: Optional[SkillManifest] = None):
        if manifest:
            self.manifest = manifest
        else:
            name_val = getattr(self, "name", self.__class__.__name__.lower())
            caps_val = getattr(self, "capabilities", None)
            if not caps_val:
                caps_val = [name_val] if name_val else []
            perms_val = getattr(self, "permissions", None) or []
            
            self.manifest = SkillManifest(
                name=name_val,
                version=getattr(self, "version", "1.0"),
                athena_api=getattr(self, "athena_api", 1),
                description=getattr(self, "description", ""),
                capabilities=caps_val,
                permissions=perms_val
            )

    def on_initialize(self, ctx: SkillContext) -> None:
        """Lifecycle hook: Called once prior to execution for setup."""
        pass

    def run(self, ctx: SkillContext, task: str) -> SubagentResult:
        """Lifecycle hook: Main execution engine. Must be implemented by skill subclass."""
        raise NotImplementedError

    def on_teardown(self, ctx: SkillContext) -> None:
        """Lifecycle hook: Called post-execution for cleanup."""
        pass
