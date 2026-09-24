"""Protocol tests for the SQLite starter.

These tests intentionally exercise storage calls from multiple threads: that
is the closest local equivalent to several worker processes racing to claim an
inbox.  The production guarantee comes from SQLite's BEGIN IMMEDIATE boundary,
not from a Python lock.
"""

from __future__ import annotations

import subprocess
import sys
import time
import os

# Default to a scratch DB so `pytest` never resets the dev server's
# `./agent-relay.db`. Respect an explicit RELAY_DATABASE_URL/DATABASE_URL
# (e.g. CI pointing at PostgreSQL), but otherwise isolate tests.
os.environ.setdefault("RELAY_DATABASE_URL", "sqlite:////tmp/agent-relay-test.db")

from concurrent.futures import ThreadPoolExecutor
import httpx
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from database import Attempt, Base, Task, as_db_time, db_session, engine, utcnow
from storage import claim_one


@pytest.fixture(autouse=True)
def empty_database():
    # Resets whatever DB RELAY_DATABASE_URL points at. Defaults to the
    # scratch /tmp file above; never run against a DB with data you need.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def register(client: TestClient, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_protocol_idempotency_terminal_retry_and_auth_boundary():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "uppercase")
        sent = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert sent.status_code == 201
        duplicate = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert duplicate.status_code == 201
        assert duplicate.json() == sent.json()
        conflict = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "different"},
        )
        assert conflict.status_code == 409

        task_id = sent.json()["task_id"]
        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "worker-a", "wait_seconds": 0},
        )
        assert claim.status_code == 200
        claim_data = claim.json()
        assert "claim_token" in claim_data
        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert complete.status_code == 200
        retry = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert retry.status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}", headers=recipient_headers).status_code == 200
        forbidden = client.get(f"/api/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {sender['token']}"})
        assert forbidden.status_code == 200  # sender is an authorized participant
        no_credentials = client.get("/api/v1/agents")
        assert no_credentials.status_code == 401
        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers).json()
        assert attempts["items"][0]["outcome"] == "completed"
        assert "claim_token" not in attempts["items"][0]


def test_sqlite_atomic_claims_distribute_without_overlap():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, _recipient_headers = register(client, "recipient")
        for index in range(16):
            response = client.post(
                "/api/v1/tasks",
                headers=sender_headers,
                json={"to": recipient["agent_id"], "input": f"task-{index}"},
            )
            assert response.status_code == 201
        with ThreadPoolExecutor(max_workers=16) as pool:
            claims = list(pool.map(lambda index: claim_one(recipient["agent_id"], f"worker-{index}"), range(16)))
        claims = [claim for claim in claims if claim is not None]
        assert len(claims) == 16
        assert len({claim["task_id"] for claim in claims}) == 16
        with db_session() as db:
            processing = list(db.query(Task).filter(Task.status == "processing"))
            assert len(processing) == 16
            assert all(task.attempt_count == 1 for task in processing)


def test_expiry_requeues_and_old_token_is_stale_before_recovery():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "recipient")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "recover me"},
        ).json()
        task_id = task["task_id"]
        first = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "dead", "wait_seconds": 0}
        ).json()
        with db_session() as db:
            attempt = db.query(Attempt).filter(Attempt.task_id == task_id).one()
            attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))
        stale = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": first["claim_token"], "output": "TOO LATE"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_claim"
        assert main.recover_expired() == 1
        second = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "replacement", "wait_seconds": 0}
        )
        assert second.status_code == 200
        assert second.json()["attempt"] == 2
        assert second.json()["claim_token"] != first["claim_token"]


def test_dashboard_is_asset_and_invalid_input_is_documented_error():
    with TestClient(main.app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "sessionStorage" in page.text
        missing_name = client.post("/api/v1/agents", json={})
        assert missing_name.status_code == 400
        assert missing_name.json()["error"]["code"] == "invalid_input"


def test_acceptance_scenario_one_over_real_http_api_and_sqlite(tmp_path, free_tcp_port):
    """SPEC acceptance scenario 1 through Uvicorn, HTTP, and a separate SQLite file.

    This deliberately does not use TestClient: the server is a real subprocess,
    as it would be in local development, and receives requests through TCP.
    """

    database_path = tmp_path / "acceptance-scenario.db"
    environment = {**os.environ, "RELAY_DATABASE_URL": f"sqlite:///{database_path}"}
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
            "--port", str(free_tcp_port), "--log-level", "warning",
        ],
        cwd=os.path.dirname(__file__),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{free_tcp_port}"
    try:
        with httpx.Client(base_url=base_url, timeout=2.0) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if client.get("/ready").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if time.monotonic() >= deadline:
                    stderr = server.stderr.read() if server.stderr else ""
                    pytest.fail(f"Uvicorn did not become ready: {stderr}")
                time.sleep(0.05)

            sender = client.post("/api/v1/agents", json={"name": "agent-1"})
            recipient = client.post("/api/v1/agents", json={"name": "agent-2"})
            assert sender.status_code == recipient.status_code == 201
            sender_data, recipient_data = sender.json(), recipient.json()
            sender_headers = {"Authorization": f"Bearer {sender_data['token']}"}
            recipient_headers = {"Authorization": f"Bearer {recipient_data['token']}"}

            created = client.post(
                "/api/v1/tasks",
                headers={**sender_headers, "Idempotency-Key": "acceptance-scenario-1"},
                json={"to": recipient_data["agent_id"], "input": "return integration result"},
            )
            assert created.status_code == 201
            task_id = created.json()["task_id"]
            assert created.json()["status"] == "queued"

            claim = client.post(
                "/api/v1/tasks/claim", headers=recipient_headers,
                json={"worker_id": "agent-2-worker", "wait_seconds": 0},
            )
            assert claim.status_code == 200
            assert claim.json()["task_id"] == task_id
            assert claim.json()["attempt"] == 1

            completed = client.post(
                f"/api/v1/tasks/{task_id}/complete", headers=recipient_headers,
                json={"claim_token": claim.json()["claim_token"], "output": "integration result"},
            )
            assert completed.status_code == 200
            assert completed.json() == {"task_id": task_id, "status": "completed"}

            # These are the same authenticated endpoints rendered by dashboard.html.
            sender_view = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
            sender_tasks = client.get("/api/v1/tasks?direction=sent&limit=100", headers=sender_headers)
            attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers)
            assert sender_view.status_code == sender_tasks.status_code == attempts.status_code == 200
            assert sender_view.json()["status"] == "completed"
            assert sender_view.json()["output"] == "integration result"
            assert sender_tasks.json()["items"] == [sender_view.json()]
            history = attempts.json()["items"]
            assert len(history) == 1
            assert history[0]["attempt"] == 1
            assert history[0]["worker_id"] == "agent-2-worker"
            assert history[0]["outcome"] == "completed"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
