from fastapi.testclient import TestClient


def _client():
    import server
    return TestClient(server.app)


def test_profile_search_settings_normalizes_tags():
    client = _client()
    created = client.post("/profiles", json={
        "slug": "profile-api-test",
        "name": "Profile API Test",
        "keywords": ["one"],
        "cv_sections": {"about": "Candidate"},
        "default_location": "Bern",
        "active": True,
    })
    assert created.status_code in (200, 409)
    profile = next(p for p in client.get("/profiles").json() if p["slug"] == "profile-api-test")
    response = client.patch(f"/profiles/{profile['id']}/search-settings", json={
        "keywords": ["AI engineer, LLM engineer", "ai ENGINEER"],
        "default_location": "",
    })
    assert response.status_code == 200
    assert response.json()["keywords"] == ["AI engineer", "LLM engineer"]
    assert response.json()["default_location"] == ""


def test_variant_inherits_search_settings():
    from db.models import Job
    from db.session import get_session, init_db
    init_db()
    client = _client()
    source = next(p for p in client.get("/profiles").json() if p["slug"] == "profile-api-test")
    with get_session() as session:
        job = Job(
            dedup_hash="variant-test-job",
            title="Platform Engineer",
            company="Example",
            location="Bern",
            description="A sufficiently detailed description",
            url="https://example.test/job",
            source="test",
        )
        session.add(job)
        session.flush()
        job_id = job.id
    response = client.post(f"/profiles/{source['id']}/variants", json={
        "name": "Tailored Platform",
        "slug": "tailored-platform-test",
        "job_id": job_id,
        "cv_sections": {"about": "Tailored candidate"},
        "active": True,
    })
    assert response.status_code in (200, 409)
    variant = next(p for p in client.get("/profiles").json() if p["slug"] == "tailored-platform-test")
    assert variant["keywords"] == source["keywords"]
    assert variant["default_location"] == source["default_location"]
    assert variant["parent_profile_id"] == source["id"]
    assert variant["tailored_for_job_id"] == job_id


def test_cv_upload_returns_preview_without_saving(monkeypatch):
    from cv_sections import CVSections
    from llm import structured

    async def fake_structured(**_kwargs):
        return CVSections(about="Parsed candidate", skills="Python"), "openai/test"

    monkeypatch.setattr(structured, "call_structured", fake_structured)
    client = _client()
    before = client.get("/profiles").json()
    response = client.post(
        "/profiles/parse-cv",
        files={"file": ("cv.txt", ("Candidate experience and skills. " * 8).encode(), "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["cv_sections"]["skills"] == "Python"
    assert response.json()["model"] == "openai/test"
    assert client.get("/profiles").json() == before
