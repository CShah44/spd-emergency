# Coordinator failover: measured on Kubernetes

Roadmap task 3. Reproduce with `python scripts/measure_coordinator_failover.py delete|kill|freeze`
(`kill` and `freeze` need Docker Desktop's Kubernetes, where pods are Docker containers).

**Setup.** Docker Desktop Kubernetes (one node), the full system deployed with
`kubectl apply -k k8s`: two coordinator pods, six storage nodes, 717 records. A writer runs
**inside the cluster** (in a request-service pod, through the `coordinator` Service like the
real services) and writes a new record every 50 ms. Five seconds in, the leading coordinator
is failed. Afterwards the writer reads back every write that was acknowledged. Run on
2026-11-22, final code.

| How the leader failed | Outage seen by the client | Writes acknowledged | Acknowledged but lost | Handover |
|---|---:|---:|---:|---|
| **Clean delete** (`kubectl delete pod`) | 3.2 s | 469 of 491 | **0** | epoch 7 → 8 |
| **Crash** (`docker kill`) | 9.1 s | 380 of 456 | **0** | epoch 8 → 9 |
| **Freeze** for 10 s (`docker pause`, then resume) | 12.2 s | 333 of 412 | **0** | epoch 9 → 10 |

Earlier runs of the same scenarios (before two fixes described below) gave 5.9–6.1 s,
8.6–9.3 s and 10.9 s, also with 0 lost.

**Outage** is the longest gap between two successful writes. The writes in that gap got a
503, a connection error, or no answer within 2 s: never acknowledged, so a real client
retries them (the matching service already does).

## Where the time goes

- **Clean delete:** the leader releases the lease as it shuts down, so the standby takes it
  within a second. Then the new leader **settles**: it waits until all six storage nodes have
  registered again (at most one 3 s heartbeat round) before serving, and the Service switches
  to its pod once it has labelled it.
- **Crash:** no release, so the standby waits for the **6 s lease** to run out
  (`LEASE_DURATION_S`), then settles.
- **Freeze:** the same 6 s, plus requests that went to the frozen pod hang until their 2 s
  timeout, until the new leader moves the label. When the frozen coordinator wakes up, its own
  clock tells it its lease is stale: it announces "now STANDBY" before doing anything.

Faster is possible (a shorter lease, a shorter heartbeat interval) at the price of more
false failovers when the API server or the network is merely slow.

## Found and fixed while measuring

1. **Same pod, same epoch.** After `docker kill`, Kubernetes restarted the container within
   a second or two and the new process, with the same name (the pod name), simply "renewed its
   own" lease: same epoch for a new process. Now each process competes as
   `<pod>_<random suffix>`, so every new leader term gets a new epoch.
2. **A frozen leader never announced that it had lost.** On waking, its clock already said
   "not leader", so comparing before and after a round showed no change and it kept its old
   registry and its leader label. Changes are now compared with what was last announced. Unit
   test: `test_a_leader_that_froze_announces_it_lost`.

## Fencing

In these runs the frozen leader stepped down by its own clock, so no storage node had to
refuse it. Fencing is the second line of defence for a request that was already past that
check when the process froze. That case is covered by the unit test
`test_takeover_fences_the_old_leader_and_picks_the_freshest_node`: the old leader's write
arrives with epoch 0, the node knows epoch 1, refuses it (409 fenced), and the old leader
steps down with the data unchanged.
