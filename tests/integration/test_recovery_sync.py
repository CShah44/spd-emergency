"""Spec section 11 against the live stack: recovery and data synchronization
after a node comes back, and (Kubernetes only) pod failure and automatic recovery."""

import time
import subprocess

import pytest

from common.storage_client import StorageError

pytestmark = pytest.mark.integration


def _change_shard_while_down(stack, shard):
    """Update one record, delete one, create some: everything a returning node must catch up on."""
    k_upd, rec = stack.new_request(shard)
    stack.store.put(k_upd, rec)
    stack.store.put_if_version(k_upd, {**rec, "status": "MATCHED"}, 1)
    k_del, rec = stack.new_request(shard)
    stack.store.put(k_del, rec)
    stack.store.delete(k_del)
    created = []
    for _ in range(5):
        k, rec = stack.new_resource(shard)
        stack.store.put(k, rec)
        created.append(k)
    return k_upd, k_del, created


def test_returning_node_catches_up_on_everything_it_missed(stack):
    shard = 1
    victim = stack.leader_of(shard)
    partner = stack.partner(victim)

    stack.fail(victim)
    stack.store.put(*stack.new_request(shard))  # first call notices: victim DOWN
    stack.wait_state(victim, "DOWN", timeout=15)
    k_upd, k_del, created = _change_shard_while_down(stack, shard)

    stack.revive(victim)
    stack.wait_state(victim, "UP")
    report = next(n for n in stack.status()["nodes"] if n["node_id"] == victim)["last_recovery"]
    assert report["partner"] == partner and report["pulled"] >= 8 and report["conflicts"] == 0

    # Look inside the recovered node itself, not through the coordinator.
    r = stack.node_direct(victim, f"/internal/data/{k_upd}")
    assert r.status_code == 200, f"{victim} is missing {k_upd}, which was created while it was down"
    assert r.json()["status"] == "MATCHED"
    assert stack.node_direct(victim, f"/internal/data/{k_del}").status_code == 404
    assert all(stack.node_direct(victim, f"/internal/data/{k}").status_code == 200 for k in created)
    assert stack.entries(victim) == stack.entries(partner)  # tombstones included


def test_no_failback_after_recovery(stack):
    """The recovered node does not take leadership back: moving it while writes are
    in flight could let two nodes accept compare-and-set writes for one shard."""
    shard = 2
    old_leader = stack.leader_of(shard)
    stack.fail(old_leader)
    stack.store.put(*stack.new_request(shard))
    new_leader = stack.leader_of(shard)
    assert new_leader == stack.partner(old_leader)
    stack.revive(old_leader)
    stack.wait_state(old_leader, "UP")
    assert stack.leader_of(shard) == new_leader


def test_no_data_lost_across_alternating_failures(stack):
    """Primary fails, replica takes writes; primary returns and catches up; then the
    replica fails and the primary must serve every write, including the ones made
    while it was down. The strongest end-to-end check that sync really happened."""
    shard = 0
    first = stack.leader_of(shard)
    second = stack.partner(first)
    written = {}

    def write_some(n):
        for _ in range(n):
            k, rec = stack.new_request(shard)
            stack.store.put(k, rec)
            written[k] = rec["id"]

    write_some(3)
    stack.fail(first)
    write_some(5)                      # only `second` has these
    stack.revive(first)
    stack.wait_state(first, "UP")
    stack.fail(second)
    write_some(5)                      # only `first` has these
    assert stack.leader_of(shard) == first
    for k, rid in written.items():
        assert stack.store.get(k)["id"] == rid, k
    stack.revive(second)
    stack.wait_state(second, "UP")
    assert stack.entries(first) == stack.entries(second)


# -- Kubernetes: pod failure and automatic recovery --------------------------------------------

def _pod_uid(pod):
    return subprocess.run(["kubectl", "get", "pod", "-n", "emergency", pod, "-o", "jsonpath={.metadata.uid}"],
                          capture_output=True, text=True).stdout


def test_deleted_storage_pod_is_replaced_and_catches_up(kubernetes_stack):
    """We only delete the pod; Kubernetes (the StatefulSet controller) recreates it
    with the same name and volume, and our recovery brings it up to date. Writes
    to its shard keep succeeding the whole time."""
    stack = kubernetes_stack
    shard = 1
    victim = stack.leader_of(shard)
    pod = f"shard{shard}-{0 if victim.endswith('primary') else 1}"
    uid_before = _pod_uid(pod)

    stack.fail(victim, "delete-pod")
    failures, left_up, start = [], False, time.monotonic()
    while time.monotonic() - start < 90:
        try:
            stack.store.put(*stack.new_request(shard))
        except StorageError as exc:
            failures.append(exc)
        state = stack.node_state(victim)
        left_up = left_up or state != "UP"
        if left_up and state == "UP":
            break
        time.sleep(0.2)

    assert failures == [], f"writes failed while the pod was replaced: {failures}"
    assert left_up, "the coordinator never saw the node leave"
    assert stack.node_state(victim) == "UP"
    assert _pod_uid(pod) not in ("", uid_before), "expected a NEW pod with the same name"
    report = next(n for n in stack.status()["nodes"] if n["node_id"] == victim)["last_recovery"]
    assert report["pulled"] > 0  # it missed the writes made during the gap, and got them
    assert stack.entries(victim) == stack.entries(stack.partner(victim))


def test_deleted_coordinator_pod_is_replaced_and_nodes_rejoin(kubernetes_stack):
    """The coordinator keeps its registry in memory, so a new coordinator pod starts
    knowing nothing. Nodes find out from the 404 on their next heartbeat and
    register again; the data itself was never on the coordinator."""
    stack = kubernetes_stack
    key, rec = stack.new_resource()
    stack.store.put(key, rec)

    subprocess.run(["kubectl", "delete", "pod", "-n", "emergency", "-l", "app=coordinator", "--wait=false"],
                   check=True, capture_output=True)

    def healed():
        return len(stack.status()["nodes"]) == 6 and stack.all_up()
    time.sleep(2)  # let the old pod go, so we are not looking at its registry
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if healed():
                break
        except Exception:  # noqa: BLE001  (coordinator restarting / port-forward reconnecting)
            pass
        time.sleep(0.5)
    assert healed(), "nodes did not re-register with the new coordinator"
    assert stack.store.get(key)["id"] == rec["id"]
