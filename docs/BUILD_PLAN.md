# Emergency Matching: Build Plan (Course Spec)

The goal is the **full course spec working and demo-able** before any resume upgrades (those are in `RESUME_ROADMAP.md`). Build from the bottom up: storage → coordinator → request service → RabbitMQ → matching service → Docker Compose → Kubernetes. Every phase ends with something you can run and check.

---

## Phase 0: Setup (day 1)

1. **Tools.** Docker Desktop and `kubectl` are already installed on this machine. Turn on Kubernetes in **Docker Desktop → Settings → Kubernetes → Enable**. Images built locally then work in the cluster without a registry (`imagePullPolicy: IfNotPresent`), so minikube and kind aren't needed. Check with `kubectl get nodes`, which should show `docker-desktop Ready`.
2. **Python.** Use `python:3.12-slim` in the containers. Local Python is 3.14, which some libraries may not support yet, so use a 3.12 venv locally if anything fails to install.
3. **Git.** Run `git init`, create a private GitHub repo, and add the team. Add a `.gitignore` covering `__pycache__/`, `.venv/`, `*.db`, `.env`, and `slides-reference/` (course material, don't publish it).
4. **Decide the team split** (see the end of this file) and agree on the design decisions in Phase 1 **before anyone writes service code**.

## Phase 1: Lock the design (day 1–2)

Write each decision into `docs/DESIGN.md` so everyone builds against the same contracts.

| Decision | Recommended choice | Why |
|---|---|---|
| Stack | **FastAPI + httpx + pika** (pika from Assignment 3) | Fast to write, free Swagger UI at `/docs` for demos |
| IDs | UUID4 strings, keys namespaced `request:<id>` and `resource:<id>` | One key-value space; type is visible in the key |
| Shard function | `int(sha1(key).hexdigest(), 16) % 3` | **Never Python's built-in `hash()`.** It is randomised per process, so two services would disagree on the shard |
| Storage per node | SQLite file on a persistent volume: table `kv(key, value_json, version, updated_at)` | Simple, durable, and `version` enables compare-and-set |
| Replication | Coordinator writes to the **primary and then the replica** of the shard. Reads use the primary, falling back to the replica | Simplest scheme that meets the spec |
| Recovery sync | A recovering node pulls changed keys from its partner (`GET /internal/data?since=<ts>`) **before** being marked healthy | Meets "synchronize when a failed node recovers" |
| Matching search | Coordinator `GET /internal/query?type=water&status=AVAILABLE` queries all 3 shards and merges | Needed because resources are sharded by ID |
| Matching rule | Same type + `AVAILABLE` + nearest by haversine distance | Spec needs type and location; nearest is easy to justify |
| Claiming | `PUT /internal/data/{key}` with `If-Match: <version>`; returns 409 on conflict, then try the next candidate | Prevents double assignment once matching scales. Cheap to add from day 1 |
| Queue topology | Durable direct exchange `emergency`, durable queue `match.requests`, persistent messages, manual ack, `prefetch=1`, DLQ `match.requests.dlq` | No lost messages, fair spread across scaled consumers |
| `POST /match/{id}` | Request service calls the matching service over HTTP and returns the result synchronously. The automatic path goes through RabbitMQ | Spec has both a manual endpoint and an async flow; both share one `match()` function |
| Heartbeats | Every 3 s; node marked DOWN after 10 s without one | Fast enough to show during a demo |

**Status state machines** (write these into `DESIGN.md`):
- Request: `PENDING → MATCHED → IN_PROGRESS → FULFILLED`, plus `PENDING → UNMATCHED` (nothing available) and `* → CANCELLED`
- Resource: `AVAILABLE → ASSIGNED → AVAILABLE`, plus `* → OFFLINE`

Reject invalid transitions with **409**.

## Repository layout

The full file-by-file structure, with an owner for every file, is in **`DESIGN.md` section 9**. Use the **repo root as the Docker build context** so each service's Dockerfile can `COPY services/common`.

## Phase 2: Storage node (days 2–4)

- Endpoints: `PUT/GET/DELETE /internal/data/{key}`, `GET /internal/data?since=&prefix=`, `GET /health`.
- Config from env vars: `NODE_ID`, `SHARD_ID`, `ROLE` (primary or replica), `COORDINATOR_URL`, `DATA_DIR`.
- On startup, call `POST /internal/nodes/register`, then send heartbeats in a background loop.
- **Done when:** one node in Docker stores and returns data, and the data survives a container restart (volume mounted).

## Phase 3: Coordinator (days 3–6)

- Node registry from register and heartbeat calls; a background task marks nodes DOWN.
- Routes data operations: compute the shard → write to the live primary and replica → read from primary or replica.
- Fan-out `GET /internal/query`.
- Recovery: when a DOWN node re-registers, have it sync from its partner, then mark it UP.
- Debug endpoint `GET /internal/status`: every node, its shard, role, state and last heartbeat. **You'll need it for every demo.**
- **Done when:** with 6 nodes up, keys spread across 3 shards; stopping a primary keeps reads and writes working through the replica.

## Phase 4: Request service + RabbitMQ (days 5–8)

- Implement every client endpoint in the spec table, with JSON errors (400, 404, 409, 503).
- `POST /requests`: validate → store through the coordinator (`PENDING`) → publish `{request_id}` → **201**.
- **Done when:** creating a request shows a message in the RabbitMQ management UI (port 15672).

## Phase 5: Matching service (days 7–10)

- pika consumer: fetch the request → query candidates → sort by distance → claim with `If-Match` → update both records → ack. If no candidate, mark `UNMATCHED` and ack. On error, nack to the DLQ.
- Expose HTTP `POST /match/{id}` using the same function.
- **Done when:** 20 requests submitted together are all processed, there is no double assignment with 2 consumers, and the queue drains.

## Phase 6: Docker Compose, full stack (days 9–11)

- Containers: `rabbitmq` (management image), `coordinator`, `request-service`, `matching-service`, `storage-s0-primary`, `storage-s0-replica` … `storage-s2-replica`, one named volume per storage node.
- Healthchecks plus `depends_on: condition: service_healthy`, so services wait for RabbitMQ.
- Write `scripts/seed.py`, which registers sample resources across a few cities.
- **Done when:** `docker compose up --build` brings everything up and the full flow works end to end.

## Phase 7: Kubernetes (days 11–15)

- **Deployments + Services** for coordinator, request service and matching service (2+ replicas each).
- **RabbitMQ**: a Deployment (or StatefulSet) plus a Service.
- **Storage**: one **StatefulSet per shard** (`shard0`, `shard1`, `shard2`) with `replicas: 2`. Ordinal 0 is the primary and ordinal 1 the replica. Use `volumeClaimTemplates` for persistence and a headless Service for stable DNS (`shard0-0.shard0`).
- ConfigMap for shared settings; a NodePort or `kubectl port-forward` to reach the request service.
- Readiness and liveness probes on `/health`.
- **Spec demos:** `kubectl delete pod <matching-pod>` → it comes back automatically; `kubectl scale deploy matching-service --replicas=4` → the queue drains faster.
- **Storage failure in Kubernetes:** a deleted pod restarts within seconds, so the "during failure" state barely shows. Run the storage failure demo either with `docker stop` on the Compose stack (the spec allows "stopping the container **or** pod") or by scaling the shard's StatefulSet to 1 (removes the replica). Optionally, add an admin endpoint `POST /admin/pause` that makes a node stop heartbeating and answering, so you can fail any node on demand.

## Phase 8: Tests and demo scripts (throughout, finalised days 14–17)

- Unit tests: shard function, distance and ranking, state transitions.
- Integration tests (pytest against Compose): every item in spec section 11.
- `scripts/demo_*.sh` or `.py`, one per item in spec section 10, printing a clear before/during/after state using `/internal/status`.
- README: prerequisites, build, run (Compose and Kubernetes), a demo walkthrough, an architecture diagram, and design choices with justifications (the spec's section 12 says you must justify them).

---

## Team split (2 people)

The project splits cleanly into two halves that meet at the **coordinator API** (`DESIGN.md` section 8):

| | **Person A: storage side** | **Person B: app side** |
|---|---|---|
| Programs | Storage node, coordinator | Request service, matching service, client script |
| Ready-made pieces | – | RabbitMQ setup (exchange, queue, DLQ) |
| Spec features | Sharding, replicas, heartbeats, failover, recovery sync | Client REST API, async processing, matching, "only if still free" claiming |
| Docker | Dockerfiles for storage node and coordinator | Dockerfiles for request and matching services, **docker-compose.yml** |
| Kubernetes | StatefulSets + headless Services for the 3 shards, coordinator Deployment | Deployments + Services for the apps and RabbitMQ, scaling and pod-recovery demos |
| Demos | Data distribution, storage failure, node recovery | Create/retrieve, register, matching, many requests via RabbitMQ, Kubernetes recovery and scaling |
| Tests | Shard rule, replication, failover, resync | Validation, status transitions, matching rule, concurrent matching |
| Shared | `services/common/` (record shapes, shard rule), README, `DESIGN.md` | same |

**Rules that keep two people from blocking each other:**
- **Day 1, together:** agree on `DESIGN.md` and write `services/common/` (record shapes, fixed lists, shard rule).
- **Day 1–2, Person A:** ship a **fake coordinator**, an in-memory dictionary behind the exact coordinator API. Person B builds against it while A builds the real one.
- Each person works only in their own folders (`services/storage_node` + `services/coordinator` for A; `services/request_service` + `services/matching_service` for B), so merge conflicts are rare.
- One branch per feature, and a pull request that **the other person reviews** before merging. Both of you end up understanding the whole system, which you'll need for the viva/demo.
- A 10-minute check-in every day or two.

**Sync points:**
1. **End of week 1:** swap the fake coordinator for the real one and run the full flow in Docker Compose.
2. **End of week 2:** all failure handling works in Compose (storage node stop/start, concurrent matching).
3. **Week 3:** Kubernetes, demo scripts, README. Rehearse the full demo together at least once.

## Open questions for the team
- What is the submission deadline? The day numbers above assume about 3 weeks.
- Will the demo be live or recorded? A recording lets the failure demos be edited down.
