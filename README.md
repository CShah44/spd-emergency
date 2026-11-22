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
| **Web page** (`ui_service`) | Delta Command: a live dashboard and demo controls; talks to the request service over HTTP | Python 3.12, NiceGUI (no JavaScript to write) |
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
  matching_service/   matcher, geo, RabbitMQ consumer, sweeper
  ui_service/         the Delta Command web page (NiceGUI)
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
| **Delta Command web page** | http://localhost:8081 |
| Client API and Swagger UI | http://localhost:8080 , http://localhost:8080/docs |
| Coordinator (status of all nodes) | http://localhost:8000/internal/status |
| RabbitMQ management UI | http://localhost:15672 (guest / guest) |

To run several matchers: `docker compose up -d --scale matching-service=3`.
Stop everything with `docker compose down`.

## Run on Kubernetes

Kubernetes runs through minikube, which starts a whole cluster inside one Docker container
(Docker Desktop's built-in Kubernetes also works). One command does everything:

```
python scripts/k8s_up.py              # build images, start the cluster, deploy, forward ports
python scripts/k8s_up.py --skip-build # reuse the images from last time (faster)
python scripts/k8s_up.py --seed       # also register sample responders
python scripts/k8s_up.py --fresh --seed   # delete all old data first: a clean demo start
```

Stored data survives restarts, so demo runs pile up unless you start with `--fresh`.

It checks Docker and the local ports first, waits until every pod is ready and all six
storage nodes are `UP`, then forwards the same local ports as Docker Compose (so every
URL, demo and test below works unchanged). Stop Compose first; both use the same ports.

```
python scripts/k8s_down.py            # stop the forward and pause the cluster; data is kept
python scripts/k8s_down.py --wipe     # also delete the system and all its data (clean slate)
```

Useful while it runs: `kubectl -n emergency get pods`, `kubectl -n emergency logs deploy/coordinator`.

## The Delta Command web page

Open **http://localhost:8081** (Compose or Kubernetes). It refreshes by itself every second.
From top to bottom it follows the path of a request:

| Panel | What it shows |
|---|---|
| Storage cluster | The 3 shards and their primary and replica: state (UP, RECOVERING, DOWN), the current leader, key counts, and what a returning node copied. Each node has a **Pause** button that simulates a failure, and **Resume** to bring it back. |
| Matching: the queue | Messages waiting, matchers connected (one per matching pod: it changes when you scale), and dead-lettered messages. Shows "RabbitMQ unreachable" if the broker is down. |
| Matching: requests by stage | Counters for PENDING, MATCHED, IN PROGRESS and FULFILLED (plus UNMATCHED and CANCELLED) that update as requests move. |
| What just happened | A running log: new requests, `PENDING -> MATCHED`, responders becoming `ASSIGNED`, nodes going DOWN, leaders changing. |
| Report an emergency / Register a responder | Forms with city presets (the seed data's cities). The confirmation says which shard stored the record. |
| Responders | Who is assigned to what: each responder's status, its shard, and the request it is serving with that request's stage. Assigned rows are highlighted. |
| Emergency requests | Active requests first, newest on top; a request that is fulfilled or cancelled drops below the "Finished" divider. Each row has a progress bar (PENDING, MATCHED, IN PROGRESS, FULFILLED), its shard, and the action buttons that are valid for its status. |
| Stress test | Sends 20 random requests at once, so you can watch the queue fill and drain. |

**Shard chips.** Every request and responder shows `S0`, `S1` or `S2`: the shard that stores it
(the same `sha1(key) mod 3` rule the coordinator uses). When a node of that shard is down the
chip turns orange (`S0 !`: running on one node), and red if the shard has no live node, so a
failure shows on every record it affects.

A single match takes tens of milliseconds, so one request usually flips straight to `MATCHED`
between two refreshes; use the stress test to see the queue work. Deleting pods and scaling
replicas are `kubectl` actions and stay in the terminal; the "matchers connected" count shows
their effect.

The Pause and Resume buttons only work when `DEMO_CONTROLS=true`, which Compose and the
Kubernetes ConfigMap set. It is off by default because anyone who can reach the API could
otherwise take storage nodes down. The page runs as a single pod (it keeps each open tab's
state in memory).

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
| `demo_storage_failure.py` | A storage node fails; the replica keeps serving (on Kubernetes add `--method pause`) |
| `demo_recovery.py` | The node returns and catches up before serving again (on Kubernetes add `--method pause`) |
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
- **A sweeper as a safety net.** A request is saved before it is queued, so a broker outage
  can leave one saved but never queued. Matching is safe to repeat, so a periodic sweep
  that matches old `PENDING` requests directly is enough, with no extra moving parts.
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
- **Stateless app services as Deployments, storage and RabbitMQ as StatefulSets.** App pods
  are interchangeable. Storage pods and the broker need a stable name and their own disk,
  so a restarted node gets its data back and queued messages survive a broker restart.
- **The web page is its own small service, written in Python.** It uses only the public REST API (plus two read-only calls and the demo controls), so it can be removed without touching anything else. NiceGUI keeps the project single-language.
- **Health probes everywhere.** Compose waits for healthy dependencies; Kubernetes only
  sends traffic to ready pods and restarts stuck ones.

## Known limitations

- If RabbitMQ is down when a request is created, the request is saved as `PENDING` and the
  client still gets `201`, but nothing is queued. A sweeper in the matching service finds
  requests that have been `PENDING` for over a minute (checked every 30 s) and matches them
  directly, so such a request is matched about a minute late, not lost. Both numbers are
  settings (`SWEEP_INTERVAL_S`, `PENDING_STALE_AFTER_S`). A full outbox is on the roadmap.
- If freeing a responder fails after its request ends, the responder stays `ASSIGNED`
  (logged, no cleanup job yet).
- Quantity and capacity are stored but not used by the basic matching.

More ideas are in [docs/RESUME_ROADMAP.md](docs/RESUME_ROADMAP.md).
