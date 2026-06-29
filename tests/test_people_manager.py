import pytest
import people_manager

def test_people_manager_detection():
    # Verify Lucky is detected
    ctx_lucky = people_manager.get_relevant_people_context("How is lucky doing today?")
    assert "PERSONAL ENTITY PROFILE (Lucky)" in ctx_lucky
    assert "Doberman cross" in ctx_lucky
    
    # Verify Ringgu is detected
    ctx_ringgu = people_manager.get_relevant_people_context("Tell me about ringgu")
    assert "PERSONAL ENTITY PROFILE (Ringgu)" in ctx_ringgu
    assert "EV scooter" in ctx_ringgu
    
    # Verify unrelated messages return empty string (0 tokens!)
    ctx_none = people_manager.get_relevant_people_context("Let's write some python code for SQLite")
    assert ctx_none == ""
