import os
import time
import logging
from datetime import datetime
from pathlib import Path

import memory_engine
import memory_sweep
import people_manager
import decisions_manager
import business_manager
import schedule_manager
import insights_manager
import daily_manager

logger = logging.getLogger("athena.second_brain")

class SecondBrainEngine:
    """
    Athena's Second Brain Orchestrator & Compounding Cognitive Engine.
    Combines memory tiering, entity routing, daily consolidation, and human materialization.
    """
    def __init__(self):
        self.people = people_manager
        self.decisions = decisions_manager
        self.business = business_manager
        self.schedule = schedule_manager
        self.insights = insights_manager
        self.daily = daily_manager
        
    def run_brain_consolidation(self) -> dict:
        """
        Executes the 'Sleep' Memory Consolidation routine.
        1. Runs SQLite chunk lifecycle tiering sweep.
        2. Consolidates today's facts and turns into a daily journal note.
        3. Returns consolidation statistics.
        """
        logger.info("Starting Second Brain consolidation sweep ('Sleep' routine)...")
        start_time = time.time()
        
        # 1. Run memory lifecycle sweep (Active/Passive tiering)
        try:
            memory_sweep.run_memory_sweep()
            sweep_status = "success"
        except Exception as e:
            logger.error("Memory sweep failed during brain consolidation: %s", e)
            sweep_status = f"failed: {e}"
            
        # 2. Materialize today's daily journal note
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            daily_note = self.daily.generate_daily_note(today_str)
            daily_status = "success"
        except Exception as e:
            logger.error("Daily note generation failed: %s", e)
            daily_status = f"failed: {e}"
            
        elapsed = time.time() - start_time
        logger.info("Second Brain consolidation finished in %.2fs", elapsed)
        
        return {
            "date": today_str,
            "elapsed_seconds": round(elapsed, 2),
            "sweep_status": sweep_status,
            "daily_status": daily_status,
            "entities_tracked": len(self.people.list_people())
        }

_brain_instance = None

def get_brain() -> SecondBrainEngine:
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = SecondBrainEngine()
    return _brain_instance

def run_consolidation() -> dict:
    return get_brain().run_brain_consolidation()
