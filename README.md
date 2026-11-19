# S.P.D. Emergency

A distributed emergency-resource matching system. People who need help post a JSON
request, people who can help post a JSON resource, and a matching engine pairs each
request with the nearest suitable, available helper. The name nods to the Power
Rangers S.P.D. morphing call ("S.P.D. Emergency!"); the control room is "Delta Command".

Built for the CS838 cloud course project. The shared data contract is in
[docs/DESIGN.md](docs/DESIGN.md), the build order in [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

## Architecture

```
  client/cli.py ──HTTP──► Request Service ──publish──► RabbitMQ ──consume──► Matching Service
                          (2 pods)                     (queue)               (2+ pods)
                              │                                                   │
                              └──────────────── StorageClient (HTTP) ─────────────┘
                                                        │
                                                   Coordinator
                          heartbeats · shard rule · replication · failover · catch-up
                              ┌─────────────────────────┼─────────────────────────┐
                           Shard 0                   Shard 1                   Shard 2
                      primary + replica         primary + replica         primary + replica
                        (SQLite each)             (SQLite each)             (SQLite each)
```

| Part | What it does | Stack |
|---|---|---|
| **Request service** | Client REST API: validates input, saves requests and resources, queues "please match this" messages | Python 3.12, FastAPI, Uvicorn, pydantic v2, httpx, pika |
| **RabbitMQ** | Holds pending work between the two services | `rabbitmq:3.13-management`, AMQP |
| **Matching service** | Takes one message at a time and matches the request; also serves `POST /match/{id}` | Python, FastAPI, pika, httpx, a background consumer thread |
| **Coordinator** | The only door to storage: picks the shard, writes to the leader, copies to the replica, fails over, catches nodes up | Python, FastAPI, async httpx |
| **Storage nodes** (6) | One SQLite file each; versioned records with compare-and-set | Python, FastAPI, SQLite (WAL, `synchronous=FULL`) |
| **Packaging** | One image per service; Compose for a laptop, Kubernetes for the real deployment | Docker, Docker Compose, Kubernetes (Deployments, StatefulSets, Services, ConfigMap) |

### How one request flows

1. `POST /requests` hits a request-service pod. The body is validated (bad input gets a 400).
2. The record is saved through the coordinator: the key is hashed to a shard, written to
   that shard's leader node, copied to its replica, and only then acknowledged.
3. A `{request_id, attempt}` message is published to RabbitMQ (with publisher confirms) and
   the client gets `201` immediately. The client never waits for matching.
4. A matching-service pod receives the message, finds the nearest free resource of the
   same type within 50 km, claims it with a version check, and marks the request `MATCHED`
   (or `UNMATCHED` if nothing suitable exists). Only then is the message acknowledged.
5. The client reads the result with `GET /requests/{id}`.

## Repository layout

```
services/
  common/             shared: record shapes and status rules, shard rule, StorageClient, config, queue contract
  storage_node/       one storage node (SQLite + API + heartbeat)
  coordinator/        registry, router, recovery (and an in-memory fake used by the unit tests)
  request_service/    client API (schemas, service rules, RabbitMQ publisher)
  matching_service/   matcher, geo, RabbitMQ consumer
client/cli.py         command-line client
k8s/                  Kubernetes manifests (apply with kustomize)
scripts/              seed data, demos, helpers
tests/unit/           fast tests, no network needed
tests/integration/    tests against a live stack
docs/                 design, build plan, resume roadmap
```

## Prerequisites

- Docker Desktop (with Docker Compose)
- Python 3.12 or newer, for the client, demos and tests
- For Kubernetes: `kubectl`, and a cluster (Docker Desktop's Kubernetes, or minikube)

```
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
```

## Run with Docker Compose

```
docker compose up --build -d
python scripts/seed.py            # registers sample responders in five cities
```

| What | Where |
|---|---|
| Client API and Swagger UI | http://localhost:8080 , http://localhost:8080/docs |
| Coordinator (status of all nodes) | http://localhost:8000/internal/status |
| RabbitMQ management UI | http://localhost:15672 (guest / guest) |

To run several matchers: `docker compose up -d --scale matching-service=3`.
Stop everything with `docker compose down`.

## Run on Kubernetes

With minikube (Docker Desktop's built-in Kubernetes works the same way, without the image load step):

```
docker compose build                          # builds the four service images the manifests use
docker pull rabbitmq:3.13-management
minikube start --driver=docker
for i in storage-node coordinator request-service matching-service rabbitmq:3.13-management; do minikube image load $i; done
kubectl apply -k k8s
kubectl -n emergency get pods                 # wait until all are Running
python scripts/k8s_forward.py                 # keep running: same local ports as Compose
```

Stop Compose first; both use the same local ports.

## Using it

```
python client/cli.py register water "City Water Tanker 4" 12.99 77.60 --capacity 500
python client/cli.py request water 12.9716 77.5946 --quantity 20 --details "2 kids, flooded"
python client/cli.py get-request req-<id>        # MATCHED, with assigned_resource_id
python client/cli.py resources --status ASSIGNED
python client/cli.py set-request-status req-<id> IN_PROGRESS
python client/cli.py set-request-status req-<id> FULFILLED    # frees the responder
```

REST endpoints (request service):

| Call | Purpose |
|---|---|
| `POST /requests`, `GET /requests/{id}` | Create and read a request |
| `PUT /requests/{id}/status` | Move a request along its life cycle |
| `POST /resources`, `GET /resources?type=&status=` | Register and list responders |
| `PUT /resources/{id}/status` | Set a responder `AVAILABLE` or `OFFLINE` |
| `POST /match/{id}` | Match a request now instead of waiting for the queue |
| `GET /health` | Liveness |

Errors: invalid input is `400`, an unknown id `404`, an illegal status move `409`,
storage unavailable `503`.

## Demos

Each script prints a before / during / after view.

| Script | Shows |
|---|---|
| `demo_distribution.py` | Records spread across the 3 shards and copied to replicas |
| `demo_storage_failure.py` | A storage node fails; the replica keeps serving |
| `demo_recovery.py` | The node returns and catches up before serving again |
| `demo_matching.py` | Create, register and match: nearest responder, free responders only, release on fulfil |
| `demo_async.py` | 40 requests at once flow through RabbitMQ; nobody is assigned twice |
| `demo_k8s_recovery.py --app matching-service` | Delete an app pod; Kubernetes replaces it and no request is lost |
| `demo_k8s_scaling.py --replicas 4` | Scale the matchers up; work spreads across the pods |

## Tests

```
python -m pytest                      # unit tests, no stack needed
python -m pytest -m integration       # needs a running Compose or Kubernetes stack
```

For the concurrency test, run the stack with `--scale matching-service=3`.
The integration tests skip themselves when no stack is reachable.

## Design choices and why

**Matching and correctness**
- **Version check instead of locks.** Every record carries a `version`; a change is saved
  only if the version is still the one that was read. Two matchers racing for one
  responder cannot both win: the loser gets a conflict and tries the next candidate. This
  works across processes and machines, where an in-memory lock would not.
- **Claim the resource first, then update the request.** If the second step fails the
  resource is released. If the matcher dies between the two, the retry finds the resource
  it already claimed and finishes the job instead of taking a second one.
- **Clients cannot set `MATCHED`, `UNMATCHED` or `ASSIGNED`.** Only the matcher knows the
  truth; a client setting them would leave a request matched to nothing.
- **Straight-line (haversine) distance within 50 km.** Simple, deterministic and needs no
  map service. Ties go to the earliest-registered resource. The limit is configurable.

**Messaging**
- **Asynchronous matching through RabbitMQ.** The API answers as soon as the request is
  safely stored; matching can be slow, retried or scaled separately.
- **Acknowledge after matching, one message at a time (prefetch 1).** A pod that dies
  mid-job returns its message to the queue. Each free pod takes the next message, so work
  spreads across pods by itself.
- **Three attempts, then a dead-letter queue.** A message that keeps failing is parked, not
  retried forever. A storage outage does not use up an attempt, because it is not the
  message's fault.

**Storage**
- **Hash sharding (SHA-1 of the key, mod 3).** Even spread, and every service computes the
  same answer. Python's built-in `hash()` is avoided because it differs between processes.
- **Synchronous replication to one replica, "newer version wins".** A write is acknowledged
  only after both copies have it. Versions, not timestamps, decide, because container clocks
  can disagree. Applying the same update twice changes nothing, which makes catch-up safe.
- **Delete leaves a tombstone.** Otherwise a node that was down would copy a deleted
  record back when it recovered.
- **Sticky leadership.** After a failover the replica stays leader when the old primary
  returns. Handing leadership back mid-write could let two nodes accept a change on the
  same version.
- **A returning node catches up before serving.** It registers as `RECOVERING`, receives
  new writes, syncs with its partner, and only then becomes `UP`.
- **A search that cannot reach a whole shard fails with 503** instead of returning a
  partial list. A partial "no water available" answer would wrongly mark requests
  `UNMATCHED`.
- **SQLite per node.** One file, atomic compare-and-set in a single SQL statement, durable
  with `synchronous=FULL`, and no extra database server to run.

**Deployment**
- **One image per service, configured by environment variables.** The same image runs in
  Compose and Kubernetes; only the settings change.
- **Stateless app services as Deployments, storage as StatefulSets.** App pods are
  interchangeable. Storage pods need a stable name and their own disk, so a restarted
  node gets its data back.
- **Health probes everywhere.** Compose waits for healthy dependencies; Kubernetes only
  sends traffic to ready pods and restarts stuck ones.

## Known limitations

- If RabbitMQ is down when a request is created, the request is saved as `PENDING` and the
  client still gets `201`, but it is not queued. It can be matched with `POST /match/{id}`.
  The proper fix (an outbox) is on the roadmap.
- On Kubernetes, RabbitMQ keeps its data in an `emptyDir`: queued messages are lost if its
  pod is deleted. Compose keeps them in a volume.
- If freeing a responder fails after its request ends, the responder stays `ASSIGNED`
  (logged, no cleanup job yet).
- Quantity and capacity are stored but not used by the basic matching.

More ideas are in [docs/RESUME_ROADMAP.md](docs/RESUME_ROADMAP.md).
