# Upgrade Plan: Tasks 1–6 from the Resume Roadmap

Two people, working in parallel. **A** owns storage and the coordinator. **B** owns the app
services, RabbitMQ and the ops tooling. Same split as the course build, so each person
mostly edits only their own folders.

The goal of every task is a **number or a demo you can show**, not just a feature. Each PR
says what it measured.

| # | Task | Owner | Size | What it proves |
|---|---|---|---|---|
| 1 | Metrics + Prometheus + Grafana | A and B (split by service) | 1–2 days | Live graphs of the whole system |
| 2 | Load + chaos tests | B (A adds storage and coordinator chaos) | 2 days | req/s, p99 match latency, **lost requests = 0** |
| 3 | Coordinator HA with leader election and fencing | A | 3–4 days | Kill the leader under load: failover time, 0 lost writes |
| 4 | Operation log for replica catch-up | A | 2–3 days | Catch-up time grows with downtime, not data size |
| 5 | Geo-sharding | A (routing) and B (matching) | 3–5 days | Shards touched per match: 3 → 1, latency before/after |
| 6 | KEDA autoscaling on queue depth | B | 1 day | Matchers go 1 → 5 → 1 through a load spike |

---

## Step 0: together, before splitting up (half a day)

Agree on these three things, merge them as one small PR (`feat/upgrade-step0`), then split.

1. **`services/common/metrics.py`**: one helper both sides use, so every service exposes
   `GET /metrics` the same way. Add `prometheus-client` to each service's
   `requirements.txt`. All metric names start with `spd_`.
2. **The metric names below**: the dashboard (B) and the services (A and B) must agree.
3. **The geo-sharding interface** (section 5) is settled now, even though it's built later,
   so nobody designs around the old one.

### Agreed metric names

| Metric | Type | Labels | Owner | Where |
|---|---|---|---|---|
| `spd_http_requests_total` | counter | `service`, `route`, `status` | both | `common/metrics.py` middleware |
| `spd_http_request_seconds` | histogram | `service`, `route` | both | same middleware |
| `spd_node_state` | gauge (1 = in that state) | `node`, `shard`, `state` | A | coordinator |
| `spd_shard_available` | gauge (0/1) | `shard` | A | coordinator |
| `spd_replication_failures_total` | counter | `shard` | A | coordinator router |
| `spd_recovery_seconds` | histogram | `node` | A | coordinator recovery |
| `spd_recovery_entries_copied_total` | counter | `node` | A | coordinator recovery |
| `spd_storage_keys` | gauge | `node` | A | storage node |
| `spd_coordinator_is_leader` | gauge (0/1) | `pod` | A | coordinator (task 3) |
| `spd_coordinator_epoch` | gauge | `pod` | A | coordinator (task 3) |
| `spd_shards_per_query` | histogram | | A | coordinator router (task 5) |
| `spd_replication_lag_writes` | gauge | `shard`, `node` | A | coordinator (leader), from the operation-log positions (task 4) |
| `spd_recovery_total` | counter | `node`, `mode` (`log`, `full`, `alone`) | A | coordinator recovery |
| `spd_fenced_total` | counter | `node` | A | storage node: calls refused for an old epoch (task 3) |
| `spd_coordinator_settle_seconds` | histogram | | A | coordinator: a new leader's settle step (task 3) |
| `spd_requests_created_total` | counter | `type` | B | request service |
| `spd_publish_failures_total` | counter | | B | request service |
| `spd_match_outcomes_total` | counter | `outcome` | B | matcher |
| `spd_match_seconds` | histogram | | B | matcher (time inside `match_request`) |
| `spd_match_e2e_seconds` | histogram | | B | matcher: `now - request.created_at` when MATCHED/UNMATCHED |
| `spd_dead_lettered_total` | counter | | B | consumer |
| `spd_sweeper_rescued_total` | counter | | B | sweeper |

Queue depth comes from RabbitMQ itself (its Prometheus plugin), so nobody writes it.

---

## Ownership: who edits which files

