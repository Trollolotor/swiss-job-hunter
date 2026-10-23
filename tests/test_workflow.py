"""Upstream workflow adaptations must preserve custom matching and source evidence."""

from datetime import datetime

from fastapi.testclient import TestClient

from tests.test_agent_optimization import database as database  # noqa: PLC0414 -- register the shared pytest fixture
from tests.test_agent_optimization import seed


def add_sources(db, job_id):
    from db.models import Employer, EmployerSource, JobSource

    with db.get_session() as session:
        employer = Employer(name="Acme", identity_key="acme-workflow")
        session.add(employer)
        session.flush()
        portal = EmployerSource(
            employer_id=employer.id, namespace="ats:acme", platform="smartrecruiters",
            tenant="Acme", portal_url="https://careers.example.test/",
        )
        session.add(portal)
        session.flush()
        for namespace in ["board:jobs.ch", "board:jobscout24.ch", "ats:acme"]:
            direct = namespace == portal.namespace
            session.add(JobSource(
                job_id=job_id, employer_id=employer.id,
                source_id=portal.id if direct else None, namespace=namespace,
                posting_id="one", url=f"https://example.test/{namespace}/one",
                application_url="https://careers.example.test/apply/one" if direct else None,
                availability="open", payload_json='{"locations":["Bern"]}',
                content_hash="preserved-content",
            ))


def test_source_and_active_filters_keep_profiles_provenance_and_stats(database):
    from analyzer.config import get_config
    from analyzer.matching import local_evaluate
    from config.runtime import get_automation_config, get_llm_config
    from db.models import Job, JobProfile, SearchProfile
    from employers.config import get_config as employer_config
    from server import app

    jid, profile_id, _ = seed(database)
    add_sources(database, jid)
    with database.get_session() as session:
        job = session.get(Job, jid)
        job.user_stars, job.posted_at = 4, datetime.now()
        link = local_evaluate(session, job, session.get(SearchProfile, profile_id))
        link.match_score, link.score_method, link.evaluation_state = .8, "llm", "scored"
        link.match_explanation, link.requirements_json = "Evidence retained", '[{"state":"supported"}]'
        session.add(Job(dedup_hash="archived", title="Old role", company="Acme", location="Bern",
                        description="", source="jobscout24.ch", url="https://example.test/old", status="archived"))
    settings_before = (get_config(), get_automation_config(), get_llm_config(), employer_config())
    client = TestClient(app)
    assert len(client.get("/jobs").json()) == 2  # API default stays compatible.
    assert len(client.get("/jobs?status=active").json()) == 1
    assert len(client.get("/jobs?status=archived").json()) == 1
    for source in ["jobs.ch", "jobscout24.ch", "board:jobscout24.ch", "employer", "ats:acme"]:
        params = {"source": source, "status": "active", "profile_id": profile_id, "q": "Python",
                  "min_stars": 3, "min_score": .7, "published_within": "1", "include_unknown_dates": False}
        response = client.get("/jobs", params=params)
        assert response.status_code == 200
        records = response.json()
        assert [r["id"] for r in records] == [jid]
        record = records[0]
        assert record["source"] == "jobs.ch"
        assert len(record["sources"]) == 3
        assert record["application_url"] == "https://careers.example.test/apply/one"
        assert record["match_score"] == .8 and record["matching_profile_id"] == profile_id
        stats = client.get("/stats", params={**params, "threshold": .7}).json()
        assert stats["total"] == stats["above_threshold"] == 1
        assert client.get("/jobs", params={**params, "min_score": .9}).json() == []
    assert client.get("/jobs?source=employer&direct_only=true").json() == []
    assert client.get("/jobs?source=nonexistent").json() == []
    assert settings_before == (get_config(), get_automation_config(), get_llm_config(), employer_config())
    with database.get_session() as session:
        link = session.get(JobProfile, (jid, profile_id))
        assert link.match_explanation == "Evidence retained"
        assert link.requirements_json == '[{"state":"supported"}]'


