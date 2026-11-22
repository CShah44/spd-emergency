"""What the Delta Command page shows, as plain functions with no web code in them:
the colors, which buttons each status gets, and the "what just happened" feed
(found by comparing one refresh with the previous one).

Kept apart from main.py so it can be tested without a browser or NiceGUI.
"""

from dataclasses import dataclass
from typing import Any

from common import config
from common.models import REQUEST_TRANSITIONS, RequestStatus, ResourceStatus, Type
from common.sharding import request_key, resource_key, shard_for

TYPES = [t.value for t in Type]

# The seed data's cities (scripts/_demo.py), so the page's presets match the sample responders.
CITIES = {
    "Bengaluru": (12.9716, 77.5946),
    "Mumbai": (19.0760, 72.8777),
    "Chennai": (13.0827, 80.2707),
    "Kolkata": (22.5726, 88.3639),
    "Ahmedabad": (23.0225, 72.5714),
}

YELLOW, BLUE, CYAN, GREEN, ORANGE, RED, GREY = "#ffcf1f", "#2f7bff", "#35d0e6", "#2ecc71", "#ff7a3b", "#ff3b3b", "#7b86a8"

REQUEST_COLORS = {
    RequestStatus.PENDING: YELLOW, RequestStatus.MATCHED: BLUE, RequestStatus.IN_PROGRESS: CYAN,
    RequestStatus.FULFILLED: GREEN, RequestStatus.UNMATCHED: ORANGE, RequestStatus.CANCELLED: GREY,
}
RESOURCE_COLORS = {ResourceStatus.AVAILABLE: GREEN, ResourceStatus.ASSIGNED: BLUE, ResourceStatus.OFFLINE: GREY}
NODE_COLORS = {"UP": GREEN, "RECOVERING": YELLOW, "DOWN": RED}


def color_of(status: str, palette: dict) -> str:
    return palette.get(status, GREY)


# -- which shard a record lives on -----------------------------------------------------------------------

SHARD_COLORS = ["#4da3ff", "#c792ff", "#5ee0b0"]  # one outline color per shard


def shard_of(kind: str, record_id: str) -> int:
    """The shard that stores this record: the same rule the coordinator uses (sha1 of the key, mod 3).
    `kind` is "request" or "resource"."""
    key = request_key(record_id) if kind == "request" else resource_key(record_id)
    return shard_for(key, config.NUM_SHARDS)


def shard_color(shard: int) -> str:
    return SHARD_COLORS[shard % len(SHARD_COLORS)]


def shard_health(view: dict[str, Any] | None) -> dict[int, str]:
    """"ok", "degraded" (one node is down or silent: the other one is serving) or "down"
    (no live node) for every shard in the cluster view."""
    health = {}
    for shard in (view or {"shards": []})["shards"]:
        if not shard["available"]:
            health[shard["shard"]] = "down"
        elif any(n["state"] != "UP" or n["silent"] for n in shard["nodes"]):
            health[shard["shard"]] = "degraded"
        else:
            health[shard["shard"]] = "ok"
    return health


# -- the life of a request -------------------------------------------------------------------------------

PIPELINE = ["PENDING", "MATCHED", "IN_PROGRESS", "FULFILLED"]  # the happy path, left to right
SIDE_STATUSES = ["UNMATCHED", "CANCELLED"]


def is_finished(status: str) -> bool:
    """FULFILLED and CANCELLED: no moves left."""
    return not REQUEST_TRANSITIONS[RequestStatus(status)]


def lifecycle(status: str) -> list[dict[str, str]]:
    """The four steps of the happy path, each "done", "current" or "todo", for the little progress bar
    in every request row. UNMATCHED waits at the first step; CANCELLED never got further."""
    if status == "CANCELLED":
        return [{"label": s, "state": "todo", "color": GREY} for s in PIPELINE]
    at = 0 if status == "UNMATCHED" else PIPELINE.index(status)
    steps = []
    for index, step in enumerate(PIPELINE):
        state = "done" if index < at or status == "FULFILLED" else "current" if index == at else "todo"
        color = ORANGE if status == "UNMATCHED" and state == "current" else color_of(step, REQUEST_COLORS)
        steps.append({"label": step, "state": state, "color": color})
    return steps


# -- buttons ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    label: str
    kind: str  # "match" (POST /match/{id}) or "status" (PUT .../status)
    target: str | None = None
    tone: str = "primary"  # "primary" (blue), "positive" (green), "warning" (yellow) or "negative" (red outline)


def request_actions(status: str) -> list[Action]:
    cancel = Action("Cancel", "status", "CANCELLED", "negative")
    return {
        "PENDING": [Action("Match now", "match"), cancel],
        "UNMATCHED": [Action("Retry", "status", "PENDING", "warning"), cancel],
        "MATCHED": [Action("Start", "status", "IN_PROGRESS"), cancel],
        "IN_PROGRESS": [Action("Fulfil", "status", "FULFILLED", "positive"), cancel],
    }.get(status, [])


def resource_actions(status: str) -> list[Action]:
    return {
        "AVAILABLE": [Action("Take offline", "status", "OFFLINE", "negative")],
        "OFFLINE": [Action("Bring online", "status", "AVAILABLE", "positive")],
    }.get(status, [])  # ASSIGNED resources are freed when their request ends


# -- small formatting helpers -------------------------------------------------------------------------------

def short_id(record_id: str) -> str:
    return record_id[:12]


