import time
import logging
import inspect
import uuid
import config
import skills
import providers
import retrieval
from service_providers_manager import get_service_manager
from subagent_result import SubagentResult

logger = logging.getLogger("athena.worker")

def load_skill(skill_name: str):
    """Loads a skill from the central registry by name or capability."""
    s = skills.get(skill_name)
    if not s:
        s = skills.get_by_capability(skill_name)
    return s

def execute(plan: dict) -> SubagentResult:
    """Executes the loaded skill inside a managed SkillContext container.
    
    Validates policies, invokes lifecycle methods (on_initialize, run, on_teardown),
    and handles exceptions gracefully.
    """
    skill_name = plan.get("skill")
    capability = plan.get("capability") or skill_name
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

    # 1. Build SkillContext for Dependency Injection
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    artifacts_dir = config.get_athena_home() / "artifacts" / task_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    ctx = skills.SkillContext(
        task_id=task_id,
        capability=capability,
        artifacts_dir=artifacts_dir,
        services=get_service_manager(),
        llm_router=providers,
        memory_reader=retrieval,
        logger=logger,
        metadata={"plan": plan}
    )

    manifest_name = skill_obj.manifest.name if hasattr(skill_obj, "manifest") else skill_name

    try:
        # 2. Lifecycle: on_initialize
        if hasattr(skill_obj, "on_initialize") and callable(skill_obj.on_initialize):
            skill_obj.on_initialize(ctx)

        # 3. Lifecycle: run
        target_func = skill_obj.run if hasattr(skill_obj, "run") and callable(skill_obj.run) else skill_obj
        sig = inspect.signature(target_func)
        
        kwargs = {}
        if "ctx" in sig.parameters:
            kwargs["ctx"] = ctx
        if "task" in sig.parameters:
            kwargs["task"] = task_desc
        elif "task_description" in sig.parameters:
            kwargs["task_description"] = task_desc
        if "memory_context" in sig.parameters:
            kwargs["memory_context"] = memory_ctx
        if "capability" in sig.parameters:
            kwargs["capability"] = capability

        # Positional fallback for skills expecting (task, memory_context) or (ctx, task)
        if not kwargs:
            if len(sig.parameters) >= 2:
                result = target_func(ctx, task_desc)
            else:
                result = target_func(task_desc)
        else:
            result = target_func(**kwargs)

        skills.get_registry().record_execution(manifest_name, success=True)
        return result

    except Exception as exc:
        logger.exception("Exception occurred during execution of skill '%s': %s", manifest_name, exc)
        skills.get_registry().record_execution(manifest_name, success=False)
        return SubagentResult(
            user_output=f"Error executing skill '{manifest_name}': {exc}",
            aal_summary={
                "task": task_desc,
                "skill_used": manifest_name,
                "outcome": "failed",
                "confidence": 0.0,
                "notes": f"Execution crashed: {exc}"
            },
            memory_payload=[],
            artifacts=[]
        )
    finally:
        # 4. Lifecycle: on_teardown
        if hasattr(skill_obj, "on_teardown") and callable(skill_obj.on_teardown):
            try:
                skill_obj.on_teardown(ctx)
            except Exception as teardown_exc:
                logger.error("Error during teardown of skill '%s': %s", manifest_name, teardown_exc)