def test_tracker_moves_preserve_application_evaluation_and_manual_ownership(database):
    from analyzer.matching import local_evaluate, update_workflow
    from db.models import Application, Job, JobProfile, JobSource, JobStatusChange, SearchProfile
    from job_lifecycle import mark_unavailable
    from server import app

    jid, profile_id, _ = seed(database)
    add_sources(database, jid)
    sent_at = datetime(2026, 1, 2, 10, 30)
    with database.get_session() as session:
        job = session.get(Job, jid)
        job.status, job.status_origin = "considering", "user"
        link = local_evaluate(session, job, session.get(SearchProfile, profile_id))
        link.match_score, link.score_method, link.evaluation_state = .9, "llm", "scored"
        link.requirements_json = '[{"requirement":"Python"}]'
        session.add(Application(job_id=jid, applied_at=sent_at, cover_letter="My tailored letter",
                                notes="Referral", apply_method="manual", contact_name="Recruiter"))
    client = TestClient(app)
    for status in ["applied", "archived", "considering", "applied"]:
        assert client.patch(f"/jobs/{jid}/status", json={"status": status}).json() == {"ok": True}
    assert client.patch(f"/jobs/{jid}/status", json={"status": "invalid"}).status_code == 400
    assert client.patch("/jobs/99999/status", json={"status": "applied"}).status_code == 404
    record = client.get("/tracker").json()[0]
    assert record["id"] == jid and record["applied_at"] == sent_at.isoformat()
    assert record["application_url"] == "https://careers.example.test/apply/one"
    with database.get_session() as session:
        job = session.get(Job, jid)
        update_workflow(session, job)
        mark_unavailable(job, "closed")
        mark_unavailable(job, "closed")
        assert job.status == "applied" and job.status_origin == "user"
        assert job.applied_at == sent_at
        assert session.query(JobSource).count() == 3
        application = session.query(Application).one()
        assert (application.cover_letter, application.notes, application.contact_name) == (
            "My tailored letter", "Referral", "Recruiter")
        assert application.applied_at == sent_at
        link = session.get(JobProfile, (jid, profile_id))
        assert link.match_score == .9 and link.requirements_json == '[{"requirement":"Python"}]'
        assert session.query(JobStatusChange).filter_by(job_id=jid, origin="user").count() == 4


def test_first_applied_move_stamps_once_and_restore_remains_user_owned(database):
    from db.models import Application, Job
    from job_lifecycle import transition
    from server import app

    jid, _, _ = seed(database)
    client = TestClient(app)
    assert client.patch(f"/jobs/{jid}/status", json={"status": "applied"}).status_code == 200
    first = client.get("/tracker").json()[0]["applied_at"]
    assert first
    for status in ["archived", "considering", "applied"]:
        client.patch(f"/jobs/{jid}/status", json={"status": status})
    assert client.get("/tracker").json()[0]["applied_at"] == first
    with database.get_session() as session:
        job = session.get(Job, jid)
        assert not transition(job, "archived", origin="scoring")
        assert job.status == "applied" and job.status_origin == "user"
        assert session.query(Application).count() == 0  # Do not fabricate a sent application.


def test_tracker_hides_browsing_and_untouched_rejects_but_keeps_interactions(database):
    from db.models import Application, Job, JobEvent
    from server import app

    with database.get_session() as session:
        for name, status, origin in [
            ("browsed", "viewed", "user"), ("new", "new", "system"),
            ("auto", "archived", "scoring"), ("legacy", "archived", "legacy_unknown"),
            ("manual", "archived", "user"), ("starred", "archived", "scoring"),
            ("notes", "archived", "legacy_unknown"), ("application", "archived", "legacy_unknown"),
            ("considering", "considering", "user"),
        ]:
            job = Job(dedup_hash=name, title=name, company="Acme", location="Bern", source="jobs.ch",
                      description="", url="https://example.test/"+name, status=status, status_origin=origin)
            session.add(job)
            session.flush()
            if name == "starred": job.user_stars = 4
            if name == "notes": session.add(JobEvent(job_id=job.id, event_type="note", note="Follow up"))
            if name == "application": session.add(Application(job_id=job.id, notes="Previous application"))
    records = TestClient(app).get("/tracker").json()
    assert {r["title"] for r in records} == {"manual", "starred", "notes", "application", "considering"}
