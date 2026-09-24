# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. Agents register, claim tasks, and
submit results through the HTTP API. Local development defaults to SQLite;
the Docker Compose and Kubernetes setups use PostgreSQL for persistent
storage.

## Project overview

- Agents register with `POST /api/v1/agents` and receive a bearer token.
- A sender creates a task with `POST /api/v1/tasks`; a recipient claims it
  with `POST /api/v1/tasks/claim` and submits the result with
  `POST /api/v1/tasks/{task_id}/complete` (or `/fail`).
- Every registration, task, claim, heartbeat, and result is persisted in the
  database — SQLite locally, PostgreSQL in the containerized/Kubernetes
  setups.
- `GET /health` is a liveness check; `GET /ready` verifies database
  connectivity and schema (it queries the real tables, so a wiped volume
  reports not-ready instead of passing with zero tables).
- A token-based dashboard is served at `/dashboard`.

## Architecture

| Component | Role |
| --- | --- |
| FastAPI Agent Relay API (`main.py`, `storage.py`, `schemas.py`) | HTTP API for registration, task lifecycle, and the dashboard |
| PostgreSQL | Persistent storage in Docker Compose and Kubernetes (`database.py` selects the engine from `RELAY_DATABASE_URL`) |
| Docker / Docker Compose | `Dockerfile` image; `compose.yaml` runs Agent Relay + PostgreSQL |
| Kubernetes | Manifests under `k8s/` deployed to a kind cluster |
| GitHub Actions CI/CD | `.github/workflows/ci.yml` — tests → build → deploy |

Agents communicate with the relay only through the HTTP API; tasks are
persisted in the database and claimed with leased, single-owner semantics
(`BEGIN IMMEDIATE` on SQLite, `FOR UPDATE SKIP LOCKED` row locking on
PostgreSQL).

## Local development

Install dependencies from the lock file and run the test suite:

```bash
uv sync --frozen
uv run pytest -q
```

Run the API locally (defaults to `./agent-relay.db` SQLite; set
`RELAY_DATABASE_URL` to use another database):

```bash
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/dashboard> for the dashboard.

Register two identities (the secret `token` is returned once — keep it out
of source control; use `Authorization: Bearer <token>` for all subsequent
calls; registration is the only unauthenticated endpoint):

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

For a shared installation, set `RELAY_ENROLLMENT_SECRET` and send it as
`X-Enrollment-Secret` when registering.

### Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a
new token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

### Storage and delivery behavior

`database.py` contains SQLAlchemy models and the transaction seam: SQLite
uses WAL plus a `BEGIN IMMEDIATE` writer transaction, while PostgreSQL uses
ordinary transactions with `FOR UPDATE SKIP LOCKED` row locks for atomic
claims. `storage.py` contains task/claim/recovery operations; routes and
request models live in `main.py` and `schemas.py`.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats
extend an active lease. A completion or failure must include the recipient's
bearer token and claim token. Repeating the exact terminal request with that
claim token is idempotent; a stale token or different result receives `409`.

## Docker

```bash
docker build -t agent-relay:local .
docker run -d --name agent-relay-homework3 -p 8000:8000 agent-relay:local
```

Dashboard: <http://127.0.0.1:8000/dashboard>

A bare `docker run` uses the default SQLite file inside the container unless
`RELAY_DATABASE_URL` is set; PostgreSQL is used via Docker Compose or
Kubernetes below.

## Docker Compose

```bash
docker compose up --build -d
docker compose down
```

- Services: `agent-relay` and `postgres` (PostgreSQL service hostname is
  `postgres` — never `localhost` from the Agent Relay container).
- Agent Relay connects using:
  `postgresql+psycopg://relay:relay@postgres:5432/agent_relay`
- PostgreSQL data is persisted through the `postgres_data` Docker volume
  (`docker compose down` keeps it; add `-v` to delete it).
- Dashboard after startup: <http://127.0.0.1:8000/dashboard>

## Kubernetes

- kind cluster name: `agent-relay`
- kubectl context: `kind-agent-relay`
- Manifests: `k8s/` (`agent-relay` Deployment + Service, `postgres`
  Deployment + Service + `postgres-pvc`)
- Agent Relay connects to PostgreSQL through the Kubernetes Service DNS
  hostname `postgres`
  (`postgresql+psycopg://relay:relay@postgres:5432/agent_relay`);
  PostgreSQL storage is the `postgres-pvc` PersistentVolumeClaim
- Image `agent-relay:local` is loaded into kind (GitHub Actions cannot use
  your local cluster, so CI creates its own temporary one)

```bash
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl apply -f k8s/
kubectl rollout status deployment/agent-relay
kubectl rollout status deployment/postgres
```

Check resources:

```bash
kubectl get pods
kubectl get deployments
kubectl get services
kubectl get pvc
```

Port-forward the Agent Relay service and open the dashboard:

```bash
kubectl port-forward svc/agent-relay 8000:8000
```

Dashboard: <http://127.0.0.1:8000/dashboard>

## CI/CD

`.github/workflows/ci.yml`:

- Triggers on `push` and `pull_request`.
- Pipeline: **tests → build → deploy**, with jobs chained by `needs:`
  (`build` needs `test`; `deploy` needs `test` and `build`).
- If tests fail, the workflow stops: **build and deploy are skipped and the
  existing version keeps running.** There is no deploy-before-test path.
- `test` runs `uv sync --frozen` and `uv run pytest -q`.
- `build` runs `docker build -t agent-relay:local .` and uploads the image
  as an artifact.
- `deploy` loads the image into an **ephemeral kind cluster inside the CI
  runner**, applies the same `k8s/` manifests, and verifies readiness.
  GitHub-hosted runners cannot reach the local Q5 kind cluster, so that
  cluster is never touched by GitHub Actions.

## Verification

- Test suite currently passes: **5 passed** (`uv run pytest -q`).
- The Q2 task flow was verified against Docker Compose and Kubernetes:
  **queued → processing/claimed → completed** (final sender-visible status
  `completed`, result persisted in PostgreSQL).
