import pytest
import skills
from skills.base_skill import BaseSkill
from subagent_result import SubagentResult
import worker

@pytest.fixture(autouse=True)
def clear_registry():
    skills.clear()
    yield
    skills.clear()

class DummySkill(BaseSkill):
    name = "dummy"
    version = "1.0"
    athena_api = 1
    description = "A dummy skill for testing"

    def run(self, task: str, memory_context: str) -> SubagentResult:
        return SubagentResult(
            user_output=f"Done task: {task}",
            aal_summary={
                "task": task,
                "skill_used": self.name,
                "outcome": "success",
                "confidence": 0.95,
                "notes": "dummy execution complete"
            },
            memory_payload=["first observation", "second observation"],
            artifacts=[{"filename": "a.txt", "path": "p/a.txt", "type": "text", "description": "dummy file"}]
        )

def test_registry_success_and_incompatible():
    # Success registration
    skill = DummySkill()
    skills.register(skill)
    assert skills.get("dummy") == skill

    # Incompatible skill
    class BadSkill(BaseSkill):
        name = "bad"
        version = "1.0"
        athena_api = 99  # Mismatched api version
        description = "Bad skill"

    with pytest.raises(skills.IncompatibleSkillError):
        skills.register(BadSkill())

def test_worker_execution_success():
    skill = DummySkill()
    skills.register(skill)

    plan = {
        "skill": "dummy",
        "task_description": "test task",
        "memory_context": "test context",
        "prior_outcome": None
    }
    
    result = worker.execute(plan)
    assert result.user_output == "Done task: test task"
    assert result.aal_summary["outcome"] == "success"
    assert result.memory_payload == ["first observation", "second observation"]
    assert len(result.artifacts) == 1

def test_worker_skill_not_found():
    plan = {
        "skill": "non_existent_skill",
        "task_description": "test task",
        "memory_context": "test context",
        "prior_outcome": None
    }
    result = worker.execute(plan)
    assert "not registered" in result.user_output
    assert result.aal_summary["outcome"] == "failed"

def test_worker_execution_crash():
    class CrashSkill(BaseSkill):
        name = "crash"
        version = "1.0"
        athena_api = 1
        description = "Crashes on run"

        def run(self, task: str, memory_context: str) -> SubagentResult:
            raise RuntimeError("something went wrong")

    skills.register(CrashSkill())
    plan = {
        "skill": "crash",
        "task_description": "test task",
        "memory_context": "test context",
        "prior_outcome": None
    }
    result = worker.execute(plan)
    assert "Error executing skill" in result.user_output
    assert result.aal_summary["outcome"] == "failed"
    assert "something went wrong" in result.aal_summary["notes"]
