from __future__ import annotations

import threading

from app.db.base import SessionLocal, init_db
from app.db.models import IngestedEvent
from app.services.event_ingestion import claim_event, event_key


def test_event_key_prefers_delivery_id(_environment):
    init_db()
    a = event_key(provider="github", delivery_id="abc", run_id="1")
    b = event_key(provider="github", delivery_id="abc", run_id="999")
    assert a == b


def test_event_key_is_provider_scoped(_environment):
    init_db()
    assert event_key(provider="github", run_id="123") != event_key(provider="azure", run_id="123")


def test_claim_event_rejects_duplicate(_environment, db):
    init_db()
    claimed, key = claim_event(
        db, provider="github", delivery_id="delivery-1",
        repository="octo/repo", run_id="42", commit_sha="abc", event_type="workflow_run",
    )
    db.commit()
    assert claimed is True

    duplicate, duplicate_key = claim_event(
        db, provider="github", delivery_id="delivery-1",
        repository="octo/repo", run_id="42", commit_sha="abc", event_type="workflow_run",
    )
    assert duplicate is False
    assert duplicate_key == key
    assert db.query(IngestedEvent).count() == 1


def test_claim_event_allows_same_run_from_different_provider(_environment, db):
    init_db()
    first, _ = claim_event(db, provider="github", repository="octo/repo", run_id="42", commit_sha="abc", event_type="workflow_run")
    db.commit()
    second, _ = claim_event(db, provider="azure", repository="octo/repo", run_id="42", commit_sha="abc", event_type="build.complete")
    assert first is True
    assert second is True


def test_concurrent_claim_has_single_winner(_environment):
    init_db()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        db = SessionLocal()
        try:
            barrier.wait()
            results.append(claim_event(db, provider="github", delivery_id="concurrent-1", repository="octo/repo", run_id="99", commit_sha="abc", event_type="workflow_run")[0])
            if results[-1]:
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
