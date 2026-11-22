# Load test

Sends emergency requests at a steady rate and answers two questions: **how fast** (p50 / p95 /
p99 from request created to MATCHED or UNMATCHED) and **was anything lost** (target: 0 lost,
0 responders given to two requests).

```
pip install -r loadtest/requirements.txt              # Locust (the load generator)
python scripts/k8s_up.py --fresh --seed               # a clean stack (or: docker compose down -v; docker compose up -d)
python loadtest/run.py --rate 10 --duration 60        # prints a result row
python loadtest/run.py --rate 10 --duration 60 --label steady --record   # also adds it to loadtest/RESULTS.md
```

`run.py` registers enough responders, runs the load, waits for every accepted request to be
answered (as long as answers keep arriving: a deep queue that is still draining is slow, not
lost) and checks each one (`verify.py`), then reads the latency from Prometheus
(`spd_match_e2e_seconds`, measured inside the matcher, not by polling from the client).
Start from a clean stack: leftovers from earlier runs are matched by the sweeper and look
like very slow requests, so `run.py` refuses to start if any are PENDING (`--allow-dirty` overrides).

To check a run again later: `python loadtest/verify.py loadtest/results/<name>-ids.txt`.

**What "lost" means:** an accepted request (the API said 201) that ends up still PENDING,
missing, or unreadable. MATCHED and UNMATCHED are both fine (UNMATCHED means nobody was in
range). A request the API *refused* (not 201) is not lost: the client knows it was not accepted;
it is counted as "refused".
