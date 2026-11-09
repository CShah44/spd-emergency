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
   └───────────┴─────────────┴──► CANCELLED
```

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
| `id` | system | – | `req-` + 16 hex characters (random) |
| `type` | client | yes | One of the fixed types |
| `location.lat` | client | yes | Number, -90 to 90 |
| `location.lon` | client | yes | Number, -180 to 180 |
| `quantity` | client | no | Positive integer. Stored, but the basic matching ignores it (quantities are on the roadmap) |
| `details` | client | no | Free text up to 500 characters. **Never used for matching** |
| `contact` | client | no | Free text |
| `status` | system | – | Starts as `PENDING` |
| `assigned_resource_id` | system | – | Filled when matched |
| `created_at`, `updated_at` | system | – | UTC, ISO 8601 |
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
| `id` | system | – | `res-` + 16 hex characters |
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

## 5. "Only if still free" (stops double assignment)

Every record has a `version` number. A change is saved **only if the version is still the one we read**:

1. Matcher A and matcher B both read `res-3` at **version 4**.
2. A says "save `ASSIGNED`, but only if it's still version 4". That works, and it becomes **version 5**.
3. B says "save `ASSIGNED`, but only if it's still version 4". It's now 5, so this is **refused (409)**. B moves on to the next candidate.

## 6. Where records are stored

- Storage key: `request:<id>` or `resource:<id>`.
- **Shard (cabinet) number** = `int(sha1(key).hexdigest(), 16) % 3`, giving 0, 1 or 2.
  - **Never use Python's built-in `hash()`.** It gives different answers in different programs, so the services would disagree about which cabinet holds a record.
- Each shard has a **primary** and a **replica**, and every write goes to both.

## 7. Message in the tray (RabbitMQ)

The Request Service publishes this when a request is created. The Matching Service consumes it.

```json
{ "request_id": "req-3f9c2a1b7d4e4f0a", "attempt": 1 }
```

- Exchange `emergency` (direct), queue `match.requests` (durable), persistent messages.
- The consumer acks **only after** the request is `MATCHED` or `UNMATCHED`.
- A message that fails 3 times goes to `match.requests.dlq`.

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

---

## 9. Recommended file structure

Owner tags: **[A]** storage side, **[B]** app side, **[Both]** shared. Each person edits only their own folders, which keeps merge conflicts rare.

```
final-project/
├── README.md                          [Both]  build, run, demo walkthrough, design choices
├── docker-compose.yml                 [B]     starts everything locally (A adds the storage entries)
├── requirements-dev.txt               [Both]  pytest, httpx: tools for running tests locally
├── .gitignore
│
├── docs/
│   ├── DESIGN.md                      [Both]  this file: the shared contract
│   ├── BUILD_PLAN.md                  [Both]
│   └── RESUME_ROADMAP.md              [Both]
│
├── services/
│   ├── common/                        [Both]  written together on day 1
│   │   ├── models.py                          record shapes, fixed types, statuses, allowed moves
│   │   ├── sharding.py                        shard_for(key) → 0, 1 or 2 (section 6)
│   │   ├── storage_client.py          [A]     the ONLY way B's services talk to the coordinator (section 8)
│   │   └── config.py                          reads settings from environment variables
│   │
│   ├── storage_node/                  [A]     the cabinet keeper (6 copies run)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py                        /internal/data/{key}, /health
│   │       ├── db.py                          SQLite table + "only if version matches" save
│   │       └── heartbeat.py                   register on startup, then "I'm alive" every 3 s
│   │
│   ├── coordinator/                   [A]     the librarian
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py                        /store API, /internal/nodes/*, /internal/status
│   │       ├── registry.py                    which nodes exist, up or down, heartbeat timeout
│   │       ├── router.py                      key → shard → write primary + replica, read with fallback
│   │       ├── recovery.py                    catch up a node that comes back
│   │       └── fake.py                        in-memory fake coordinator for days 1–2
│   │
│   ├── request_service/               [B]     the front desk
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py                        client REST API (spec section 5)
│   │       ├── schemas.py                     what a valid create/update body looks like
│   │       └── publisher.py                   drops the slip in RabbitMQ
│   │
│   └── matching_service/              [B]     the matchmaker
│       ├── Dockerfile
│       ├── requirements.txt
│       └── app/
│           ├── main.py                        POST /match/{id}, /health
│           ├── consumer.py                    picks slips from RabbitMQ, acks when done
│           ├── matcher.py                     filter → distance → sort → claim (section 4)
│           └── geo.py                         haversine_km(): distance between two map points
│
├── client/                            [B]
│   └── cli.py                                 the "1 or more clients": create, get, register, match
│
├── k8s/
│   ├── namespace.yaml                 [Both]
│   ├── configmap.yaml                 [Both]  shared settings (URLs, timeouts, MAX_MATCH_KM)
│   ├── coordinator.yaml               [A]     Deployment + Service
│   ├── storage-shard0.yaml            [A]     StatefulSet (primary + replica) + headless Service + volumes
│   ├── storage-shard1.yaml            [A]
│   ├── storage-shard2.yaml            [A]
│   ├── rabbitmq.yaml                  [B]     Deployment + Service
│   ├── request-service.yaml           [B]     Deployment (2 replicas) + Service
│   └── matching-service.yaml          [B]     Deployment (2 replicas) + Service
│
├── scripts/
│   ├── seed.py                        [B]     registers sample helpers across a few cities
│   ├── demo_distribution.py           [A]     records spread across the 3 shards
│   ├── demo_storage_failure.py        [A]     stop a node → system keeps working on the replica
│   ├── demo_recovery.py               [A]     restart it → it catches up
│   ├── demo_matching.py               [B]     create, register, match, look up
│   ├── demo_async.py                  [B]     many requests flowing through RabbitMQ
│   ├── demo_k8s_recovery.sh           [B]     kill a pod → Kubernetes restarts it
│   └── demo_k8s_scaling.sh            [B]     scale the matchers up → still available
│
└── tests/
    ├── unit/
    │   ├── test_models.py             [Both]  fixed lists, allowed status moves
    │   ├── test_sharding.py           [A]     same key → same shard, keys spread across all 3
    │   └── test_matcher.py            [B]     right type, nearest first, 50 km limit
    └── integration/                           run against the docker-compose stack
        ├── test_storage_failover.py   [A]
        ├── test_recovery_sync.py      [A]
        ├── test_api.py                [B]     every client endpoint + error codes
        └── test_concurrent_matching.py [B]    many matchers, no double assignment
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
