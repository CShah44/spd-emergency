# Design: Data Shapes and Shared Rules

Both teammates build against this file, so **change it only by agreement**. Every request and every helper is stored in one fixed JSON shape, and matching works only on the fixed fields. Free text is never interpreted.

---

## 1. Fixed lists

**Types:** what a request needs, or what a resource offers. Both sides use the same list.

| Value | Meaning |
|---|---|
| `water` | Drinking water |
| `food` | Food or meals |
| `medicine` | Medicine or medical help |
| `transport` | Vehicle or evacuation |
| `shelter` | A place to stay |

Any other value is rejected with **400**.

**Request statuses**, and the only allowed moves between them:

```
PENDING ──► MATCHED ──► IN_PROGRESS ──► FULFILLED
   │           │             │
   ├──► UNMATCHED (nothing suitable found; can go back to PENDING for a retry)
   │    │
   └────┴──────┴─────────────┴──► CANCELLED
```

`UNMATCHED → CANCELLED` is allowed (the person gives up waiting).

| Status | Plain meaning |
|---|---|
| `PENDING` | Saved, waiting to be matched |
| `MATCHED` | A helper has been assigned |
| `IN_PROGRESS` | The helper is on the way |
| `FULFILLED` | Help was delivered (final) |
| `UNMATCHED` | No suitable helper was found |
| `CANCELLED` | Called off (final) |

**Resource statuses:**

| Status | Plain meaning |
|---|---|
| `AVAILABLE` | Free and can be matched |
| `ASSIGNED` | Promised to a request |
| `OFFLINE` | Temporarily not offering help |

`ASSIGNED → AVAILABLE` happens when its request is `FULFILLED` or `CANCELLED`.

Allowed resource moves: `AVAILABLE → ASSIGNED`, `AVAILABLE → OFFLINE`, `OFFLINE → AVAILABLE`, `ASSIGNED → AVAILABLE`. `ASSIGNED → OFFLINE` is **not** allowed directly: the resource must be released first, so no request is left stranded.

Any status change that isn't allowed above is rejected with **409**.

---

## 2. Request (someone who needs help)

```json
{
  "id": "req-3f9c2a1b7d4e4f0a",
  "type": "water",
  "location": { "lat": 12.9716, "lon": 77.5946 },
  "quantity": 20,
  "details": "2 kids, ground floor flooded",
  "contact": "+91-90000-00000",
  "status": "PENDING",
  "assigned_resource_id": null,
  "created_at": "2026-11-09T10:15:00Z",
  "updated_at": "2026-11-09T10:15:00Z",
  "version": 1
}
```

| Field | Set by | Required on create | Rules |
|---|---|---|---|
| `id` | system | – | `req-` + 16 hex characters (random). With geo-sharding (the default) it is `req-<cell>-<16 hex>`, where `<cell>` is the 3-character geohash of the request's location (section 6). Ids without a cell still work |
| `type` | client | yes | One of the fixed types |
| `location.lat` | client | yes | Number, -90 to 90 |
| `location.lon` | client | yes | Number, -180 to 180 |
| `quantity` | client | no | Positive integer. Stored, but the basic matching ignores it (quantities are on the roadmap) |
| `details` | client | no | Free text up to 500 characters. **Never used for matching** |
| `contact` | client | no | Free text |
| `status` | system | – | Starts as `PENDING` |
| `assigned_resource_id` | system | – | Filled when matched |
| `created_at`, `updated_at` | system | – | UTC, ISO 8601 |
| `created_ts` | system | – | When the request was created, in seconds since 1970 with fractions. `created_at` only has whole seconds, so the matcher times itself against this (`spd_match_e2e_seconds`). Records from before it was added don't have it |
| `version` | system | – | Starts at 1, goes up by 1 on every change (see section 5) |

## 3. Resource (someone who can help)

```json
{
  "id": "res-8b1e0c55a2f94d17",
  "type": "water",
  "name": "City Water Tanker 4",
  "location": { "lat": 12.9900, "lon": 77.6000 },
  "capacity": 500,
  "availability": "24x7",
  "contact": "+91-90000-00001",
  "status": "AVAILABLE",
  "assigned_request_id": null,
  "created_at": "2026-11-09T09:00:00Z",
  "updated_at": "2026-11-09T09:00:00Z",
  "version": 1
}
```

