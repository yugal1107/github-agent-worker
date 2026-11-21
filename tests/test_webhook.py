import hmac
import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_worker.config import Settings
from agent_worker.queue import JobQueue
from agent_worker.web import MAX_WEBHOOK_BYTES, create_app

SECRET = b"test-only-secret-not-for-deployment"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "jobs.sqlite3"


@pytest.fixture
def client(db_path):
    settings = Settings(SECRET, "@my-agent", 10, frozenset({20}), frozenset({30}), db_path)
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def payload():
    return {
        "action": "created",
        "installation": {"id": 10},
        "repository": {"id": 20, "full_name": "test-owner/test-project"},
        "issue": {"number": 40},
        "comment": {
            "id": 50,
            "body": "@my-agent Fix the button layout\nAdd a regression test.",
            "user": {"id": 30, "type": "User"},
        },
        "sender": {"id": 30, "type": "User"},
    }


def send(client, payload, *, event="issue_comment", delivery=None, secret=SECRET):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid4()),
            "X-Hub-Signature-256": "sha256=" + hmac.digest(secret, body, "sha256").hex(),
        },
    )


def jobs(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute("SELECT issue_number, task, status FROM jobs ORDER BY id").fetchall()


def test_queues_valid_command_and_deduplicates_redeliveries(client, payload, db_path):
    delivery = str(uuid4())
    response = send(client, payload, delivery=delivery)
    assert response.status_code == 202
    assert response.json() == {"status": "queued", "job_id": 1}
    assert send(client, payload, delivery=delivery.upper()).json() == {"status": "duplicate"}
    assert send(client, payload).json() == {"status": "duplicate"}
    assert jobs(db_path) == [(40, "Fix the button layout\nAdd a regression test.", "queued")]


def test_issue_reply_reuses_session_across_worker_restart(client, payload, db_path):
    queue = JobQueue(db_path)
    assert send(client, payload).status_code == 202
    run = queue.claim()
    assert run.kind == "implement"
    assert run.repository == "test-owner/test-project"
    assert queue.claim() is None

    payload["comment"].update(id=51, body="@my-agent reply 1. Keep the API compatible.")
    assert send(client, payload).json() == {"status": "not_waiting"}
    queue.mark_waiting(run)
    # The reply can arrive just after the question posts, before the process exits.
    assert send(client, payload).status_code == 202
    payload["comment"].update(id=52)
    assert send(client, payload).json() == {"status": "busy"}
    queue.finish(run, True)

    restarted = JobQueue(db_path)
    restarted.initialize()
    restarted.recover_interrupted()
    reply = restarted.claim()
    assert reply.session_id == run.session_id
    assert reply.kind == "reply"
    assert reply.task == "1. Keep the API compatible."
    restarted.finish(reply, True)
    assert send(client, payload).json() == {"status": "not_waiting"}


def test_worker_recovers_interrupted_reply(client, payload, db_path):
    queue = JobQueue(db_path)
    assert send(client, payload).status_code == 202
    original = queue.claim()
    queue.mark_waiting(original)
    queue.finish(original, True)
    payload["comment"].update(id=51, body="@my-agent implement more changes")
    assert send(client, payload).json() == {"status": "waiting_for_reply"}
    payload["comment"].update(body="@my-agent reply 1. Use option B")
    assert send(client, payload).status_code == 202
    assert queue.claim().kind == "reply"
    queue.recover_interrupted()
    payload["comment"].update(id=52)
    assert send(client, payload).status_code == 202
    assert queue.claim().session_id == original.session_id


def test_rejects_missing_repository_name(client, payload, db_path):
    payload["repository"]["full_name"] = "owner/.."
    assert send(client, payload).status_code == 400
    assert jobs(db_path) == []


def test_rejects_invalid_signature(client, payload, db_path):
    assert send(client, payload, secret=b"wrong").status_code == 401
    assert client.post("/webhooks/github", json=payload).status_code == 401
    assert jobs(db_path) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("installation", {"id": 11}),
        ("repository", {"id": 21}),
        ("sender", {"id": 31, "type": "User"}),
        ("sender", {"id": 30, "type": "Bot"}),
        ("issue", {"number": 40, "pull_request": {}}),
        ("action", "edited"),
    ],
)
def test_ignores_unapproved_or_unsupported_events(client, payload, db_path, field, value):
    payload[field] = value
    assert send(client, payload).json() == {"status": "ignored"}
    assert jobs(db_path) == []


def test_ignores_mention_inside_noncommand(client, payload, db_path):
    payload["comment"]["body"] = "Can @my-agent fix this?"
    assert send(client, payload).json() == {"status": "ignored"}
    assert jobs(db_path) == []


def test_ping_and_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert send(client, {}, event="ping").json() == {"status": "pong"}


def test_rejects_invalid_or_large_bodies(client, payload, db_path):
    assert send(client, b"\xff").status_code == 400
    assert send(client, b" " * (MAX_WEBHOOK_BYTES + 1)).status_code == 413
    payload["comment"]["body"] = "@my-agent " + "x" * 4097
    assert send(client, payload).status_code == 422
    payload["comment"]["body"] = "@my-agent \ud800"
    assert send(client, payload).status_code == 422
    assert jobs(db_path) == []
