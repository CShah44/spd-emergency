"""Leader election between coordinator replicas. Roadmap task 3.

Two coordinators run at once; exactly one of them (the LEADER) does the work.
They agree through a Kubernetes Lease, a small record in the Kubernetes API:

    holderIdentity        who leads (the pod name)
    renewTime             when the leader last said "still here"
    leaseDurationSeconds  how long that promise lasts
    leaseTransitions      how many times leadership has changed hands

The rules (the same ones Kubernetes' own controllers use):
  * The leader rewrites renewTime every RETRY_PERIOD. If it can't for
    RENEW_DEADLINE, it stops acting as leader, BEFORE the lease runs out.
  * A standby takes over only when it has seen no change to the lease for a
    full LEASE_DURATION (or the leader released it on a clean shutdown).
  * Every write to the lease carries the resourceVersion it read, so two
    standbys racing for an expired lease can't both win: the API rejects the
    second write (409).

Expiry is judged by THIS process's clock ("I haven't seen the lease change for
N seconds"), never by comparing renewTime with our clock, so the two pods'
clocks don't need to agree.

The EPOCH is leaseTransitions: it goes up by one at every handover. The
coordinator sends it to storage nodes, which refuse an older one (fencing, see
storage_node/app/main.py). A leader that froze and woke up after losing the
lease is still running with the old epoch, so its writes are refused.

Routing: the leader labels its own pod coordinator-role=leader and the
coordinator Service selects that label, so traffic goes to the leader. (Not
the readiness probe: a pod that is never ready would block rolling updates,
since Kubernetes won't remove an old pod until a new one is ready.)
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

import httpx

log = logging.getLogger("coordinator.election")

# The leader's pod carries this label; the coordinator Service selects on it.
LEADER_LABEL = "coordinator-role"


class LeaseConflict(Exception):
    """Someone else changed (or created) the lease since we read it."""


@dataclass(frozen=True)
class Lease:
    holder: str  # "" = nobody (released)
    duration_s: int
    transitions: int
    resource_version: str = ""


class LeaseBackend(Protocol):
    async def get(self) -> Lease | None: ...
    async def create(self, lease: Lease) -> Lease: ...  # LeaseConflict if it exists
    async def update(self, lease: Lease) -> Lease: ...  # LeaseConflict if resource_version is stale


class MemoryLeaseBackend:
    """A lease in memory, shared by the coordinators of one test process."""

    def __init__(self):
        self._lease: Lease | None = None
        self._version = 0
        self.available = True  # set False to simulate the Kubernetes API being unreachable

    def _check(self) -> None:
        if not self.available:
            raise ConnectionError("lease API unreachable")

    async def get(self) -> Lease | None:
        self._check()
        return self._lease

    async def create(self, lease: Lease) -> Lease:
        self._check()
        if self._lease is not None:
            raise LeaseConflict("lease exists")
        return self._store(lease)

    async def update(self, lease: Lease) -> Lease:
        self._check()
        if self._lease is None or self._lease.resource_version != lease.resource_version:
            raise LeaseConflict("stale resourceVersion")
        return self._store(lease)

    def _store(self, lease: Lease) -> Lease:
        self._version += 1
        self._lease = replace(lease, resource_version=str(self._version))
        return self._lease


class KubernetesLeaseBackend:
    """coordination.k8s.io/v1 Lease through the Kubernetes REST API, using the
    pod's service account (needs a Role allowing get/create/update on leases)."""

    SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, name: str, pod_name: str, namespace: str | None = None):
        self.name = name
        self.pod_name = pod_name  # our own pod, which gets the leader label
        self.namespace = namespace or _read(f"{self.SA_DIR}/namespace").strip()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._http = httpx.AsyncClient(
            base_url=f"https://{host}:{port}", verify=f"{self.SA_DIR}/ca.crt", timeout=3.0
        )
        self._path = f"/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases"

    def _headers(self) -> dict[str, str]:
        # Read the token every time: Kubernetes rotates it.
        return {"Authorization": f"Bearer {_read(f'{self.SA_DIR}/token').strip()}"}

    async def get(self) -> Lease | None:
        resp = await self._http.get(f"{self._path}/{self.name}", headers=self._headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _from_k8s(resp.json())

    async def create(self, lease: Lease) -> Lease:
        resp = await self._http.post(self._path, json=self._body(lease), headers=self._headers())
        if resp.status_code == 409:
            raise LeaseConflict("lease exists")
        resp.raise_for_status()
        return _from_k8s(resp.json())

    async def update(self, lease: Lease) -> Lease:
        resp = await self._http.put(f"{self._path}/{self.name}", json=self._body(lease), headers=self._headers())
        if resp.status_code == 409:
            raise LeaseConflict("stale resourceVersion")
        resp.raise_for_status()
        return _from_k8s(resp.json())

    # -- routing: the Service sends traffic to the pod labelled as leader ---------------

    async def set_leader_label(self, leader: bool) -> None:
        """Put LEADER_LABEL on our own pod (leader) or take it off (standby). On
        taking over, also take it off any other pod still carrying it."""
        pod = self.pod_name
        await self._patch_label(pod, leader)
        if leader:
            resp = await self._http.get(
                f"/api/v1/namespaces/{self.namespace}/pods",
                params={"labelSelector": f"{LEADER_LABEL}=leader"},
                headers=self._headers(),
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                if item["metadata"]["name"] != pod:
                    await self._patch_label(item["metadata"]["name"], False)

    async def _patch_label(self, pod: str, leader: bool) -> None:
        resp = await self._http.patch(
            f"/api/v1/namespaces/{self.namespace}/pods/{pod}",
            json={"metadata": {"labels": {LEADER_LABEL: "leader" if leader else None}}},
            headers={**self._headers(), "Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code != 404:  # a pod that is already gone needs no label change
            resp.raise_for_status()

    def _body(self, lease: Lease) -> dict:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # MicroTime
        metadata = {"name": self.name, "namespace": self.namespace}
        if lease.resource_version:
            metadata["resourceVersion"] = lease.resource_version
        return {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": metadata,
            "spec": {
                "holderIdentity": lease.holder,
                "leaseDurationSeconds": lease.duration_s,
                "leaseTransitions": lease.transitions,
                "renewTime": now,
                "acquireTime": now,
            },
        }

    async def close(self) -> None:
        await self._http.aclose()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _from_k8s(obj: dict) -> Lease:
    spec = obj.get("spec") or {}
    return Lease(
        holder=spec.get("holderIdentity") or "",
        duration_s=int(spec.get("leaseDurationSeconds") or 0),
        transitions=int(spec.get("leaseTransitions") or 0),
        resource_version=obj["metadata"]["resourceVersion"],
    )


class LeaderElector:
    """Keeps trying to become (or stay) leader. Read `is_leader()` and `epoch`."""

    def __init__(
        self,
        backend: LeaseBackend,
        identity: str,
        lease_duration_s: float = 6.0,
        renew_deadline_s: float = 4.0,
        retry_period_s: float = 1.0,
        on_change: Callable[[bool, int | None], Awaitable[None] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not retry_period_s < renew_deadline_s < lease_duration_s:
            raise ValueError("need retry_period < renew_deadline < lease_duration")
        self.backend = backend
        self.identity = identity
        self.lease_duration_s = lease_duration_s
        self.renew_deadline_s = renew_deadline_s
        self.retry_period_s = retry_period_s
        self.on_change = on_change
        self._clock = clock
        self.epoch: int | None = None
        self._leading = False
        self._announced = False  # the last state reported through on_change
        self._last_renew = float("-inf")
        self._observed: Lease | None = None
        self._observed_at = float("-inf")
        self._stopped = False

    def is_leader(self) -> bool:
        """Leader AND renewed recently. The second part matters after a freeze:
        a process that wakes up must not act on a lease it may have lost."""
        return self._leading and self._clock() - self._last_renew < self.renew_deadline_s

    def holder(self) -> str | None:
        return self._observed.holder if self._observed else None

    async def run(self) -> None:
        while not self._stopped:
            await self.tick()
            await asyncio.sleep(self.retry_period_s)

    async def tick(self) -> None:
        """One round: acquire or renew, then report a change of leadership.

        A change is judged against what we last ANNOUNCED, not against
        is_leader() before this round: after a freeze, is_leader() has already
        turned False by the clock, and the loss must still be announced (so the
        coordinator drops its registry and its leader label)."""
        try:
            renewed = await self._try_acquire_or_renew()
        except LeaseConflict:
            renewed = False  # lost a race; the next round reads the winner
        except Exception as exc:  # noqa: BLE001  (API unreachable: keep trying, deadline decides)
            log.warning("lease API error: %r", exc)
            renewed = None
        if renewed:
            self._leading = True
            self._last_renew = self._clock()
        elif renewed is False:
            self._leading = False
        leading = self.is_leader()
        if leading != self._announced:
            await self._notify(leading)

    async def step_down(self, reason: str) -> None:
        """Stop acting as leader now (a storage node said our epoch is old)."""
        if self._leading or self._announced:
            log.warning("stepping down: %s", reason)
            self._leading = False
            await self._notify(False)

    async def release(self) -> None:
        """Clean shutdown: hand the lease back so a standby takes over at once
        instead of waiting for it to expire."""
        self._stopped = True
        if not self._leading:
            return
        self._leading = self._announced = False
        try:
            lease = await self.backend.get()
            if lease is not None and lease.holder == self.identity:
                await self.backend.update(replace(lease, holder="", duration_s=1))
                log.info("released the lease")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not release the lease: %r", exc)

    async def _try_acquire_or_renew(self) -> bool:
        lease = await self.backend.get()
        if lease is None:
            new = await self.backend.create(Lease(self.identity, int(self.lease_duration_s), 0))
            return self._took(new)

        if self._observed is None or lease.resource_version != self._observed.resource_version:
            self._observed, self._observed_at = lease, self._clock()  # it changed: restart the timer
        held_by_other = lease.holder not in ("", self.identity)
        if held_by_other and self._clock() - self._observed_at < max(lease.duration_s, 1):
            return False  # someone else leads and is still renewing

        transitions = lease.transitions if lease.holder == self.identity else lease.transitions + 1
        new = await self.backend.update(
            replace(lease, holder=self.identity, duration_s=int(self.lease_duration_s), transitions=transitions)
        )
        return self._took(new)

    def _took(self, lease: Lease) -> bool:
        if self.epoch != lease.transitions:
            log.warning("%s leads the coordinators now (epoch %d)", self.identity, lease.transitions)
        self.epoch = lease.transitions
        self._observed, self._observed_at = lease, self._clock()
        return True

    async def _notify(self, leading: bool) -> None:
        self._announced = leading
        if self.on_change is not None:
            result = self.on_change(leading, self.epoch)
            if asyncio.iscoroutine(result):
                await result
