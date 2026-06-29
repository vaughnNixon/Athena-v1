import pytest
import memory_engine
import business_manager

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_athena.db"
    monkeypatch.setattr(memory_engine, "get_db_path", lambda: test_db)
    monkeypatch.setattr(business_manager, "get_business_dir", lambda: tmp_path / "business")
    memory_engine.initialize_db()
    yield

def test_record_company():
    path = business_manager.record_company(
        name="Acme Corp",
        industry="SaaS",
        summary="Automated analytics vendor.",
        why_it_matters="Key competitor."
    )
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Acme Corp" in content
    assert "Key competitor" in content

def test_get_relevant_business_context():
    business_manager.record_company(
        name="BrightStack Studio",
        industry="video production vendor",
        summary="Overflow editing support.",
        why_it_matters="Partnership mapping."
    )
    
    ctx = business_manager.get_relevant_business_context("What do we know about BrightStack Studio?")
    assert "BUSINESS CONTEXT" in ctx
    assert "video production vendor" in ctx
    
    ctx_none = business_manager.get_relevant_business_context("Let's debug python test cases")
    assert ctx_none == ""
