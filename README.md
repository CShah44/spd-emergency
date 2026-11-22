# S.P.D. Emergency

A distributed emergency-resource matching system. People who need help post a JSON
request, people who can help post a JSON resource, and a matching engine pairs each
request with the nearest suitable, available helper. The name nods to the Power
Rangers S.P.D. morphing call ("S.P.D. Emergency!"); the control room is "Delta Command".

Built for the CS838 cloud course project. The shared data contract is in
[docs/DESIGN.md](docs/DESIGN.md), the build order in [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

## Quick start

Needs Docker Desktop running (the first build takes a few minutes).

```
git clone https://github.com/CShah44/spd-emergency.git
cd spd-emergency
docker compose up --build -d
python scripts/seed.py            # sample responders (needs the Python setup under Prerequisites)
```

Then open **http://localhost:8081** (the Delta Command web page) and **http://localhost:3000**
(the Grafana dashboard). Kubernetes, tests and everything else are below; if something does not
open, see [Troubleshooting](#troubleshooting).

## Architecture

```
  client/cli.py ──HTTP──► Request Service ──publish──► RabbitMQ ──consume──► Matching Service
                          (2 pods)                     (queue)               (1-5 pods)
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
| **Web page** (`ui_service`) | Delta Command: a live dashboard and demo controls; talks to the request service over HTTP | Svelte 5 + Vite page, served by Python 3.12 FastAPI |
| **Matching service** | Takes one message at a time and matches the request; also serves `POST /match/{id}` | Python, FastAPI, pika, httpx, a background consumer thread |
| **Coordinator** | The only door to storage: picks the shard, writes to the leader, copies to the replica, fails over, catches nodes up | Python, FastAPI, async httpx |
| **Storage nodes** (6) | One SQLite file each; versioned records with compare-and-set | Python, FastAPI, SQLite (WAL, `synchronous=FULL`) |
| **Observability** | Every service answers `GET /metrics`; Prometheus collects them, Grafana draws the dashboard | `prometheus-client`, Prometheus, Grafana, RabbitMQ's Prometheus plugin |
| **Autoscaling** | Sets the number of matching pods from the queue length (Kubernetes only) | KEDA, RabbitMQ scaler |
| **Packaging** | One image per service; Compose for a laptop, Kubernetes for the real deployment | Docker, Docker Compose, Kubernetes (Deployments, StatefulSets, Services, ConfigMap) |

### How one request flows

1. `POST /requests` hits a request-service pod. The body is validated (bad input gets a 400).
2. The record is saved through the coordinator: the key picks a shard (by its geohash region for
   new ids, by a hash for older ones), written to
   that shard's leader node, copied to its replica, and only then acknowledged.
3. A `{request_id, attempt}` message is published to RabbitMQ (with publisher confirms) and
   the client gets `201` immediately. The client never waits for matching.
4. A matching-service pod receives the message, finds the nearest free resource of the
   same type within 50 km (looking first in the request's geohash cell and its 8 neighbours,
   so usually only one or two shards are asked), claims it with a version check, and marks the
   request `MATCHED`
   (or `UNMATCHED` if nothing suitable exists). Only then is the message acknowledged.
5. The client reads the result with `GET /requests/{id}`.

## Repository layout

```
services/
  common/             shared: record shapes and status rules, shard rule, geohash cells, StorageClient, config, queue contract, metrics
  storage_node/       one storage node (SQLite + API + heartbeat)
  coordinator/        registry, router, recovery (and an in-memory fake used by the unit tests)
  request_service/    client API (schemas, service rules, RabbitMQ publisher)
  matching_service/   matcher, geo, RabbitMQ consumer, sweeper
  ui_service/         the Delta Command web page (Svelte app in web/, FastAPI server in app/)
client/cli.py         command-line client
k8s/                  Kubernetes manifests (apply with kustomize); observability/ is Prometheus + Grafana, keda/ the autoscaler
observability/        Prometheus config, Grafana data source and dashboard, RabbitMQ plugin list
loadtest/             load generator, the lost-requests check, and the measured results (RESULTS.md)
scripts/              seed data, demos, the Kubernetes start/stop scripts, chaos.py
tests/unit/           fast tests, no network needed
tests/integration/    tests against a live stack
docs/                 design, build plan, resume roadmap
```

## Prerequisites

- Docker Desktop (with Docker Compose)
- Python 3.12 or newer, for the client, demos and tests
- For Kubernetes: `kubectl` and `minikube` (`scripts/k8s_up.py` needs minikube; Docker Desktop's own
  Kubernetes is not used). KEDA is installed by the script and needs internet the first time.

```
python -m venv .venv
.venv/Scripts/activate            # Windows (cmd, Git Bash);  .venv\Scripts\Activate.ps1 in PowerShell
                                  # on Linux/macOS: source .venv/bin/activate
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
| **Grafana dashboard** | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

To run several matchers: `docker compose up -d --scale matching-service=3`.
Stop everything with `docker compose down`.

## Run on Kubernetes

Kubernetes runs through minikube, which starts a whole cluster inside one Docker container.
Install `minikube` and `kubectl` first, and start Docker Desktop. One command does everything:

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

**Autoscaling.** `k8s_up.py` also installs [KEDA](https://keda.sh) (it needs internet the first
time). It watches the length of the RabbitMQ queue and sets the number of matching pods: about one
pod for every 20 requests waiting, never fewer than 1 and never more than 5
(`k8s/keda/matching-scaledobject.yaml`). A load spike takes the matchers from 1 to 5 and, once the
queue is empty, back to 1: `python scripts/demo_k8s_keda.py` runs that and shows it, and the
Grafana panel "Matching pods and the queue" draws it. KEDA owns the pod count, so `kubectl scale`
is undone within seconds; to hold a number, pause KEDA at it (the comments in that file say how).
Docker Compose has no KEDA: there the count is fixed with `--scale`.

## The Delta Command web page

Open **http://localhost:8081** (Compose or Kubernetes). It is live: the server pushes a new
picture to the browser (Server-Sent Events) as soon as anything changes, checking every second.
It has three views; press `1`, `2`, `3` to switch, `?` for the briefing.

**Command** is the console. The five parts of the system each keep an S.P.D. squad number and
color everywhere on the page: 1 red request service, 2 blue coordinator, 3 green storage,
4 yellow queue, 5 pink matcher. A request's color says whose hands it is in: yellow on the
queue, pink once the matcher paired it, cyan while the responder is on the way, green when done.

| Panel | What it shows |
|---|---|
| Mission flow | The five parts in the order a request meets them, each with its live number (requests on file, shards serving, nodes answering, messages waiting, matchers connected). When something happens, the parts it passed through light up one after the other. Click one to read what it does. |
| Dispatch console | Report an emergency, register a responder (city presets match the seed data), or send a stress test of 20 requests. The confirmation says which shard stored the record. |
| Storage vault | The 3 shards and their primary and replica: state, the leader (crown), key counts, whether the two copies are in sync, and what a returning node copied. **Pause** simulates a crash; **Resume** brings the node back. |
| Requests board | A stage bar and counters (PENDING, MATCHED, IN PROGRESS, FULFILLED, UNMATCHED, CANCELLED) that also filter the list; search by id, type, geohash cell or responder. Active requests first, each with its 4-step progress bar, shard, responder and only the buttons the API allows. |
| Comms log | What just happened, each line tagged with the part of the system that did it; filter by kind. |
| Responders | Assigned first, with the request each one serves and its stage; then available, then offline. |
| Patrol grid | Requests (dots) and responders (squares) on the geohash cells the matcher searches; lit cells hold open requests and a pink line joins each matched pair. |

Every panel has a **?** that says what it shows and links to the briefing.

**Telemetry** shows the Grafana dashboard inside the page (time range 5 min to 6 h), with links
to Grafana, Prometheus, the RabbitMQ console and the API docs. Grafana must allow embedding
(`GF_SECURITY_ALLOW_EMBEDDING=true`, set in Compose and `k8s/observability/grafana.yaml`); the
page reaches it at `GRAFANA_URL` (default `http://localhost:3000`, as the browser sees it).

**Briefing** explains each part of the system, every operation step by step (which part does
what, the API call, what to watch on the page and which Grafana panels show it), and three demo
playbooks (failover drill, surge, one request end to end) whose steps jump to the right panel.

**Shard chips.** Every request and responder shows `S0`, `S1` or `S2`: the shard that stores it
(the same rule the coordinator uses: by geohash region for ids that carry a cell, `sha1(key) mod 3` otherwise). When a node of that shard is down the
chip turns orange (`S0 !`: running on one node), and red if the shard has no live node, so a
failure shows on every record it affects.

A single match takes tens of milliseconds, so one request usually flips straight to `MATCHED`
between two refreshes; use the stress test to see the queue work. Deleting pods and scaling
replicas are `kubectl` actions and stay in the terminal; the "matchers connected" count shows
their effect.

The Pause and Resume buttons only work when `DEMO_CONTROLS=true`, which Compose and the
Kubernetes ConfigMap set. It is off by default because anyone who can reach the API could
otherwise take storage nodes down. The page runs as a single pod: it polls the request service
only while a tab is open, and keeps the feed in memory.

**Working on the page.** It is a Svelte 5 app in `services/ui_service/web`, built into
`services/ui_service/app/static` and served by a small FastAPI server
(`services/ui_service/app`). The Docker image builds it; to work on it locally:

```
cd services/ui_service/web && npm ci && npm run build        # once, or after changes
cd .. && uvicorn app.main:app --port 8081                       # with ../ on PYTHONPATH, API_URL set
npm run dev                                                     # optional: hot reload on :5173
```

## Metrics and the Grafana dashboard

Every service answers `GET /metrics` (Prometheus text format), and all metric names start with
`spd_` (they are defined in `services/common/metrics.py`). Prometheus collects them every
5 seconds and Grafana draws them. Open **http://localhost:3000**: the dashboard
"S.P.D. Emergency: Delta Command" is the home page and needs no login (`admin` / `admin` to edit).
Both the data source and the dashboard are files in `observability/grafana/`, so there is
nothing to set up by hand, on Compose or on Kubernetes (where `k8s_up.py` loads the dashboard).

Rows: cluster health, throughput (requests and matches per second), latency (p50 / p95 / p99
from request created to answered), the queue (waiting, matchers listening, dead letters),
replication and recovery, and the coordinator (leader and term). Queue depth comes from RabbitMQ's
own Prometheus plugin (`observability/rabbitmq/enabled_plugins`).

On the storage side, the coordinator's view of the cluster (node states, available shards,
replication lag) is reported by the **leader only**: Prometheus scrapes both coordinator pods,
and a standby removes those series instead of leaving stale ones. Besides the agreed names,
the storage side also reports `spd_replication_lag_writes` (writes a node hasn't received from
its partner, from the operation log), `spd_recovery_total{mode}` (catch-ups from the log vs.
full syncs), `spd_fenced_total` (calls refused for an old coordinator epoch) and
`spd_coordinator_settle_seconds` (a new leader's wait for the nodes).

Start measurements from `--fresh` (Kubernetes) or `docker compose down -v` (Compose): old data
from earlier runs is matched by the sweeper and shows up as very slow requests.

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
| `demo_k8s_keda.py` | A load spike: KEDA takes the matchers from 1 to 5 and back, nothing lost |
| `measure_coordinator_failover.py delete` (or `kill`, `freeze`) | Fail the leading coordinator while a writer runs; counts acknowledged writes lost (`kill` and `freeze` need Docker Desktop's Kubernetes) |
| `bench_catchup.py` | Catch-up by operation log vs. full sync, for different shard sizes |

## Tests

```
python -m pytest                      # unit tests, no stack needed
python -m pytest -m integration       # needs a running Compose or Kubernetes stack
```

For the concurrency test, run the stack with `--scale matching-service=3`.
The integration tests skip themselves when no stack is reachable.

## Load and chaos tests

`loadtest/` sends emergency requests at a steady rate and reports two things: how long each took
to be answered (p50 / p95 / p99, read from `spd_match_e2e_seconds` in Prometheus) and whether
anything was lost (an accepted request that was never answered) or double-assigned. One command
runs a scenario and prints its result row:

```
pip install -r loadtest/requirements.txt
python loadtest/run.py --fresh --rate 10 --duration 60 --label steady --record
python loadtest/run.py --fresh --rate 5 --duration 60 --scenario kill-matcher --record
python scripts/chaos.py list            # the failures it can cause
```

`--fresh` wipes and restarts the Kubernetes stack first: the cost of a match grows with the number
of responders stored, so a row is only comparable when it starts from an empty system (the run
refuses to start on a used one). Scenarios: `kill-matcher`, `kill-rabbitmq`, `kill-storage-leader`,
`pause-storage-node`, `kill-coordinator-leader`. All results are in
[loadtest/RESULTS.md](loadtest/RESULTS.md), with how they were measured and what they do not show.

## What was added after the spec

Six upgrades were planned in [docs/UPGRADE_PLAN.md](docs/UPGRADE_PLAN.md). Each is meant to show
a number. Every number here is from `loadtest/RESULTS.md` (single runs on one laptop, minikube with
4 CPUs and 4 GB, so the size of a gain is indicative).

**Metrics, Prometheus and Grafana.** *Problem:* nothing showed what the system was doing. *Fix:*
every service exposes `/metrics` through one helper (`services/common/metrics.py`); Prometheus and
Grafana run on Compose and Kubernetes, with the dashboard provisioned from files. *Result:* the
latency, queue and shard numbers in this README are read from that dashboard; the storage side adds panels for replication lag, catch-ups, fencing and coordinator takeovers.

**Load and chaos tests.** *Problem:* "it is resilient" was an opinion. *Fix:* a load generator, an
independent check that every accepted request was answered and no responder was given twice, and
scripted failures under load. *Result:* nothing lost and nothing double-assigned in the eight
scenarios, which include crashing a matcher, the broker, a storage leader and the coordinator
leader. With two matchers and no autoscaling, capacity was between 5 and 10 requests per second.

**Geo-sharding.** *Problem:* records were placed by a hash, so "free water near
here" asked all three shards every time. *Fix:* new ids carry a geohash cell; the matcher looks
in the request's cell and its 8 neighbours first and asks every shard only if that finds nobody
(`docs/GEO_SHARDING.md`). The storage side places each record by the first two characters of its
cell (a region) on a consistent-hash ring and answers `cells=` queries from only the shards that own
those cells, so a search near Mumbai keeps working while the shard that owns Chennai is down.
*Result:* shards asked per
query went from 3 to 1.66 on average. The price is a hot spot: after a run one shard held 56% of
the keys. The latency gain from geo alone is not isolated: the same configuration measured a
median of 3.1 s in one run and 7.8 s in another.

**KEDA autoscaling.** *Problem:* a fixed number of matchers cannot follow a load spike. *Fix:* KEDA
sets the number of matching pods from the queue length (about one per 20 waiting requests, 1 to
5). *Result:* the matchers went 1 to 5 and back to 1 through a 20 requests-per-second spike with
nothing lost; the median wait was 89 s with two fixed matchers and 10.4 s with KEDA (one run each).

**Coordinator HA with fencing.** *Problem:* one coordinator pod handled every read and write, so
if it died everything stopped until Kubernetes restarted it. *Fix:* two coordinators elect a leader
through a Kubernetes Lease; only the leader is ready, and every call to a storage node carries the
leader's epoch so a leader that froze and woke up is refused. *Result:* with a writer saving a record
every 50 ms and the leader failed on purpose, **0 acknowledged writes were lost** in all three
cases, and the longest gap seen by the client was 3.2 s (leader deleted), 9.1 s (crashed) and 12.2 s
(frozen for 10 s). Measured on Docker Desktop's Kubernetes, not minikube
([docs/COORDINATOR_FAILOVER.md](docs/COORDINATOR_FAILOVER.md)).

**Operation log for catch-up.** *Problem:* a node coming back compared its whole data set with its
partner's. *Fix:* each node numbers its writes, and a returning node asks only for what it missed;
it falls back to the full comparison if the logs don't line up. *Result:* the log reads exactly the
writes that were missed (10 entries instead of 40,000 on a 20,000-record shard), and was about 5 to
25 times faster at 10,000 records or more. These are single, noisy runs on an in-process test
cluster ([docs/CATCHUP_BENCHMARK.md](docs/CATCHUP_BENCHMARK.md), `scripts/bench_catchup.py`).

The same scenarios before and after (the final system also has KEDA, so the matcher count is not
fixed at 2 as in the baseline, and the gains come from several changes together):

| Scenario | Baseline | Final |
|---|---|---|
| Steady 5 req/s, p99 | 353 ms | 100 ms |
| Steady 10 req/s, p99 | 4.82 s | 247 ms |
| Overload 20 req/s, median / p99 | 89 s / over 120 s | 7.8 s / 28.7 s |
| Crash a matcher, recovery | 8.3 s | 1.8 s |
| Crash RabbitMQ, p95 / p99 | 85 s / 113 s | 85 s / 113 s |
| Lost or double-assigned requests | 0 | 0 |

## Troubleshooting

- **The web page does not open at http://localhost:8081 after pulling new code.** Images and
  port-forwards are built from the code you had: rebuild with `docker compose up --build -d`, or
  run `python scripts/k8s_up.py` again (it also restarts an old port-forward, which would not
  know about new ports). Check `docker compose ps` or `kubectl -n emergency get pods`, and
  `curl localhost:8081/health` (it should answer `{"status":"ok"}`).
- **"port is already allocated" or "local ports already in use".** Docker Compose and the
  Kubernetes port-forward use the same ports, so run only one: `docker compose down` first.
- **`k8s_up.py` says minikube or kubectl is missing.** Install both, and start Docker Desktop.
  The first run builds every image and installs KEDA, which takes several minutes.
- **The page opens but shows the system as unreachable.** An old request-service image is
  probably running: rebuild, as above.
- **Everything is slow, or a load-test row refuses to start.** Records from earlier runs stay
  stored (and old `PENDING` requests get matched late). Start clean with
  `python scripts/k8s_up.py --fresh` (Kubernetes) or `docker compose down -v` (Compose).
- **`kubectl scale` on the matchers has no lasting effect.** KEDA owns the count; see the
  Autoscaling note under Kubernetes.

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
- **Autoscaling on queue length, not CPU.** The work is waiting messages, so KEDA scales the
  matchers on the length of `match.requests` (about 20 per pod, 1 to 5). Scale-down waits a minute so
  the pods don't flap.
- **Three attempts, then a dead-letter queue.** A message that keeps failing is parked, not
  retried forever. A storage outage does not use up an attempt, because it is not the
  message's fault.

**Storage**
- **Hash sharding (SHA-1 of the key, mod 3).** Even spread, and every service computes the
  same answer. Python's built-in `hash()` is avoided because it differs between processes.
- **Geo-sharding: ids carry their geohash cell.** Hashing records apart means "what is free near
  here" must ask every shard. A new id is `res-<cell>-<hex>`, and the cell's first two characters
  (a region) are placed on a consistent-hash ring, so nearby records share a shard and the matcher
  searches only the request's cell and its 8 neighbours (which cover the 50 km limit). It falls
  back to every shard if that finds nobody, and goes straight to every shard when the radius is
  too big for nine cells to be sure to cover it. Ids without a cell still work, so nothing
  stored has to move. `GEO_SHARDING=false` turns it off. The trade-off is a hot spot: a busy
  region lands on one shard.
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
- **Two coordinators on Kubernetes, one leader, with fencing.** They elect a leader through a
  Kubernetes Lease and the Service routes to the leader's pod. If the leader dies, the
  standby takes over, waits for the storage nodes to check in, and puts the freshest node of
  each shard in charge before serving. Every call to a storage node carries the leader's
  epoch, and nodes refuse an older one, so a leader that froze and woke up can't change data
  (split brain). Details in DESIGN.md section 8.3. Measured on Kubernetes: 0 acknowledged
  writes lost when the leader is deleted (3.2 s outage), crashes (9.1 s) or freezes (12.2 s);
  see docs/COORDINATOR_FAILOVER.md.
- **Catch-up reads an operation log, not the whole data set.** Each node numbers its own
  writes, and each node remembers how far it has received its partner's. A returning node
  fetches only the writes after that point, so catching up depends on how long it was gone,
  not on how much data the shard holds (`scripts/bench_catchup.py`). If the two nodes have
  never synced, or a disk was wiped, it falls back to comparing everything.
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
- **The web page is its own small service.** Its server uses only the public REST API (plus two read-only calls and the demo controls), so it can be removed without touching anything else. The page is Svelte (compiled, about 50 KB of JavaScript gzipped, fonts bundled so it works offline); everything it knows about the system (shards, allowed moves, the feed) is computed in Python and unit-tested.
- **The load test checks the system from outside.** It saves the id of every request the API accepted
  and later asks about each one, so a lost request cannot hide behind the system's own counters.
  Latency is measured inside the matcher from a precise creation time (`created_ts`), because
  `created_at` only has whole seconds.
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
- Geo-sharding concentrates a busy region on one shard (56% of the keys in one measured run), and
  a match asks 1.66 shards on average, not exactly one. Records from before geo-sharding are only
  found on the shards a search asks, so mixed old and new data is not optimal (start clean).
- A match gets slower as more responders are stored: each storage node scans every responder it
  holds and filters by type and status as it reads them. Nothing is indexed by type or status.
- Killing RabbitMQ loses no request, but those requests are only matched when the sweeper finds
  them (about a minute and a half), so their latency is 85 to 113 s (p95 / p99).
- An `UNMATCHED` request is not retried by itself; a retry is a client call.
- The numbers in `loadtest/RESULTS.md` are single runs on one laptop, and recovery times are
  approximate (the queue's consumer count has 5 s resolution).
- Not built: moving data when a shard is added (the ring only decides where a region would go),
  and asking only the cells that come within 50 km of the request instead of all nine. The
  second is a recommendation in `docs/GEO_SHARDING.md`: a simulation on the five cities puts it at
  about 1.2 shards per match instead of about 1.6.
- A coordinator failover takes 3 to 12 s (see above); writes in that gap are refused or time
  out, never acknowledged, so the request service's callers retry them.

More ideas are in [docs/RESUME_ROADMAP.md](docs/RESUME_ROADMAP.md).
