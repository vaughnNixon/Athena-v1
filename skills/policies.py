import logging
from typing import List
from .manifest import SkillManifest

logger = logging.getLogger("athena.skills.policies")

# Standardized Permission Constants
PERM_NETWORK_HTTP = "permission.network.http"
PERM_FILESYSTEM_READ = "permission.filesystem.read"
PERM_FILESYSTEM_WRITE = "permission.filesystem.write"
PERM_SHELL_EXECUTE = "permission.shell.execute"
PERM_STORAGE_ARTIFACTS = "permission.storage.artifacts"

class PolicyViolationError(PermissionError):
    """Raised when a skill attempts to perform an action without required permissions."""
    pass

class SkillPolicyEngine:
    def __init__(self, allowed_permissions: List[str] = None, enforce_strict: bool = False):
        self.allowed_permissions = allowed_permissions # None means all standard permissions allowed unless restricted
        self.enforce_strict = enforce_strict

    def validate_manifest_permissions(self, manifest: SkillManifest):
        """Validates that a skill's declared permissions are acceptable under current system policies."""
        if self.allowed_permissions is not None:
            for perm in manifest.permissions:
                if perm not in self.allowed_permissions:
                    raise PolicyViolationError(
                        f"Skill '{manifest.name}' requests forbidden permission '{perm}'."
                    )

    def check_permission(self, manifest: SkillManifest, permission: str):
        """Enforces that a skill has declared the required permission for an action."""
        if permission not in manifest.permissions:
            err_msg = f"Skill '{manifest.name}' attempted action requiring undeclared permission '{permission}'."
            logger.error(err_msg)
            raise PolicyViolationError(err_msg)
