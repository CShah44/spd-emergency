"""The load test's verdict: which requests count as lost, and what counts as a responder given twice.
If this check were wrong, every "lost requests = 0" number would be meaningless."""

import itertools
import time

from loadtest import verify
from loadtest.run import match_latency, result_row, seconds


def req(rid, status, resource=None):
    return {"id": rid, "status": status, "assigned_resource_id": resource}


def res(rid, status="ASSIGNED", holder=None):
    return {"id": rid, "status": status, "assigned_request_id": holder}


# -- classify ----------------------------------------------------------------------------------------

def test_matched_and_unmatched_are_both_answers():
    requests = {"r1": req("r1", "MATCHED", "x1"), "r2": req("r2", "UNMATCHED")}

    report = verify.classify(["r1", "r2"], requests, [res("x1", holder="r1")])

    assert (report.matched, report.unmatched, report.lost) == (1, 1, 0) and report.ok


def test_a_request_still_pending_is_lost():
    report = verify.classify(["r1"], {"r1": req("r1", "PENDING")}, [])

    assert report.pending == 1 and report.lost == 1 and not report.ok


def test_a_missing_or_unreadable_request_is_lost():
    report = verify.classify(["r1", "r2", "r3"], {"r1": "missing", "r2": "unreadable"}, [])

    assert (report.missing, report.unreadable, report.lost) == (1, 2, 3)


def test_one_responder_for_two_requests_is_double_assignment():
    requests = {"r1": req("r1", "MATCHED", "x1"), "r2": req("r2", "MATCHED", "x1")}

    report = verify.classify(["r1", "r2"], requests, [res("x1", holder="r1")])

    assert len(report.double_assigned) == 1 and "r1" in report.double_assigned[0] and "r2" in report.double_assigned[0]
    assert not report.ok


def test_a_matched_request_whose_responder_points_elsewhere_is_inconsistent():
    requests = {"r1": req("r1", "MATCHED", "x1")}

    report = verify.classify(["r1"], requests, [res("x1", holder="someone-else")])

    assert report.inconsistent == ["r1 -> x1"] and not report.ok


def test_a_matched_request_whose_responder_is_free_again_is_inconsistent():
    report = verify.classify(["r1"], {"r1": req("r1", "MATCHED", "x1")}, [res("x1", status="AVAILABLE")])

    assert report.inconsistent == ["r1 -> x1"]


def test_a_responder_held_for_a_request_that_never_got_it_is_a_leaked_claim():
    requests = {"r1": req("r1", "UNMATCHED")}

    report = verify.classify(["r1"], requests, [res("x1", holder="r1")])

    assert report.leaked == ["x1 held for r1"] and not report.ok


def test_responders_held_for_requests_outside_the_run_are_ignored():
    report = verify.classify(["r1"], {"r1": req("r1", "UNMATCHED")}, [res("x9", holder="from-an-earlier-run")])

    assert report.ok


def test_a_clean_run_with_many_requests_is_ok():
    ids = [f"r{n}" for n in range(50)]
    requests = {i: req(i, "MATCHED", f"x{n}") for n, i in enumerate(ids)}
    resources = [res(f"x{n}", holder=i) for n, i in enumerate(ids)]

    report = verify.classify(ids, requests, resources)

    assert report.matched == 50 and report.ok


# -- waiting for answers ---------------------------------------------------------------------------------

def test_requests_answered_late_are_picked_up_by_asking_again():
    calls = itertools.count()
    answers = {"r1": [req("r1", "PENDING"), req("r1", "PENDING"), req("r1", "MATCHED", "x1")], "r2": [req("r2", "UNMATCHED")]}
    seen = {"r1": 0, "r2": 0}

    def get(request_id):
        next(calls)
        record = answers[request_id][min(seen[request_id], len(answers[request_id]) - 1)]
        seen[request_id] += 1
        return record

    results = verify.fetch_answers(["r1", "r2"], get, wait_s=5, interval_s=0)

    assert results["r1"]["status"] == "MATCHED" and results["r2"]["status"] == "UNMATCHED"
    assert seen["r2"] == 1  # an answered request is not asked about again


def test_a_request_that_never_gets_answered_is_returned_pending_when_time_runs_out():
    results = verify.fetch_answers(["r1"], lambda rid: req(rid, "PENDING"), wait_s=0.05, interval_s=0.01)

    assert results["r1"]["status"] == "PENDING"


def test_a_missing_request_is_not_asked_about_again():
    seen = []

    def get(request_id):
        seen.append(request_id)
        return "missing"

    results = verify.fetch_answers(["r1"], get, wait_s=5, interval_s=0)

    assert results["r1"] == "missing" and seen == ["r1"]


def test_waiting_goes_on_while_answers_keep_arriving():
    """A slow backlog is not a loss: while answers keep arriving it keeps waiting, and ends with everything answered."""
    ids = [f"r{n}" for n in range(1, 9)]
    # one more request gets answered on every pass
    state = {"answered": 0}

    def slowly(request_id):
        number = int(request_id[1:])
        if number <= state["answered"]:
            return req(request_id, "MATCHED", "x")
        if number == state["answered"] + 1:
            state["answered"] += 1
        return req(request_id, "PENDING")

    results = verify.fetch_answers(ids, slowly, wait_s=30, interval_s=0.01, stall_s=0.5)

    assert all(r["status"] == "MATCHED" for r in results.values())


def test_waiting_stops_once_nothing_has_been_answered_for_the_stall_time():
    started = time.monotonic()

    results = verify.fetch_answers(["r1"], lambda rid: req(rid, "PENDING"), wait_s=30, interval_s=0.01, stall_s=0.1)

    assert results["r1"]["status"] == "PENDING" and time.monotonic() - started < 5


# -- the result row --------------------------------------------------------------------------------------

def test_durations_are_shown_in_a_readable_unit():
    assert seconds(0.0734) == "73 ms" and seconds(2.5) == "2.50 s" and seconds(None) == "n/a"


def test_an_empty_stack_is_clean():
    from loadtest.run import dirty_reason

    assert dirty_reason({"total": 0, "PENDING": 0}, 0) is None


def test_old_requests_or_responders_make_the_stack_dirty():
    from loadtest.run import dirty_reason

    assert "5 requests" in dirty_reason({"total": 5, "PENDING": 0}, 0)
    assert "40 responders" in dirty_reason({"total": 0, "PENDING": 0}, 40)
    assert "2 still PENDING" in dirty_reason({"total": 9, "PENDING": 2}, 0)


def test_a_latency_in_the_top_bucket_is_shown_as_a_lower_bound():
    assert seconds(120.0) == ">120 s" and seconds(119.0) == "119.00 s"


def test_the_row_has_one_cell_per_column():
    report = verify.classify(["r1"], {"r1": req("r1", "MATCHED", "x1")}, [res("x1", holder="r1")])

    row = result_row("steady", 20, 30, 1, 0, report, {"p50": 0.07, "p95": 0.1, "p99": 0.2, "observed": 1})

    assert row.count("|") == 15 and "| steady | 20 | 30 s | 1 | 0 | 1 | 0 | 70 ms | 100 ms | 200 ms | 0 | 0 |" in row


def test_latency_is_n_a_when_prometheus_is_unreachable():
    latency = match_latency("http://127.0.0.1:9", 30)

    assert latency["p50"] is None and latency["observed"] is None
