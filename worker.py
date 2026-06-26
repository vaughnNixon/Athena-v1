import logging
import skills
from subagent_result import SubagentResult

logger = logging.getLogger("athena.worker")

def load_skill(skill_name: str):
    """Loads a skill from the central registry."""
    return skills.get(skill_name)

def execute(plan: dict) -> SubagentResult:
    """Executes the loaded skill according to the plan.
    
    If the skill does not exist or fails during execution, it returns a 
    gracefully structured SubagentResult with outcome='failed'.
    """
    skill_name = plan.get("skill")
    task_desc = plan.get("task_description", "")
    memory_ctx = plan.get("memory_context", "")

    skill_obj = load_skill(skill_name)
    if not skill_obj:
        logger.warning("Skill '%s' not found in registry.", skill_name)
        return SubagentResult(
            user_output=f"Error: Skill '{skill_name}' is not registered.",
            aal_summary={
                "task": task_desc,
                "skill_used": skill_name,
                "outcome": "failed",
                "confidence": 0.0,
                "notes": f"Skill '{skill_name}' not found in registry."
            },
            memory_payload=[],
            artifacts=[]
        )
    
    try:
        if hasattr(skill_obj, "run") and callable(skill_obj.run):
            return skill_obj.run(
                task=task_desc,
                memory_context=memory_ctx
            )
        elif callable(skill_obj):
            return skill_obj(
                task=task_desc,
                memory_context=memory_ctx
            )
        else:
            raise TypeError("Loaded skill must implement run() or be callable.")
    except Exception as exc:
        logger.exception("Exception occurred during execution of skill '%s': %s", skill_name, exc)
        return SubagentResult(
            user_output=f"Error executing skill '{skill_name}': {exc}",
            aal_summary={
                "task": task_desc,
                "skill_used": skill_name,
                "outcome": "failed",
                "confidence": 0.0,
                "notes": f"Execution crashed: {exc}"
            },
            memory_payload=[],
            artifacts=[]
        )
