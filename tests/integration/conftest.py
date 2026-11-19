"""Integration tests: run against a REAL running stack, not an in-process copy.

    docker compose up -d --build                  # or: kubectl apply -k k8s/ + scripts/k8s_forward.py
    .venv\\Scripts\\python -m pytest -m integration

They really stop containers / pause nodes / delete pods, so they are opt-in:
the plain `pytest` run skips them (see addopts in pytest.ini). If no stack is
reachable they skip themselves instead of failing.

Unit tests prove our logic with simulated failures. These prove the same
claims hold with real processes, a real network, real timeouts and SQLite on
disk: the PID-1 shutdown bug, for example, only ever showed up here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _demo  # noqa: E402  (shared with the demo scripts: one way to fail/revive a node)

RECOVERY_TIMEOUT_S = 60


class Stack:
    """The running stack plus a record of what each test broke, so it can be put back."""

    def __init__(self):
        self.kubernetes = _demo.on_kubernetes()
        # The realistic failure on each platform. `stop` kills the container; on
        # Kubernetes a killed pod is replaced at once, so `pause` (the node answers
        # 503 and stops heartbeating) is the way to keep a node down for a while.
        self.method = "pause" if self.kubernetes else "stop"
        self.failed: dict[str, str] = {}

    store = _demo.store
    status = staticmethod(_demo.store.status)
    node_direct = staticmethod(_demo.node_direct)
    node_state = staticmethod(_demo.node_state)
    leader_of = staticmethod(_demo.leader_of)
    new_request = staticmethod(_demo.new_request)
    new_resource = staticmethod(_demo.new_resource)

    def fail(self, node_id: str, method: str | None = None) -> None:
        method = method or self.method
        _demo.fail_node(node_id, method)
        if method != "delete-pod":  # Kubernetes brings that one back by itself
            self.failed[node_id] = method

    def revive(self, node_id: str) -> None:
        _demo.revive_node(node_id, self.failed.pop(node_id))

    def wait_state(self, node_id: str, state: str, timeout: float = RECOVERY_TIMEOUT_S) -> float:
        took = _demo.wait_for(lambda: self.node_state(node_id) == state, timeout)
        assert took is not None, f"{node_id} not {state} after {timeout}s (now {self.node_state(node_id)})"
        return took

    def entries(self, node_id: str) -> dict:
        """Everything a node holds, tombstones included, read from the node itself."""
        def read():
            r = self.node_direct(node_id, "/internal/sync")
            return r.json()["entries"] if r.status_code == 200 else None
        got = []
        _demo.wait_for(lambda: got.append(read()) or got[-1] is not None, 15)
        assert got and got[-1] is not None, f"cannot read {node_id} directly"
        return got[-1]

    def all_up(self) -> bool:
        return all(n["state"] == "UP" for n in self.status()["nodes"])

    def partner(self, node_id: str) -> str:
        shard, role = node_id[1], node_id.split("-")[1]
        return f"s{shard}-{'replica' if role == 'primary' else 'primary'}"


@pytest.fixture(scope="session")
def _reachable():
    try:
        st = _demo.store.status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no stack at {_demo.COORDINATOR_URL} ({exc}); start Compose or Kubernetes first")
    if st.get("mode") != "real":
        pytest.skip("the coordinator is the in-memory fake; integration tests need the real one")


@pytest.fixture
def stack(_reachable):
    s = Stack()
    # Start every test from a healthy cluster (a previous run may have left a node recovering).
    assert _demo.wait_for(s.all_up, RECOVERY_TIMEOUT_S) is not None, \
        f"not all nodes UP before the test: {[(n['node_id'], n['state']) for n in s.status()['nodes']]}"
    yield s
    # Put back whatever the test broke, even if it failed half-way.
    for node_id in list(s.failed):
        try:
            s.revive(node_id)
        except Exception:  # noqa: BLE001  (already back, e.g. the test revived it)
            s.failed.pop(node_id, None)
    assert _demo.wait_for(s.all_up, RECOVERY_TIMEOUT_S) is not None, "cluster did not heal after the test"


@pytest.fixture
def kubernetes_stack(stack):
    if not stack.kubernetes:
        pytest.skip("Kubernetes-only test (the stack is Docker Compose)")
    return stack


# -- app side (request + matching service) ------------------------------------------------------

@pytest.fixture
def app(_reachable):
    """The client API of the running stack (scripts/_demo_app.py: the same helpers the
    demos use). Skips if the request service isn't reachable. Starts every test with
    the Port Blair test area emptied of responders left over from earlier runs."""
    import _demo_app

    try:
        _demo_app.api.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no request service at {_demo_app.API_URL} ({exc}); start the stack first")
    _demo_app.retire_leftovers()
    return _demo_app
