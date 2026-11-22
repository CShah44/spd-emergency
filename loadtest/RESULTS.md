# Load test results

Every number here came from `python loadtest/run.py` (see `loadtest/README.md`). Resume lines may
only quote numbers from this file.

## Baseline (before coordinator HA, the operation log and geo-sharding)

| scenario | rate (req/s) | duration | accepted | refused | matched | unmatched | p50 | p95 | p99 | lost | double-assigned | recovery | date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| steady | 5 | 60 s | 293 | 0 | 285 | 8 | 174 ms | 245 ms | 353 ms | 0 | 0 | - | 2026-11-22 |
| steady | 10 | 60 s | 579 | 1 | 579 | 0 | 863 ms | 4.09 s | 4.82 s | 0 | 0 | - | 2026-11-22 |
| overload | 20 | 60 s | 1173 | 0 | 1171 | 2 | 89.25 s | >120 s | >120 s | 0 | 0 | - | 2026-11-22 |
| kill-matcher | 5 | 60 s | 293 | 0 | 286 | 7 | 83 ms | 221 ms | 244 ms | 0 | 0 | 8.3 s | 2026-11-22 |
| kill-rabbitmq | 5 | 60 s | 255 | 0 | 253 | 2 | 80 ms | 85.16 s | 113.03 s | 0 | 0 | 23.6 s | 2026-11-22 |

**What was measured.** The app services (request service, matching service) are the current
code from `wow-factor`. The coordinator and storage nodes were built from `main` at `990d039`,
i.e. before task 3 (a single coordinator, no leader election) and task 4 (recovery by comparing
the whole data set, no operation log). So the later rows can be compared with these.

**Setup.** Kubernetes on minikube (4 CPUs, 4 GB) on one Windows laptop: 2 request-service pods,
2 matching-service pods, 1 coordinator, 6 storage pods, 1 RabbitMQ pod. The load generator and
Prometheus run on the same machine, and the API is reached through `kubectl port-forward`.
Before every row the cluster and its data were wiped and redeployed, because the cost of a
match grows with the number of responders stored (see below), so rows are only comparable from
a clean start. Each row registers 1.5 responders per planned request in five cities first.

**Columns.**
- *accepted / refused*: requests the API took (201) or turned away. A refused request is not lost: the client was told.
- *p50 / p95 / p99*: seconds from the request being created to the matcher saving MATCHED or UNMATCHED, from `spd_match_e2e_seconds` in Prometheus. The histogram's largest bucket is 120 s, so `>120 s` means "slower than that".
- *lost*: accepted requests that were never answered. A request is only counted lost when no answer has arrived for 150 s (longer than the sweeper needs to rescue one that was never queued) or 15 minutes have passed. *Double-assigned* also counts responders assigned to two requests, matches not mirrored on the responder, and claims never completed. Target for both: 0.
- *recovery*: time from the crash until every matcher is consuming from the queue again and every pod of the service is Ready.

**What the baseline shows.**
- **With two matchers, capacity lies between 5 and 10 requests per second.** At 5 req/s the p99 is 353 ms. At 10 req/s the queue builds and p99 reaches 4.8 s. At 20 req/s the queue grows for the whole minute: the median request waits 89 s. Nothing was lost even then: every accepted request was answered, just late.
- **Why.** In a trial run with about 900 responders registered, a match took roughly 150 ms (median, inside the matcher), and each matcher handles one message at a time. Every match asks all three shards, and each storage node scans every responder record it holds and filters by type and status as it reads them, so the cost grows with the total number of responders stored, not with the number nearby. Geo-sharding (task 5) and more matchers (task 6) are aimed at this.
- **Killing a matcher costs nothing visible.** The other pod carried the load (p99 244 ms, nothing refused, nothing lost) and the replacement was consuming again after 8.3 s.
- **Killing RabbitMQ loses nothing, but those requests are slow.** Requests accepted while the broker was down were saved but not queued, so they waited for the sweeper: p95 85 s, p99 113 s. The broker was back after 23.6 s. Fewer requests were accepted (255 against about 300 planned); the slowest `POST /requests` during the run took 1.1 s.
- **One request in the steady 10 req/s run was refused with a 503** at the very start of the run (1 of 580). It was not investigated further.

The steady rows were run with an earlier version of the verifier that waited a fixed 60 s; every
request was already answered by then, so the final verifier gives the same result. The other
three rows were rerun with the final one.

## With KEDA autoscaling (task 6)

The same scenario as the baseline "overload" row (20 req/s for 60 s, fresh cluster, same machine). The
matchers now start at 1 and KEDA sets their number from the queue length (about one per 20 waiting
requests, 1 to 5). Over the run the matchers went from 1 to 5 and the queue peaked at 280 waiting requests.

| scenario | rate (req/s) | duration | accepted | refused | matched | unmatched | p50 | p95 | p99 | lost | double-assigned | recovery | date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| overload-keda | 20 | 60 s | 1180 | 0 | 1180 | 0 | 10.44 s | 46.55 s | 57.31 s | 0 | 0 | - | 2026-11-22 |

Against two fixed matchers in the baseline, the median wait fell from 89 s to 10.4 s and the p99 from
over 120 s to 57 s, with nothing lost in either. It is still slow: the queue peaked about when the load
stopped, so even five matchers did not keep up with 20 requests a second at this data size. The
autoscaler reacts to a queue that has already formed, and each match still scans every responder
(see the baseline notes), which geo-sharding is meant to cut.

## With geo-sharding (task 5)

The same overload as the KEDA row (20 req/s for 60 s, fresh cluster, KEDA on) with new records carrying
their geohash cell in the id and the matcher searching only the request's cell and its 8 neighbours.
The "before" for this comparison is the `overload-keda` row above.

| scenario | rate (req/s) | duration | accepted | refused | matched | unmatched | p50 | p95 | p99 | lost | double-assigned | recovery | date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| overload-keda-geo | 20 | 60 s | 1184 | 0 | 1184 | 0 | 3.09 s | 4.96 s | 15.72 s | 0 | 0 | - | 2026-11-22 |

- **Median wait 10.4 s -> 3.1 s, p99 57 s -> 15.7 s**, nothing lost or double-assigned. Fewer shards asked per match means less scanning per match, so the same matchers get through the queue faster.
- **Shards asked per query: 1.66 on average** over the whole run (`spd_shards_per_query`: 820 queries touched 1 shard, 1,548 touched 2, 12 touched all 3). That is down from 3, but not "mostly 1": a request and its neighbours often straddle two regions. This average also includes the verifier's and the web page's own queries.
- **The hot spot is real.** Records per shard after the run: shard 0 held 587, shard 1 held 1,673 and shard 2 held 724 (each on two nodes), so one shard holds 56% of everything. The five cities hash to uneven regions; with hash sharding the three would have been about equal. That is the price of keeping nearby records together.
- Only one run per side, on one laptop, so treat the size of the gain as indicative. The before and after were measured on separate fresh clusters.
