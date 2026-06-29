import pytest
import memory_engine
import second_brain

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    memory_engine.initialize_db()
    yield

def test_second_brain_consolidation():
    res = second_brain.run_consolidation()
    assert isinstance(res, dict)
    assert res["sweep_status"] == "success"
    assert res["daily_status"] == "success"
    assert "date" in res
