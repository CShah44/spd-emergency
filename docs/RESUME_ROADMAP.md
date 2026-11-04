# Emergency Matching: Resume-Grade Roadmap

This is what to add **after the course spec is complete** to turn the project into something that holds up in a professional interview. The rule for every addition: it must **solve a real problem you can demonstrate and measure**. Interviewers ask what happens when two pods match the same resource, or how many requests were lost during a node failure (ideally zero, measured). Nobody is impressed by a list of technology names.

> Don't break the spec while upgrading. RabbitMQ and Kubernetes are mandatory, so don't swap RabbitMQ for Kafka. `hash(id) mod 3` "may be used", so replacing it with consistent hashing or geo-sharding is allowed, as long as you justify the change.

---

## Tier 1: highest resume value

### 1. Correctness under concurrency and failure
| Addition | Problem it solves | How to prove it |
|---|---|---|
| **Atomic resource claim** (compare-and-set on a `version` field, or `UPDATE ... WHERE status='AVAILABLE'`) | Two scaled matching pods assign the same resource | Test: 500 concurrent requests against 10 resources, assert no double assignment |
| **Transactional outbox** | "Store the request, then publish" is a dual write. A crash in between means the request is stored but never matched | Kill the request service between the two steps; the request still gets matched after restart |
| **Idempotent consumers** (deduplicate on message ID or request ID) | RabbitMQ is at-least-once, so redelivery causes a second match | Force a redelivery (kill the consumer before it acks); still only one assignment |
| **Retry with backoff + dead-letter queue** | A message that keeps failing loops forever | A poison message ends up in the DLQ, can be inspected, and can be replayed |
| **Publisher confirms + durable queues + persistent messages** | Messages are lost if RabbitMQ restarts | Restart the RabbitMQ pod with messages in flight; nothing is lost |

### 2. Real replication
- **Per-primary operation log with sequence numbers.** A recovering node asks its partner for "everything after seq N" and catches up. This is a proper answer to "synchronize data when a failed node recovers."
- **Automatic failover with epochs or fencing tokens.** A returning old primary is demoted and can't accept writes, which prevents **split-brain**. Demonstrate it explicitly.
- **Highly available coordinator.** Run 2 replicas with leader election through the **Kubernetes Lease API**. This ties to the "ask the leader" and coordinator-failure material in Lecture 10.
- *Stretch:* Raft for each shard group (for example hashicorp/raft, or a Python Raft library). Strong signal, large time cost. Only attempt it once everything else is done.

### 3. Fix the sharding and matching mismatch
- The problem: sharding by `hash(id)` scatters "all available water near X" across every shard, so each match becomes a scatter-gather query.
- **Geo-sharding:** convert location to **geohash or H3 cells** and shard by region, so most matches stay on one shard. Fall back to neighbouring cells, then other shards.
- **Consistent hashing** instead of `mod 3`, so adding a 4th shard moves only about 1/4 of the keys. Demonstrate a live rebalance.
- **Benchmark** single-shard lookups against scatter-gather and put the numbers in the README.

### 4. Observability
- **Prometheus metrics:** queue depth, match latency, matches per second, shard health, replication lag.
- **Grafana dashboard.** It doubles as the centrepiece of the demo video.
- **OpenTelemetry tracing**, with trace context passed **through RabbitMQ message headers**, so one trace covers client → request service → queue → matcher → shard.
- Structured JSON logs with a correlation ID.

### 5. Load and chaos testing (this is where the resume numbers come from)
- **k6 or Locust:** throughput, p50/p95/p99 latency from request to match.
- **Chaos under load:** kill storage, matcher and RabbitMQ pods (scripted, or with Chaos Mesh). Measure lost requests (target 0), failover time, and resync time.
- **KEDA autoscaling on RabbitMQ queue depth:** show matching pods going 1 → 5 → 1 through a load spike.

### 6. Production-grade deployment
- **Helm chart**; readiness and liveness probes; resource requests and limits; PodDisruptionBudgets; ConfigMaps and Secrets.
- **GitHub Actions CI:** unit tests, image builds, and an **end-to-end test on a `kind` cluster** on every PR.
- *Optional:* EKS or GKE with **Terraform**, plus a short cost and architecture write-up.

---

## Tier 2: product features
- **Priority:** medical requests jump the queue (RabbitMQ priority queues, or separate queues with weighted consumers).
- **Quantity and partial fulfilment:** a request for 200 L of water can be split across several resources by capacity.
- **Escalation:** if a request is unmatched after N minutes, widen the radius or re-queue at higher priority.
- **Volunteer accept/decline with timeout and reassignment**, using the **saga pattern** with compensating actions.
- **Live map UI** (Leaflet plus WebSockets or SSE) showing requests, resources and matches as they happen.
- **Auth and roles** (requester, volunteer, admin) and API rate limiting.
- *Algorithmic stretch:* **batch matching** with min-cost assignment (the Hungarian algorithm) instead of greedy nearest-first. Report the reduction in total travel distance.

## What to avoid
- **Technology name-dropping.** Every component needs a one-sentence reason you can say out loud.
- **Replacing the custom storage with Postgres.** The hand-built sharded and replicated storage is the interesting part. Benchmark against Postgres if you like, but keep yours.
- **Upgrading before the spec works.** Secure the grade first.

## Order of work
1. Finish the course spec (see `BUILD_PLAN.md`).
2. Concurrency correctness: atomic claim, idempotency, DLQ, outbox.
3. Metrics and dashboard, then load tests, for **baseline numbers**.
4. Op-log replication and fencing, then chaos tests, for **failure numbers**.
5. KEDA, Helm and CI.
6. Geo-sharding or consistent hashing, and the map UI.
7. Write-up: architecture diagram, design doc covering each tradeoff, and a 3–5 minute demo video.

## Resume bullet templates
These are templates only. **Fill in your own measured numbers** and never claim anything you didn't measure.

- Built a distributed emergency-resource matching platform (Python, RabbitMQ, Kubernetes) with geo-sharded storage across 3 replicated shard groups and log-based replica catch-up.
- Eliminated double-assignment under concurrent matching using optimistic locking and idempotent consumers; verified with 500-way concurrency tests.
- Sustained **N req/s** at **p99 X ms** end-to-end match latency, with **zero lost requests** across chaos tests killing storage, matcher, and broker pods under load.
- Implemented queue-depth autoscaling (KEDA), OpenTelemetry tracing across HTTP and AMQP, and CI end-to-end tests on kind.