| Area | Owner | Notes |
|---|---|---|
| `services/storage_node/`, `services/coordinator/` | A | |
| `services/request_service/`, `services/matching_service/`, `services/ui_service/` | B | |
| `observability/` (new: Prometheus config, Grafana dashboards), `loadtest/` (new) | B | |
| `scripts/chaos.py` (new) | B writes it | A adds storage/coordinator targets in a later PR |
| `k8s/coordinator.yaml`, `k8s/storage-*.yaml` | A | |
| `k8s/matching-service.yaml`, `k8s/rabbitmq.yaml`, `k8s/observability/`, `k8s/keda/` | B | |
| **Shared:** `services/common/`, `docker-compose.yml`, `k8s/kustomization.yaml`, `k8s/configmap.yaml`, `README.md` | both | Small edits only. Mention them in the PR. Rebase on `main` before merging. |
| **Hot spot:** `services/coordinator/app/router.py` | A | Touched by tasks 3 and 5: **3 merges before 5's routing starts** |

Branches: `feat/<task>-<short-name>`, for example `feat/3-coordinator-ha`. Small PRs; the
other person reviews.

---

## Timeline

```
           A (storage + coordinator)              B (app + ops)
Step 0     ──────────── together: metrics.py, names, geo interface ────────────
Phase 1    1: metrics in coordinator + nodes      1: Prometheus, Grafana, app metrics
                                                  6: KEDA
Phase 2    3: coordinator HA + fencing            2: load + chaos harness
                                                     -> BASELINE numbers (before 3/4/5)
Phase 3    4: operation log                       5: matching side of geo-sharding
           5: routing side (after 3 is merged)
Phase 4    ──────────── together: rerun chaos suite -> FINAL numbers, write-up ────────────
```

Each phase ends with both branches merged into `main` and the unit tests green.

---

## 1. Metrics and dashboard

**A:** add the A metrics from the table to the coordinator and storage nodes.
**B:** add the B metrics, and run Prometheus and Grafana:
- Compose: `prometheus` and `grafana` services; Grafana on `localhost:3000`, dashboard
  provisioned from `observability/grafana/` so it appears with no clicking.
- Kubernetes: same, under `k8s/observability/`, port-forwarded by `scripts/k8s_forward.py`.
- Enable RabbitMQ's `rabbitmq_prometheus` plugin for queue depth.

**Dashboard rows:** cluster health (node states, shards available), throughput
(requests/s, matches/s), latency (p50/p95/p99 of `spd_match_e2e_seconds`), queue (depth,
dead-lettered), replication (failures, recovery time), coordinator (leader, epoch).

**Done when:** `docker compose up` gives a working dashboard; pausing a storage node shows
up on it within one heartbeat.

---

## 2. Load and chaos tests

**B builds `loadtest/`** (Locust, so it stays Python):
- Seeds responders, then sends `POST /requests` at a set rate, saving every returned id.
- After the run, `loadtest/verify.py` checks that **every** saved id ends up `MATCHED` or
  `UNMATCHED` (none stuck in `PENDING`, none missing) and that no responder is assigned to
  two requests. **"Lost requests" = ids that fail this check.** The target is 0.
- Latency comes from `spd_match_e2e_seconds`, not from client polling.

**B builds `scripts/chaos.py`**: run a named failure while the load runs, e.g.
`python scripts/chaos.py kill-matcher`. B adds `kill-matcher`, `kill-rabbitmq`. **A adds**
`kill-storage-primary`, `pause-storage-node`, `kill-coordinator-leader` (after task 3).

**Report:** `loadtest/RESULTS.md`, one row per scenario: rate, p50/p95/p99, lost requests,
time to recover. Run it **once now (baseline)** and **again in Phase 4**.

**Done when:** one command runs a scenario end to end and prints that row.

---

## 3. Coordinator HA (A)

Today one coordinator pod handles every read and write; if it dies, everything stops until
Kubernetes restarts it.

- Run **2 coordinator replicas**. They elect a leader through the **Kubernetes Lease API**
  (`coordination.k8s.io/v1`, needs a ServiceAccount + Role for `leases`).
- **Only the leader is "ready"**: `/ready` returns 200 only on the leader, so the Service
  sends all traffic to it with no extra routing code. The standby stays alive and keeps
  trying to take the lease.