| Field | Set by | Required on create | Rules |
|---|---|---|---|
| `id` | system | – | `res-` + 16 hex characters, or `res-<cell>-<16 hex>` with geo-sharding (the cell of the resource's location; see section 6) |
| `type` | client | yes | One of the fixed types |
| `name` | client | yes | Who or what this is (a volunteer, NGO, truck…) |
| `location.lat` / `.lon` | client | yes | Same rules as for requests |
| `capacity` | client | no | Positive integer. Stored, not used by the basic matching |
| `availability` | client | no | Free text, e.g. "24x7" or "evenings" |
| `contact` | client | no | Free text |
| `status` | system | – | Starts as `AVAILABLE` |
| `assigned_request_id` | system | – | Filled when assigned |
| `created_at`, `updated_at`, `version` | system | – | Same as for requests |

---

## 4. Matching rule

For one request:
1. **Filter:** resources with the **same `type`** and `status == AVAILABLE`.
2. **Distance:** compute the straight-line distance on the globe from the request to each candidate (haversine formula, in km).
3. **Drop** candidates further than **50 km** (`MAX_MATCH_KM`, configurable).
4. **Sort:** nearest first. On a tie, the resource registered earliest (`created_at`) goes first.
5. **Claim** the first candidate, *only if it is still free* (section 5). If someone else got it first, try the next.
6. On success: resource → `ASSIGNED` with `assigned_request_id`, and request → `MATCHED` with `assigned_resource_id`.
7. If no candidate is left: request → `UNMATCHED`.

**Where the candidates are looked for (geo-sharding, `GEO_SHARDING`, on by default).** Step 1 first asks only the shards that own the request's geohash cell and its 8 neighbours. Together they cover the 50 km limit, so a match usually touches one or two shards instead of three. If nobody in those cells can be claimed, the matcher asks every shard once more (this is also what finds records from before geo-sharding). When the radius is too big for nine cells to be sure to cover it (a large `MAX_MATCH_KM`, or far north, where cells are narrow: `geohash.cells_cover`), it asks every shard directly. The lookup for "is a responder already assigned to this request" uses the same cells. With `GEO_SHARDING=false` every query asks every shard, as before.

Matching is triggered when a request is created, when it is retried (`UNMATCHED` to `PENDING`) and by `POST /match/{id}`. An `UNMATCHED` request is not retried by itself.

## 5. "Only if still free" (stops double assignment)

Every record has a `version` number. A change is saved **only if the version is still the one we read**:

1. Matcher A and matcher B both read `res-3` at **version 4**.
2. A says "save `ASSIGNED`, but only if it's still version 4". That works, and it becomes **version 5**.
3. B says "save `ASSIGNED`, but only if it's still version 4". It's now 5, so this is **refused (409)**. B moves on to the next candidate.

## 6. Where records are stored

- Storage key: `request:<id>` or `resource:<id>`.
- **Shard (cabinet) number** = `int(sha1(key).hexdigest(), 16) % 3`, giving 0, 1 or 2.
  - **Never use Python's built-in `hash()`.** It gives different answers in different programs, so the services would disagree about which cabinet holds a record.
- **Geo-sharding (roadmap task 5, docs/GEO_SHARDING.md).** An id that carries a geohash cell (`res-<cell>-<hex>`) is placed by its **region**, the cell's first 2 characters, on a consistent-hash ring (64 spots per shard), so nearby records share a shard. Ids without a cell keep the rule above. `GET /store?cells=tdr,tf2` asks only the shards owning those cells, and each returns only records in them (plus old-style records it holds).
- Each shard has a **primary** and a **replica**, and every write goes to both.

## 7. Message in the tray (RabbitMQ)

The Request Service publishes this when a request is created. The Matching Service consumes it.

```json
{ "request_id": "req-3f9c2a1b7d4e4f0a", "attempt": 1 }
```

- Exchange `emergency` (direct), queue `match.requests` (durable), persistent messages.
- The consumer acks **only after** the request is `MATCHED` or `UNMATCHED`.
- A message that fails 3 times goes to `match.requests.dlq`.
- A request is saved before it is queued, so a broker outage can leave one saved but never queued. A **sweeper** in every matching pod (every `SWEEP_INTERVAL_S` = 30 s) matches requests that have been `PENDING` for more than `PENDING_STALE_AFTER_S` = 60 s. Matching is safe to repeat, so it can run next to the consumer.
- The number of matching pods is set by KEDA from the queue length (1 to 5, about one per 20 waiting requests; `k8s/keda/`). Compose has no KEDA and uses a fixed `--scale`.

---

## 8. Coordinator API: the handoff between the two halves

This is the **contract between Person A (storage side) and Person B (app side)**. Person B's services talk to storage only through these calls. Person A ships an in-memory fake with these exact calls on day 1–2, so Person B isn't blocked.

| Call | What it does | Answers |
|---|---|---|
| `PUT /store/{key}` (body: the record) | Create or overwrite a record | 200, with the saved record including its new `version` |
| `PUT /store/{key}` + header `If-Match: <version>` | Save only if the version still matches | 200, or **409** if it changed |
| `GET /store/{key}` | Fetch one record | 200, or **404** |
| `DELETE /store/{key}` | Delete a record | 204, or **404** |
| `GET /store?prefix=resource:&type=water&status=AVAILABLE` | Search all shards and merge the results | 200, a list (may be empty) |
| `GET /internal/status` | Every storage node: shard, role, up/down, last heartbeat | 200 (for demos) |

The node-side calls in the spec (`/internal/nodes/register`, `/internal/nodes/heartbeat`, `/internal/data/{key}`) sit **behind** the coordinator and belong entirely to Person A.

If the right shard has no live node at all, the coordinator answers **503**.

### 8.1 Behind the coordinator (Person A only)

Storage node API. Only the coordinator calls these.

| Call | What it does | Answers |
|---|---|---|
| `PUT /internal/data/{key}` (+ optional `If-Match`) | Client write on the acting primary; the node picks the next version | 200 record, 404, 409 |
| `GET /internal/data/{key}` | One live record | 200, 404 |
| `DELETE /internal/data/{key}` | Leaves a **tombstone** (`deleted=1`, new version) | 200 `{key, version, deleted}`, 404 |
| `GET /internal/data?prefix=&<field>=` | Search live records on this node | 200 list |
| `PUT /internal/replicate/{key}` body `{version, deleted, record, origin?}` | Store exactly this version **only if newer** than what the node holds. `origin {log_id, seq}` says where the write sits in the writing node's log | 200 `{applied, version}` |
| `POST /internal/replicate-batch` body `{entries, origin?}` | The same for many entries in one transaction (recovery). `origin {log_id, through}` moves the watermark | 200 `{received, applied, conflicts}` |
| `GET /internal/sync?prefix=` | Every entry including tombstones, for a full sync | 200 `{node_id, entries}` |
| `GET /internal/oplog/position` | This node's log id and head seq, and its watermark for each partner log | 200 |
| `GET /internal/oplog?after=N&limit=` | This node's own writes after seq N, each with the key's current value | 200 `{log_id, head, entries}` |
| `GET /health` | Identity and key count; 503 while paused | 200 / 503 |
| `POST /admin/pause`, `/admin/resume` | Simulate a failure (demos) | 200 |

Node → coordinator: `POST /internal/nodes/register {node_id, shard, role, url}` on startup and after a pause, then `POST /internal/nodes/heartbeat {node_id}` every 3 s. A 404 to a heartbeat means "register again".

Client writes answer with `X-Log-Id` and `X-Log-Seq` headers; the coordinator passes them on as `origin` with the replica copy.

Rules: versions keep counting past a tombstone (deleted v3 → recreated v4). Replication and recovery both use "newer version wins", never timestamps, because container clocks can disagree.

### 8.2 Node states, leadership and recovery

| State | Serves reads/writes | Gets replicated writes |
|---|---|---|
| `UP` | yes | yes |
| `RECOVERING` | no | yes |
| `DOWN` | no | no |

- A node that registers while its partner is `UP` starts `RECOVERING`. The two nodes catch up in both directions, then it is marked `UP`. With no `UP` partner, it starts `UP` on its own data.
- **Operation log (normal catch-up).** Every client write on a node gets the next sequence number (seq) in that node's log; the log keeps each key's latest seq. Each node also keeps a **watermark** per partner log: "I have every write of that log up to seq N". It only moves over seqs actually received, so a missed write holds it back and is fetched next time. Catch-up sends each side only its own writes after the other's watermark (`/internal/oplog?after=N`), so the work follows how long the node was gone, not the size of the data.
- **Full sync (fallback).** Compare both nodes' `/internal/sync` and copy each side's newer entries. Used when the two have no watermark for each other's current log: the first sync of a new cluster, a node whose disk was wiped (a new database gets a new log id), or `RECOVERY_MODE=full` on the coordinator. It sets the watermarks, so the next catch-up uses the log.
- `/internal/status` shows each node's last catch-up: `mode` (`log` or `full`, with `why_full`), `pulled`, `pushed`, `entries_read`, `conflicts`, `seconds`. Benchmark: `python scripts/bench_catchup.py`.
- A `DOWN` node's heartbeats get 404, so it must register again, which means it always catches up before serving again.
- **Leadership is sticky:** the leader keeps the shard while it is `UP`. After a failover the replica keeps leading even when the old primary is back. Leadership moves only when the leader fails, so two nodes never accept compare-and-set writes for the same shard at once.

### 8.3 Two coordinators: leader election and fencing

With `LEADER_ELECTION=kubernetes` (the Kubernetes manifests) two coordinator pods run; with `off` (Docker Compose, the default) there is one, as before.

- **Election:** the pods compete for a Kubernetes Lease (`coordinator-leader`). The leader renews it every second; if it can't for `RENEW_DEADLINE_S` (4 s) it stops acting as leader, before the lease (`LEASE_DURATION_S`, 6 s) runs out. A standby takes over when it has seen no renewal for a full lease duration, or at once if the leader released it on a clean shutdown. Expiry is judged by the standby's own clock, so the pods' clocks needn't agree.
- **Routing:** the leader labels its pod `coordinator-role=leader` and the Service selects that label. Both pods pass the readiness probe, so rolling updates work. A standby answers 503 to anything but `/health`, `/livez`, `/ready` (200 on the leader only) and `/internal/leader`.
- **Settling after a takeover:** the registry is in memory, so a new leader starts empty. Nodes get 404 on their next heartbeat and register again, sending their operation-log position. For up to `2 × HEARTBEAT_INTERVAL_S + 1` s (or until all six are back) the leader only collects registrations and answers `/store` with 503. Then, per shard, it admits the **freshest** node first (it leads) and the other second (it catches up). Node X is at least as fresh as Y when X's watermark for Y's log reaches Y's head; if both or neither are, the configured primary leads.
- **Fencing:** the epoch is the Lease's `leaseTransitions`, +1 at every handover. Every coordinator → node call carries `X-Coordinator-Epoch`; nodes also adopt the epoch from the register and heartbeat answers. A node stores the highest epoch it has seen (on disk) and answers `409 {"error": "fenced"}` to an older one. A coordinator that gets that answer steps down at once. So a leader that froze, lost the lease, and woke up can't change data.
- `/internal/status` → `coordinator`: who leads, the epoch, whether it is settling, and the last takeover (how long settling took and which node leads each shard).
- Each process competes as `<pod>_<random suffix>`, so a container that crashed and restarted in the same pod is a new candidate and, if it wins, gets a new epoch.
- **Measured** (docs/COORDINATOR_FAILOVER.md, `scripts/measure_coordinator_failover.py`): 0 acknowledged writes lost; client-visible outage 3.2 s (pod deleted), 9.1 s (crash), 12.2 s (frozen 10 s).
- **Limit:** `kubectl port-forward svc/coordinator` (used by `scripts/k8s_forward.py`) attaches to one pod. If leadership moves while that pod stays alive, `localhost:8000` reaches the standby (503) until the forward restarts. Traffic inside the cluster always follows the Service.

---

## 9. File structure (as built)

Owner tags: **[A]** storage side, **[B]** app side, **[Both]** shared. Each person edits only their own folders, which keeps merge conflicts rare.

```
spd-emergency/
├── README.md                          [Both]  build, run, demo walkthrough, design choices
├── docker-compose.yml                 [B]     starts everything locally (A owns the storage entries)
├── requirements-dev.txt                       pytest, httpx, fastapi, pika, prometheus-client
├── pytest.ini
│
├── docs/
│   ├── DESIGN.md                      [Both]  this file: the shared contract
│   ├── BUILD_PLAN.md, RESUME_ROADMAP.md, UPGRADE_PLAN.md   the plans
│   ├── GEO_SHARDING.md                [Both]  ids that carry a cell, regions on a ring, cells= queries
│   ├── COORDINATOR_FAILOVER.md        [A]     measured failover of the two coordinators
│   └── CATCHUP_BENCHMARK.md           [A]     operation log vs. full sync
│
├── services/
│   ├── common/                        [Both]
│   │   ├── models.py                          record shapes, fixed types, statuses, allowed moves, id generation
│   │   ├── sharding.py                [A]     shard_for(key): by region for geo ids, sha1 mod 3 otherwise (section 6)
│   │   ├── geohash.py                 [B]     encode, neighbours, cells_around, cells_cover, cell_of_id
│   │   ├── storage_client.py          [A]     the ONLY way B's services talk to the coordinator (section 8)
│   │   ├── messaging.py               [B]     the RabbitMQ queue contract (section 7)
│   │   ├── metrics.py                 [Both]  every spd_* metric, and instrument(app) for GET /metrics
│   │   └── config.py                          settings from environment variables
│   │
│   ├── storage_node/                  [A]     one SQLite file per node (6 run)
│   │   └── app/   main.py, db.py (versioned records + operation log), heartbeat.py
│   │
│   ├── coordinator/                   [A]     the librarian (2 pods on Kubernetes, one leader)
│   │   └── app/   main.py, registry.py, router.py, recovery.py, election.py + leadership.py
│   │              (Lease election and fencing), state_metrics.py, fake.py (in-memory, used by the unit tests)
│   │
│   ├── request_service/               [B]     the front desk
│   │   └── app/   main.py (client API + the extra calls the web page needs), schemas.py, service.py,
│   │              publisher.py (RabbitMQ, with queue statistics), cluster.py (storage view, demo controls)
│   │
│   ├── matching_service/              [B]     the matchmaker
│   │   └── app/   main.py (POST /match/{id}, /health), consumer.py, matcher.py, geo.py (haversine),
│   │              sweeper.py (matches requests that were saved but never queued)
│   │
│   └── ui_service/                    [B]     the Delta Command web page
│       ├── app/   FastAPI server: serves the page, streams the system's state (SSE), passes actions on
│       └── web/   the Svelte app, built into the image
│
├── client/cli.py                      [B]     create, get, register, match from the command line
│
├── k8s/
│   ├── namespace.yaml, configmap.yaml, kustomization.yaml   [Both]
│   ├── coordinator.yaml, storage-shard0..2.yaml             [A]     coordinators + election RBAC; StatefulSets
│   ├── rabbitmq.yaml                  [B]     StatefulSet with its own disk, plus the plugin list
│   ├── request-service.yaml, matching-service.yaml, ui-service.yaml   [B]
│   ├── observability/                 [B]     Prometheus (finds pods itself) and Grafana
│   └── keda/                          [B]     the ScaledObject for the matchers (applied after KEDA is installed)
│
├── observability/                     [B]     Prometheus config for Compose, Grafana data source and dashboard
├── loadtest/                          [B]     Locust load, verify.py (lost / double-assigned), run.py, RESULTS.md
│
├── scripts/
│   ├── k8s_up.py, k8s_down.py, k8s_forward.py, _k8s.py      [B]     one-command Kubernetes start and stop
│   ├── seed.py, demo_matching.py, demo_async.py             [B]
│   ├── demo_k8s_recovery.py, demo_k8s_scaling.py, demo_k8s_keda.py   [B]
│   ├── chaos.py                       [Both]  crash something under load and time the recovery
│   ├── demo_distribution.py, demo_storage_failure.py, demo_recovery.py   [A]
│   └── bench_catchup.py, measure_coordinator_failover.py    [A]
│
└── tests/
    ├── unit/                                  fast, no network: matcher, consumer, sweeper, request API,
    │                                          geo matching, metrics, load-test verdict, UI server and model [B];
    │                                          sharding, storage, coordinator, failover, op log, geo routing [A]
    └── integration/                           run against a live Compose or Kubernetes stack
```

Notes:
- **Docker build context = the repo root**, so each Dockerfile can copy `services/common/` next to its own `app/`.
- `common/storage_client.py` is owned by **A** because it wraps A's API. B only *calls* it, and asks A for any change.

## 10. Work allocation

### Who owns which part of the system

```
                ┌──────────────────────────── PERSON B · app side ────────────────────────────┐
                │                                                                              │
   client/cli.py ──►  Request Service ──publish──►  RabbitMQ  ──consume──►  Matching Service   │
                │        (front desk)                (tray)                  (matchmaker)       │
                │             │                                                   │            │
                └─────────────┼───────────────────────────────────────────────────┼────────────┘
                              │                                                   │
                              │     common/storage_client.py  =  section 8        │
                              ▼          (the contract between A and B)           ▼
                ┌─────────────────────────── PERSON A · storage side ──────────────────────────┐
                │                                                                              │
                │                            Coordinator (librarian)                           │
                │                 heartbeats · shard rule · failover · catch-up                │
                │             ┌──────────────────────┼──────────────────────┐                  │
                │             ▼                      ▼                      ▼                  │
                │          Shard 0                Shard 1                Shard 2               │
                │     primary + replica      primary + replica      primary + replica          │
                │                                                                              │
                └──────────────────────────────────────────────────────────────────────────────┘

   SHARED [Both]:  services/common/  ·  DESIGN.md  ·  README.md  ·  k8s/configmap.yaml  ·  final demo
```

### Who does what, when (about 3 weeks)

```
             WEEK 1                          WEEK 2                          WEEK 3
          ────────────────────────────────────────────────────────────────────────────────────
 BOTH     [common/ + DESIGN]                                                 [README · rehearse demo]

 A        [fake coord] [storage node] [real coordinator] [failover · catch-up] [k8s storage · A demos]

 B        [request svc · RabbitMQ] [matching]  [compose · concurrent matching] [k8s apps · B demos]
          ────────────────────────────────────────────────────────────────────────────────────
                                 ▲                              ▲                        ▲
                       SYNC 1: swap fake for       SYNC 2: all failures        SYNC 3: full demo
                       real coordinator; full      handled in Compose          on Kubernetes,
                       flow runs in Compose                                    end to end
```

### Spec requirements by owner

| Spec item | Owner |
|---|---|
| Create / retrieve request, register resource, status updates (spec §5) | B |
| Asynchronous processing through RabbitMQ (§7) | B |
| Matching on type + location (§7) | B |
| 3 shards, hash-based routing, a replica per shard (§4) | A |
| Heartbeats, failure detection, use the replica, catch-up on recovery (§8) | A |
| Dockerfiles (§9.1) | Each for their own services |
| docker-compose | B (A adds the storage entries) |
| Kubernetes Deployments, Services, pod replicas, scaling, pod recovery (§9.2) | B (apps), A (storage + coordinator) |
| Demos: distribution, storage failure, node recovery (§10) | A |
| Demos: request, register, match, async, Kubernetes recovery, Kubernetes scaling (§10) | B |
| Testing (§11) | Each for their half |

### Upgrades after the spec (docs/UPGRADE_PLAN.md)

| # | Task | Owner | Where |
|---|---|---|---|
| 1 | Metrics, Prometheus, Grafana | A (coordinator, storage nodes), B (app services, the stack) | `common/metrics.py`, `observability/`, `k8s/observability/` |
| 2 | Load and chaos tests | B (load, verifier, matcher and broker scenarios), A (storage and coordinator scenarios) | `loadtest/`, `scripts/chaos.py` |
| 3 | Two coordinators, leader election, fencing | A | section 8.3 |
| 4 | Operation log for catch-up | A | section 8.2 |
| 5 | Geo-sharding | A (regions, `cells=` routing), B (ids with a cell, cell search in the matcher) | sections 2, 3, 4, 6; `docs/GEO_SHARDING.md` |
| 6 | KEDA autoscaling on queue length | B | section 7; `k8s/keda/` |

The measured results of all of them are in `loadtest/RESULTS.md`.
