import pytest
import skills
from pathlib import Path
from skills import (
    SkillManifest, SkillContext, SkillPolicyEngine, BaseSkill,
    CapabilityRegistry, RuntimeSkillLoader, PolicyViolationError,
    IncompatibleSkillError, PERM_NETWORK_HTTP, PERM_FILESYSTEM_READ
)
from subagent_result import SubagentResult

class DummySkill(BaseSkill):
    initialized = False
    torndown = False

    def __init__(self):
        manifest = SkillManifest(
            name="dummy_skill",
            version="1.0.0",
            athena_api=1,
            description="A test dummy skill.",
            capabilities=["test.dummy"],
            permissions=[PERM_NETWORK_HTTP]
        )
        super().__init__(manifest=manifest)

    def on_initialize(self, ctx: SkillContext):
        self.initialized = True

    def run(self, ctx: SkillContext, task: str) -> SubagentResult:
        return SubagentResult(
            user_output="Dummy executed",
            aal_summary={"outcome": "success", "confidence": 1.0},
            memory_payload=[],
            artifacts=[]
        )

    def on_teardown(self, ctx: SkillContext):
        self.torndown = True

def test_skill_manifest_validation():
    m = SkillManifest(name="valid", version="1.0", athena_api=1, description="desc", capabilities=["cap1"])
    m.validate(current_api_version=1)

    m_invalid_api = SkillManifest(name="invalid", version="1.0", athena_api=99, description="desc", capabilities=["cap1"])
    with pytest.raises((ValueError, IncompatibleSkillError)):
        m_invalid_api.validate(current_api_version=1)

def test_skill_policy_engine():
    engine = SkillPolicyEngine(allowed_permissions=[PERM_NETWORK_HTTP])
    m = SkillManifest(name="test", version="1.0", athena_api=1, description="", capabilities=["c"], permissions=[PERM_NETWORK_HTTP])
    engine.validate_manifest_permissions(m)
    engine.check_permission(m, PERM_NETWORK_HTTP)

    with pytest.raises(PolicyViolationError):
        engine.check_permission(m, PERM_FILESYSTEM_READ)

def test_capability_registry_and_lifecycle():
    skills.clear()
    skill = DummySkill()
    skills.register(skill)

    assert skills.get("dummy_skill") == skill
    assert skills.get_by_capability("test.dummy") == skill

    import worker
    res = worker.execute({"skill": "dummy_skill", "task_description": "do dummy"})
    assert res.user_output == "Dummy executed"
    assert skill.initialized is True
    assert skill.torndown is True