- **Rebuilding state on takeover:** the registry is in memory. A new leader waits one
  heartbeat round (every node re-registers) before saying it's ready. Nodes include their
  latest write position in the heartbeat, so the new leader picks the freshest node in each
  shard as the acting primary. (Task 4's sequence numbers make this exact.)
- **Fencing:** every leader term has an **epoch** (the Lease's `leaseTransitions`). The
  coordinator sends `X-Coordinator-Epoch` on every call to a storage node. A node remembers
  the highest epoch it has seen and rejects calls with a lower one (`409`). An old leader
  that wakes up after a pause can't write anything: this is what prevents split-brain.
- **Compose has no Kubernetes API**: `LEADER_ELECTION=off` (the default) keeps today's single
  coordinator, so Compose and the unit tests are unchanged.

**Demo and numbers:** under load, delete the leader pod. Measure failover time (last success
on the old leader → first success on the new one) and lost writes (target 0). Separately,
freeze the old leader (`kubectl exec ... kill -STOP 1`), let the standby take over, unfreeze
it, and show its writes being rejected by epoch.

---

## 4. Operation log (A)

Today a returning node compares its **whole** data set with its partner.

- Each node keeps an `oplog` table: `(epoch, seq, key, version, record, deleted)`. The acting
  primary assigns `seq`; the replica stores the same `(epoch, seq)` when it applies the write.
- Catch-up: the recovering node asks its partner for `GET /internal/oplog?after=<epoch>:<seq>`
  and applies the entries in order.
- **Fallback to today's full sync** when the partner's log has been trimmed past that point
  or the epochs have diverged (after a whole-shard outage). Keep the current code path for it.
- Trim the log past a size limit, so it doesn't grow forever.

**Numbers:** catch-up time and entries copied, op log vs. full sync, for 10k keys with a
node down for 10 s, 60 s and 5 min. Expected result: op-log time follows the downtime, full
sync follows the data size.

---

## 5. Geo-sharding (A + B)

Today records are placed by `hash(id) mod 3`, so "free water near X" asks **all 3 shards**
for every match.

**Interface (agree in Step 0):**
- A new record's **id carries its grid cell**: `res-<geohash3>-<hex>`, `req-<geohash3>-<hex>`.
  The cell is geohash precision 3 (about 156 × 156 km) of the record's location, so any
  record's shard can still be found from its id alone. (A responder doesn't move; if that
  changes, it gets re-registered.)
- `common/sharding.shard_for(key)`: if the id has a cell, the shard comes from the cell
  (consistent hashing over cells); otherwise it falls back to today's hash. **Old ids keep
  working.**
- `StorageClient.query(prefix, cells=[...], **filters)`: the coordinator asks only the
  shards that own those cells. Without `cells`, it asks every shard, as today.

**A:** cell-aware `shard_for`, consistent hashing, and the `cells=` query routing in the
coordinator, plus `spd_shards_per_query`.
**B:** the request service builds ids with the cell; the matcher queries the request's cell
plus its 8 neighbours (covering the 50 km limit), and asks every shard only if that finds
nothing.

**Numbers:** shards per match (3 → mostly 1), match latency and storage calls per match,
before and after, with the same load test.

**The tradeoff to be ready to explain:** a busy city now lands on one shard, so load is
less even than with hashing (a "hot spot"). Measure the keys per shard after seeding and
put that in the write-up too.

---

## 6. KEDA autoscaling (B)

- Install KEDA into the cluster (one `kubectl apply` of its release manifest, wrapped in
  `k8s_up.py`).
- A `ScaledObject` for `matching-service` using KEDA's **RabbitMQ scaler** on
  `match.requests`: minimum 1, maximum 5 replicas, target about 20 messages per pod.
- Compose has no KEDA, so it stays at a fixed `--scale`.

**Demo:** a load spike from task 2; the Grafana panel shows queue depth rising, pods going
1 → 5, the queue draining, and pods going back to 1.

---

## Phase 4: final numbers and write-up (together)

- Rerun every scenario in `loadtest/RESULTS.md` on the final system, next to the baseline.
- Update the README: architecture diagram, one paragraph per task (the problem, the fix, the
  number), and the known limits that remain.
- Record a 3–5 minute demo video from the Grafana dashboard.
- **Resume lines only quote numbers that are in `RESULTS.md`.**
