"""Everything the browser is sent, built from what the request service answered.

The page holds no logic of its own about the system: every refresh is turned into one
JSON "snapshot" here (records with their shard and their allowed buttons, the cluster
view, the feed of what just happened). Plain Python, no web code, so it is tested
without a browser.
"""

import itertools
from typing import Any

from . import model

FEED_LENGTH = 80  # events kept in the "what just happened" feed
RESOURCE_ORDER = {"ASSIGNED": 0, "AVAILABLE": 1, "OFFLINE": 2}


def geo_cell(record_id: str) -> str | None:
    """New ids carry their geohash cell: 'req-te7-9c55...' -> 'te7'. Older ids have none."""
    parts = record_id.split("-")
    return parts[1] if len(parts) == 3 else None


def _actions(actions: list[model.Action]) -> list[dict[str, Any]]:
    return [{"label": a.label, "kind": a.kind, "target": a.target, "tone": a.tone} for a in actions]


class State:
    """The last answers from the request service, and the feed built by comparing each one
    with the one before. One per process: every open tab sees the same picture."""

    def __init__(self) -> None:
        self.link: dict[str, Any] = {"ok": False, "note": "connecting"}
        self.requests: list[dict] = []
        self.resources: list[dict] = []
        self.summary: dict[str, int] | None = None
        self.view: dict | None = None
        self.shard_health: dict[int, str] = {}
        self.events: list[dict[str, Any]] = []
        self._requests_by_id: dict | None = None  # None until the first answer: nothing to compare yet
        self._resources_by_id: dict | None = None
        self._resource_names: dict[str, str] = {}
        self._silent_streaks: dict[str, int] = {}
        self._event_ids = itertools.count(1)

    # -- taking in answers (each returns True when something the page shows changed) ------------------

    def set_link(self, ok: bool, note: str = "") -> bool:
        link = {"ok": ok, "note": note}
        changed = link != self.link
        self.link = link
        return changed

    def apply_requests(self, requests: list[dict], when: str) -> bool:
        new = {r["id"]: r for r in requests}
        events = model.diff_requests(self._requests_by_id, new, self._resource_names, when)
        self._requests_by_id = new
        self._push(events, "request")
        changed = requests != self.requests
        self.requests = requests
        return changed or bool(events)

    def apply_resources(self, resources: list[dict], when: str) -> bool:
        resources = sorted(resources, key=lambda r: (RESOURCE_ORDER.get(r["status"], 3), r["name"]))
        new = {r["id"]: r for r in resources}
        events = model.diff_resources(self._resources_by_id, new, when)
        self._resources_by_id = new
        self._resource_names = {rid: r["name"] for rid, r in new.items()}
        self._push(events, "responder")
        changed = resources != self.resources
        self.resources = resources
        return changed or bool(events)

    def apply_summary(self, summary: dict[str, int]) -> bool:
        changed = summary != self.summary
        self.summary = summary
        return changed

    def apply_cluster(self, cluster: dict, when: str) -> bool:
        view = model.confirm_silent(model.cluster_view(cluster), self._silent_streaks)
        events = model.diff_cluster(self.view, view, when)
        self._push(events, "storage")
        changed = view != self.view
        self.view = view
        self.shard_health = model.shard_health(view)
        return changed or bool(events)

    def _push(self, events: list[model.Event], kind: str) -> None:
        fresh = [
            {"id": next(self._event_ids), "when": e.when, "text": e.text, "color": e.color,
             "kind": _event_kind(kind, e.text), "subject": e.subject}
            for e in events
        ]
        if fresh:
            self.events = (list(reversed(fresh)) + self.events)[:FEED_LENGTH]

    # -- what the browser gets -------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "link": self.link,
            "summary": self.summary,
            "requests": [self._request(r) for r in self.requests],
            "resources": [self._resource(r) for r in self.resources],
            "cluster": self.view,
            "shard_health": {str(k): v for k, v in self.shard_health.items()},
            "events": self.events,
        }

    def _request(self, r: dict) -> dict[str, Any]:
        assigned = r.get("assigned_resource_id")
        location = r.get("location") or {}
        return {
            "id": r["id"], "short": model.short_id(r["id"]), "cell": geo_cell(r["id"]),
            "type": r["type"], "status": r["status"], "finished": model.is_finished(r["status"]),
            "clock": model.clock(r.get("created_at")), "details": r.get("details"), "quantity": r.get("quantity"),
            "lat": location.get("lat"), "lon": location.get("lon"),
            "assigned": {"id": assigned, "name": self._resource_names.get(assigned, model.short_id(assigned))}
            if assigned else None,
            "shard": model.shard_of("request", r["id"]),
            "lifecycle": [s["state"] for s in model.lifecycle(r["status"])],
            "actions": _actions(model.request_actions(r["status"])),
        }

    def _resource(self, r: dict) -> dict[str, Any]:
        serving_id = r.get("assigned_request_id")
        serving = None
        if serving_id:
            req = (self._requests_by_id or {}).get(serving_id)
            serving = {"id": serving_id, "short": model.short_id(serving_id), "status": req["status"] if req else None}
        location = r.get("location") or {}
        return {
            "id": r["id"], "short": model.short_id(r["id"]), "cell": geo_cell(r["id"]),
            "name": r["name"], "type": r["type"], "status": r["status"], "capacity": r.get("capacity"),
            "lat": location.get("lat"), "lon": location.get("lon"),
            "shard": model.shard_of("resource", r["id"]), "serving": serving,
            "actions": _actions(model.resource_actions(r["status"])),
        }


def _event_kind(kind: str, text: str) -> str:
    """Which part of the system the event belongs to, so the feed can tag it with that part's color:
    'request' (request service), 'matching', 'responder', 'storage' or 'coordinator'."""
    if kind == "request" and "->" in text:
        became = text.rsplit("->", 1)[1].split()[0]
        if became in ("MATCHED", "UNMATCHED"):  # only the matcher sets these
            return "matching"
    if kind == "storage" and "led by" in text:
        return "coordinator"
    return kind