def clock(iso: str | None) -> str:
    """'2026-11-22T06:00:37Z' -> '06:00:37' (UTC)."""
    return iso[11:19] if iso and len(iso) >= 19 else "-"


# -- the cluster panel ----------------------------------------------------------------------------------------------

def _recovery_note(report: dict | None) -> str | None:
    if not report:
        return None
    if report.get("partner") is None:
        return "recovered on its own data"
    return f"caught up: copied {report.get('pulled', 0)} from {report['partner']} in {report.get('seconds', 0) * 1000:.0f} ms"


def cluster_view(cluster: dict[str, Any]) -> dict[str, Any]:
    """The parts of the coordinator's status the panel draws. Left out on purpose: the
    heartbeat ages, which change every second and would redraw the panel for nothing."""
    shards_info = cluster.get("shards", {})
    shards = []
    for shard in sorted({n["shard"] for n in cluster.get("nodes", [])}):
        leader = shards_info.get(str(shard), {}).get("acting_primary")
        nodes = [
            {
                "id": n["node_id"], "role": n["role"], "state": n["state"],
                "leader": n["node_id"] == leader if leader else bool(n.get("is_leader")),
                "keys": n.get("keys"), "recovery": _recovery_note(n.get("last_recovery")),
                # The coordinator asks every node it believes is alive for its key count. No answer
                # means the node has gone quiet but is not marked DOWN yet (that takes up to
                # 10 s of missed heartbeats). Only the real coordinator reports key counts.
                "silent": cluster.get("mode") == "real" and n["state"] != "DOWN" and n.get("keys") is None,
            }
            for n in cluster["nodes"] if n["shard"] == shard
        ]
        shards.append({"shard": shard, "available": bool(shards_info.get(str(shard), {}).get("available", True)),
                       "leader": next((n["id"] for n in nodes if n["leader"]), None), "nodes": nodes})
    return {"shards": shards, "queue": cluster.get("queue"), "controls": bool(cluster.get("controls_enabled")),
            "total_keys": cluster.get("total_keys")}


def confirm_silent(view: dict[str, Any], streaks: dict[str, int], needed: int = 2) -> dict[str, Any]:
    """A node that misses one status check may only be busy. Call it silent after `needed` in a row.
    `streaks` is the caller's memory between refreshes."""
    shards = []
    for shard in view["shards"]:
        nodes = []
        for node in shard["nodes"]:
            streaks[node["id"]] = streaks.get(node["id"], 0) + 1 if node["silent"] else 0
            nodes.append({**node, "silent": streaks[node["id"]] >= needed})
        shards.append({**shard, "nodes": nodes})
    return {**view, "shards": shards}


# -- the "what just happened" feed -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    when: str
    text: str
    color: str
    subject: str | None = None  # the record the row flashes for


def diff_requests(old: dict[str, dict] | None, new: dict[str, dict], resource_names: dict[str, str],
                  when: str) -> list[Event]:
    """Compare two refreshes. `old` is None on the first one: nothing to report yet."""
    if old is None:
        return []
    events = []
    for rid, req in new.items():
        before = old.get(rid)
        if before is None:
            # Matching is quick: the first time we see a request it may already be MATCHED.
            text = f"{short_id(rid)}  new {req['type']} request"
            if req["status"] != "PENDING":
                text += f"  ->  {req['status']}{_assigned_note(req, resource_names)}"
            events.append(Event(when, text, color_of(req["status"], REQUEST_COLORS), rid))
        elif before["status"] != req["status"]:
            text = f"{short_id(rid)}  {before['status']} -> {req['status']}{_assigned_note(req, resource_names)}"
            events.append(Event(when, text, color_of(req["status"], REQUEST_COLORS), rid))
    return events


def _assigned_note(request: dict, resource_names: dict[str, str]) -> str:
    assigned = request.get("assigned_resource_id")
    if request["status"] != "MATCHED" or not assigned:
        return ""
    return f"  (assigned: {resource_names.get(assigned, short_id(assigned))})"


def diff_resources(old: dict[str, dict] | None, new: dict[str, dict], when: str) -> list[Event]:
    if old is None:
        return []
    events = []
    for rid, res in new.items():
        before = old.get(rid)
        if before is None:
            events.append(Event(when, f"new responder: {res['name']} ({res['type']})", GREEN, rid))
        elif before["status"] != res["status"]:
            events.append(Event(when, f"{res['name']}  {before['status']} -> {res['status']}",
                                color_of(res["status"], RESOURCE_COLORS), rid))
    return events


def diff_cluster(old: dict | None, new: dict, when: str) -> list[Event]:
    if old is None:
        return []
    before = {n["id"]: n for s in old["shards"] for n in s["nodes"]}
    events = []
    for shard in new["shards"]:
        for node in shard["nodes"]:
            was = before.get(node["id"])
            if was is not None and was["state"] != node["state"]:
                events.append(Event(when, f"storage node {node['id']}  {was['state']} -> {node['state']}",
                                    color_of(node["state"], NODE_COLORS)))
            elif was is not None and node["silent"] and not was["silent"]:
                events.append(Event(when, f"storage node {node['id']} stopped answering (not marked DOWN yet)", ORANGE))
        old_leader = next((s["leader"] for s in old["shards"] if s["shard"] == shard["shard"]), None)
        if old_leader and shard["leader"] and old_leader != shard["leader"]:
            events.append(Event(when, f"shard {shard['shard']} is now led by {shard['leader']}", BLUE))
    return events
