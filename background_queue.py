import queue
import threading
import logging
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import config
import providers
from service_providers_manager import get_service_manager

logger = logging.getLogger("athena.background_queue")

class JobType(Enum):
    SESSION_SUMMARY = "session_summary"
    TOPIC_UPDATE = "topic_update"
    SUMMARY_COMPACTION = "summary_compaction"
    MEMORY_DISTILLATION = "memory_distillation"
    INDEX_MAINTENANCE = "index_maintenance"
    ROTATION_SNAPSHOT = "rotation_snapshot"

class BackgroundQueue:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(BackgroundQueue, cls).__new__(cls, *args, **kwargs)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._queue = queue.PriorityQueue()
        self._workers: list = []
        self._initialized = True
        self.start_workers(num_workers=2)

    def enqueue(self, job_type: JobType, payload: Dict[str, Any], priority: int = 5):
        # PriorityQueue sorts tuples by first item: (priority, timestamp, job_type, payload)
        import time
        self._queue.put((priority, time.time(), job_type, payload))
        logger.info("Enqueued background job: %s (priority=%d)", job_type.value, priority)

    def start_workers(self, num_workers: int = 2):
        for i in range(num_workers):
            if i >= len(self._workers) or not self._workers[i].is_alive():
                t = threading.Thread(target=self._worker_loop, daemon=True, name=f"Athena-BGWorker-{i}")
                t.start()
                if i < len(self._workers):
                    self._workers[i] = t
                else:
                    self._workers.append(t)

    def _worker_loop(self):
        while True:
            try:
                priority, ts, job_type, payload = self._queue.get()
                logger.info("Worker processing background job: %s", job_type.value)
                self._process_job(job_type, payload)
            except Exception as exc:
                logger.error("Error processing background job: %s", exc)
            finally:
                self._queue.task_done()

    def _process_job(self, job_type: JobType, payload: Dict[str, Any]):
        if job_type == JobType.MEMORY_DISTILLATION:
            import distillation
            user_msg = payload.get("user_msg", "")
            agent_msg = payload.get("agent_msg", "")
            scope_ids = payload.get("scope_ids", [])
            distillation._run_distillation(user_msg, agent_msg, scope_ids)
        elif job_type == JobType.SESSION_SUMMARY:
            import summarizer
            session_id = payload.get("session_id")
            project_id = payload.get("project_id", "default")
            history = payload.get("history", [])
            from_marker = payload.get("from_marker", 0)
            if session_id:
                summarizer.run_session_summary(session_id, project_id, history, from_marker)
        elif job_type == JobType.SUMMARY_COMPACTION:
            import summarizer
            session_id = payload.get("session_id")
            current_summary = payload.get("current_summary", "")
            if session_id:
                summarizer.run_summary_compaction(session_id, current_summary)
        elif job_type == JobType.ROTATION_SNAPSHOT:
            logger.info("Rotation snapshot registered for session %s", payload.get("session_id"))

    def get_maintenance_client(self) -> Tuple[Any, str, str]:
        cfg = config.load_config().get("maintenance_provider", {})
        if cfg.get("enabled"):
            p_name = cfg.get("provider")
            m_name = cfg.get("model")
            if p_name and m_name:
                try:
                    client, default_model = providers.get_client_for_provider(p_name)
                    return client, m_name, p_name
                except Exception as exc:
                    logger.warning("Maintenance provider '%s' failed, falling back to standard pool: %s", p_name, exc)
        
        return providers.get_routing_client()
