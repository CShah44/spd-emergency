"""The Delta Command web page: a live view of the whole system, and the controls to
drive a demo from. Written in Python with NiceGUI (no JavaScript to maintain).

From top to bottom the page follows the path of a request:
  1. the storage cluster (where records are kept)
  2. matching: the queue, the requests by stage, and what just happened
  3. the forms, and the responders (who is assigned to what)
  4. the requests: active ones first, finished ones below

It only ever talks to the request service over HTTP, like the CLI does, and polls it:
  every 1 s  the requests, their counts per status, the storage cluster and the queue
  every 3 s  the list of responders

Each browser tab gets its own Dashboard. A panel is redrawn only when its data
changed, so buttons stay put between refreshes.

Run locally (needs the request service on API_URL):
    python -m app.main          # from services/ui_service, with ../ on PYTHONPATH
"""

import asyncio
import functools
import os
import random
from datetime import datetime, timezone

from nicegui import app, ui

from . import model
from .api import ApiError, DeltaApi
from .theme import CSS, MUTED

api = DeltaApi()

FEED_LENGTH = 60  # events kept in the "what just happened" list
REQUESTS_SHOWN = 100
RESOURCES_SHOWN = 100
BURST_SIZE = 20
POLL_FAST_S = 1.0
POLL_SLOW_S = 3.0

BUTTON_COLORS = {"primary": "primary", "positive": "positive", "warning": "accent", "negative": "negative"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def chip(text: str, color: str) -> ui.label:
    return ui.label(text).classes("chip").style(f"background:{color}")


def stat(label: str, value: int | str, color: str) -> None:
    with ui.column().classes("gap-1"):
        ui.label(str(value)).classes("big-number mono").style(f"color:{color}")
        ui.label(label).classes("small muted")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Dashboard:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.resources: list[dict] = []
        self.summary: dict[str, int] | None = None  # requests per status
        self.summary_flash: set[str] = set()  # stages whose count just changed
        self.view: dict | None = None  # model.cluster_view(...) of the last refresh
        self.shard_health: dict[int, str] = {}
        self.events: list[model.Event] = []
        self.flash: set[str] = set()  # rows that just changed
        self._requests_by_id: dict | None = None  # None until the first refresh: nothing to compare yet
        self._resources_by_id: dict | None = None
        self._resource_names: dict[str, str] = {}
        self._silent_streaks: dict[str, int] = {}
        # One refreshable per panel and per Dashboard, so tabs never redraw each other.
        self.cluster_panel = ui.refreshable(self._cluster_panel)
        self.queue_panel = ui.refreshable(self._queue_panel)
        self.pipeline_panel = ui.refreshable(self._pipeline_panel)
        self.feed = ui.refreshable(self._feed)
        self.requests_table = ui.refreshable(self._requests_table)
        self.resources_table = ui.refreshable(self._resources_table)

    # -- layout -----------------------------------------------------------------------------------

    def build(self) -> None:
        ui.dark_mode(True)
        ui.add_css(CSS)
        ui.colors(primary="#2f7bff", secondary="#35d0e6", accent="#ffcf1f",
                  positive="#2ecc71", negative="#ff3b3b", warning="#ffcf1f")

        with ui.header().classes("topbar items-center justify-between px-6 py-2"):
            with ui.row().classes("items-baseline gap-4"):
                ui.label("S.P.D. EMERGENCY!").classes("brand")
                ui.label("DELTA COMMAND").classes("brand-sub")
            self.link = ui.label("connecting...").classes("small mono").style(f"color:{MUTED}")

        with ui.column().classes("w-full mx-auto gap-5 p-4").style("max-width:96rem"):
            # 1. where records are kept
            with ui.element("div").classes("panel w-full"):
                ui.label("Storage cluster: 3 shards, a primary and a replica each").classes("panel-title")
                self.cluster_panel()

            # 2. matching
            with ui.element("div").classes("w-full grid grid-cols-1 lg:grid-cols-3 gap-5"):
                with ui.element("div").classes("panel"):
                    ui.label("Matching: the queue").classes("panel-title")
                    self.queue_panel()
                with ui.element("div").classes("panel"):
                    ui.label("Matching: requests by stage").classes("panel-title")
                    self.pipeline_panel()
                with ui.element("div").classes("panel"):
                    ui.label("What just happened (UTC)").classes("panel-title")
                    self.feed()

            # 3. the forms, and who is assigned to what
            with ui.element("div").classes("w-full grid grid-cols-1 lg:grid-cols-3 gap-5"):
                with ui.column().classes("gap-5 w-full"):
                    self._request_form()
                    self._resource_form()
                    self._burst_card()
                with ui.element("div").classes("panel lg:col-span-2"):
                    ui.label("Responders: who is assigned to what").classes("panel-title")
                    self.resources_table()

            # 4. the requests
            with ui.element("div").classes("panel w-full"):
                ui.label("Emergency requests: active first, finished ones below").classes("panel-title")
                self.requests_table()

        ui.timer(POLL_FAST_S, self.refresh_fast)
        ui.timer(POLL_SLOW_S, self.refresh_slow)

    # -- the forms ----------------------------------------------------------------------------------

    def _location_inputs(self, prefix: str) -> None:
        """A city preset plus latitude and longitude. Picking a city fills in its coordinates."""
        city = ui.select(list(model.CITIES) + ["Custom"], value="Bengaluru", label="Where").classes("w-full")
        with ui.row().classes("w-full no-wrap gap-2"):
            lat = ui.number("Latitude", value=model.CITIES["Bengaluru"][0], format="%.4f").classes("w-full")
            lon = ui.number("Longitude", value=model.CITIES["Bengaluru"][1], format="%.4f").classes("w-full")

        def city_changed(event) -> None:
            if event.value in model.CITIES:
                lat.value, lon.value = model.CITIES[event.value]

        city.on_value_change(city_changed)
        setattr(self, f"{prefix}_lat", lat)
        setattr(self, f"{prefix}_lon", lon)

    def _request_form(self) -> None:
        with ui.element("div").classes("panel w-full"):
            ui.label("Report an emergency").classes("panel-title")
            self.req_type = ui.select(model.TYPES, value="water", label="What is needed").classes("w-full")
            self._location_inputs("req")
            self.req_quantity = ui.number("Quantity (optional)", value=None, min=1, precision=0).classes("w-full")
            self.req_details = ui.input("Details (optional)").props("maxlength=500").classes("w-full")
            ui.button("Send to Delta Command", on_click=self.submit_request) \
                .props("unelevated no-caps size=lg color=primary").classes("w-full mt-2 action-btn")

    def _resource_form(self) -> None:
        with ui.element("div").classes("panel w-full"):
            ui.label("Register a responder").classes("panel-title")
            self.res_type = ui.select(model.TYPES, value="water", label="What it offers").classes("w-full")
            self.res_name = ui.input("Name (a volunteer, NGO, truck...)").classes("w-full")
            self._location_inputs("res")
            self.res_capacity = ui.number("Capacity (optional)", value=None, min=1, precision=0).classes("w-full")
            ui.button("Register", on_click=self.submit_resource) \
                .props("unelevated no-caps size=lg color=positive").classes("w-full mt-2 action-btn")

    def _burst_card(self) -> None:
        with ui.element("div").classes("panel w-full"):
            ui.label("Stress test").classes("panel-title")
            ui.label(f"Send {BURST_SIZE} random requests at once and watch the queue fill and drain.") \
                .classes("small muted")
            self.burst_button = ui.button(f"Send {BURST_SIZE} requests", on_click=self.send_burst) \
                .props("unelevated no-caps size=lg color=accent text-color=dark").classes("w-full mt-2 action-btn")

    # -- panels (each one redraws only when its data changed) -------------------------------------------------

    def _cluster_panel(self) -> None:
        if self.view is None:
            ui.label("Waiting for the storage cluster...").classes("muted")
            return
        with ui.element("div").classes("w-full grid grid-cols-1 md:grid-cols-3 gap-4"):
            for shard in self.view["shards"]:
                with ui.column().classes("gap-3 w-full"):
                    with ui.row().classes("items-center justify-between w-full no-wrap"):
                        ui.label(f"SHARD {shard['shard']}").classes("font-bold") \
                            .style(f"color:{model.shard_color(shard['shard'])}; font-size:1.1rem")
                        if not shard["available"]:
                            chip("NO LIVE NODE", model.RED)
                    for node in shard["nodes"]:
                        self._node_card(node)
        total = self.view.get("total_keys")
        if total is not None:
            ui.label(f"{total} records stored in total (each one lives on exactly two nodes)") \
                .classes("small muted mt-4")

    def _node_card(self, node: dict) -> None:
        with ui.element("div").classes("node w-full" + (" leader" if node["leader"] else "")):
            with ui.row().classes("items-center justify-between w-full no-wrap"):
                ui.label(node["id"]).classes("mono node-name")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    if node["silent"]:
                        chip("NOT ANSWERING", model.ORANGE)
                    chip(node["state"], model.color_of(node["state"], model.NODE_COLORS))
            with ui.row().classes("items-center justify-between w-full no-wrap mt-1"):
                role = f"{node['role']} - LEADER" if node["leader"] else node["role"]
                ui.label(role).classes("font-bold").style(f"color:{model.YELLOW}" if node["leader"] else f"color:{MUTED}")
                ui.label("" if node["keys"] is None else f"{node['keys']} keys").classes("muted mono")
            if node["recovery"]:
                ui.label(node["recovery"]).classes("small muted mt-1")
            if self.view["controls"]:
                if node["state"] == "UP" and not node["silent"]:
                    ui.button("Pause (simulate a failure)", on_click=functools.partial(self.toggle_node, node["id"], True)) \
                        .props("unelevated no-caps color=negative").classes("action-btn w-full mt-2")
                elif node["state"] == "DOWN" or node["silent"]:
                    ui.button("Resume", on_click=functools.partial(self.toggle_node, node["id"], False)) \
                        .props("unelevated no-caps color=positive").classes("action-btn w-full mt-2")
                else:
                    ui.button("Catching up...").props("outline no-caps disable").classes("action-btn w-full mt-2")

    def _queue_panel(self) -> None:
        if self.view is None:
            ui.label("Waiting for RabbitMQ...").classes("muted")
            return
        queue = self.view["queue"]
        if queue is None:
            chip("RABBITMQ UNREACHABLE", model.RED)
            ui.label("New requests are still saved. The matching service's sweeper picks them up "
                     "about a minute later.").classes("small muted mt-2")
            return
        with ui.row().classes("gap-8"):
            stat("waiting to be matched", queue["waiting"], model.YELLOW if queue["waiting"] else "#e6ebff")
            stat("matchers connected", queue["consumers"], model.GREEN if queue["consumers"] else model.RED)
            stat("dead-lettered", queue["dead_lettered"], model.RED if queue["dead_lettered"] else "#e6ebff")

    def _pipeline_panel(self) -> None:
        if self.summary is None:
            ui.label("Waiting for requests...").classes("muted")
            return
        with ui.element("div").classes("pipeline"):
            for index, status in enumerate(model.PIPELINE):
                if index:
                    ui.label("->").classes("arrow mono")
                self._stage(status)
        with ui.row().classes("gap-3 mt-4 items-center"):
            for status in model.SIDE_STATUSES:
                self._stage(status)
            ui.label(f"{self.summary['total']} requests in all").classes("muted small")

    def _stage(self, status: str) -> None:
        color = model.color_of(status, model.REQUEST_COLORS)
        with ui.element("div").classes("stat-block" + (" flash" if status in self.summary_flash else "")) \
                .style(f"border-color:{color}"):
            ui.label(str(self.summary.get(status, 0))).classes("big-number mono").style(f"color:{color}")
            ui.label(status.replace("_", " ")).classes("small font-bold").style(f"color:{color}")

    def _feed(self) -> None:
        if not self.events:
            ui.label("Nothing yet. Send a request.").classes("muted small")
            return
        with ui.column().classes("w-full gap-0 scroll-box").style("max-height:13rem"):
            for event in self.events:
                with ui.row().classes("items-baseline gap-2 no-wrap w-full") \
                        .style("padding:4px 0; border-bottom:1px solid #22336a"):
                    ui.label(event.when).classes("mono small muted")
                    ui.label(event.text).classes("small font-bold").style(f"color:{event.color}")

    # -- the requests table -------------------------------------------------------------------------------

    def _requests_table(self) -> None:
        if not self.requests:
            ui.label("No requests yet. Report one on the left.").classes("muted")
            return
        with ui.element("div").classes("rows-scroll w-full"):
            with ui.element("div").classes("trow requests-row thead"):
                for heading in ("Request", "Needs", "Stage", "Shard", "Time", "Assigned / details", ""):
                    ui.label(heading)
            with ui.column().classes("w-full gap-0 scroll-box"):
                if not any(not model.is_finished(r["status"]) for r in self.requests):
                    ui.label("No active requests right now.").classes("muted p-3")
                finished_started = False
                for request in self.requests:
                    if model.is_finished(request["status"]) and not finished_started:
                        finished_started = True
                        ui.label("Finished: fulfilled or cancelled").classes("divider-row")
                    self._request_row(request)

    def _request_row(self, request: dict) -> None:
        finished = model.is_finished(request["status"])
        classes = "trow requests-row" + (" flash" if request["id"] in self.flash else "") + (" finished" if finished else "")
        with ui.element("div").classes(classes):
            ui.label(model.short_id(request["id"])).classes("mono font-bold")
            ui.label(request["type"]).classes("font-bold")
            with ui.column().classes("gap-1"):
                chip(request["status"].replace("_", " "), model.color_of(request["status"], model.REQUEST_COLORS))
                self._stepper(request["status"])
            self._shard_chip("request", request["id"])
            ui.label(model.clock(request["created_at"])).classes("mono muted")
            assigned = request.get("assigned_resource_id")
            if assigned:
                ui.label(self._resource_names.get(assigned, model.short_id(assigned))).classes("truncate font-bold") \
                    .style(f"color:{model.BLUE}")
            else:
                ui.label(request.get("details") or "").classes("muted truncate")
            with ui.row().classes("gap-2 no-wrap"):
                for action in model.request_actions(request["status"]):
                    self._action_button(action, functools.partial(self.request_action, request, action))

    @staticmethod
    def _stepper(status: str) -> None:
        """Four small bars: PENDING, MATCHED, IN PROGRESS, FULFILLED. The current one is taller."""
        with ui.element("div").classes("stepper"):
            for step in model.lifecycle(status):
                bar = ui.element("div").classes(f"step {step['state']}")
                if step["state"] != "todo":
                    bar.style(f"background:{step['color']}")

    def _shard_chip(self, kind: str, record_id: str) -> None:
        """Which shard stores this record. Turns orange when that shard is running on one node only,
        red when it has no live node: so a failure shows on every record it affects."""
        shard = model.shard_of(kind, record_id)
        health = self.shard_health.get(shard, "ok")
        color = {"ok": model.shard_color(shard), "degraded": model.ORANGE, "down": model.RED}[health]
        label = ui.label(f"S{shard}" + ("" if health == "ok" else " !")).classes("chip-outline") \
            .style(f"border-color:{color}; color:{color}")
        label.tooltip({
            "ok": f"Stored on s{shard}-primary and s{shard}-replica",
            "degraded": f"Shard {shard} is running on one node: the other one is down and will catch up",
            "down": f"Shard {shard} has no live node: this record cannot be read or changed",
        }[health])

    # -- the responders table -------------------------------------------------------------------------------

    def _resources_table(self) -> None:
        if not self.resources:
            ui.label("No responders yet. Register one, or run scripts/seed.py.").classes("muted")
            return
        counts = {s: sum(r["status"] == s for r in self.resources) for s in ("ASSIGNED", "AVAILABLE", "OFFLINE")}
        with ui.row().classes("gap-2 mb-3 items-center"):
            for status, count in counts.items():
                chip(f"{count} {status}", model.color_of(status, model.RESOURCE_COLORS))
            if len(self.resources) > RESOURCES_SHOWN:
                ui.label(f"showing {RESOURCES_SHOWN} of {len(self.resources)}").classes("small muted")
        with ui.element("div").classes("rows-scroll w-full"):
            with ui.element("div").classes("trow resources-row thead"):
                for heading in ("Responder", "Offers", "Status", "Shard", "Serving request", ""):
                    ui.label(heading)
            with ui.column().classes("w-full gap-0 scroll-box"):
                for resource in self.resources[:RESOURCES_SHOWN]:
                    self._resource_row(resource)

    def _resource_row(self, resource: dict) -> None:
        classes = "trow resources-row" + (" flash" if resource["id"] in self.flash else "")
        if resource["status"] == "ASSIGNED":
            classes += " assigned"
        with ui.element("div").classes(classes):
            ui.label(resource["name"]).classes("truncate font-bold")
            ui.label(resource["type"]).classes("font-bold")
            chip(resource["status"], model.color_of(resource["status"], model.RESOURCE_COLORS))
            self._shard_chip("resource", resource["id"])
            self._serving_label(resource.get("assigned_request_id"))
            with ui.row().classes("gap-2 no-wrap"):
                for action in model.resource_actions(resource["status"]):
                    self._action_button(action, functools.partial(self.resource_action, resource, action))

    def _serving_label(self, request_id: str | None) -> None:
        if not request_id:
            ui.label("-").classes("muted")
            return
        request = (self._requests_by_id or {}).get(request_id)
        text = f"{model.short_id(request_id)}  {request['status'].replace('_', ' ')}" if request else model.short_id(request_id)
        color = model.color_of(request["status"], model.REQUEST_COLORS) if request else MUTED
        ui.label(text).classes("mono font-bold").style(f"color:{color}")

    @staticmethod
    def _action_button(action: model.Action, handler) -> None:
        """A real button: filled and tall. Cancel-type actions are outlined in red."""
        color = BUTTON_COLORS[action.tone]
        props = f"no-caps size=md color={color} " + ("outline" if action.tone == "negative" else "unelevated")
        if action.tone == "warning":
            props += " text-color=dark"
        ui.button(action.label, on_click=handler).props(props).classes("action-btn")

    # -- refreshing -----------------------------------------------------------------------------------

    def _set_link(self, ok: bool, note: str = "") -> None:
        self.link.set_text("LINK OK" if ok else f"LINK LOST: {note}")
        self.link.style(f"color:{model.GREEN if ok else model.RED}")

    def _push(self, events: list[model.Event]) -> None:
        if events:
            self.events = (list(reversed(events)) + self.events)[:FEED_LENGTH]
            self.feed.refresh()

    async def refresh_fast(self) -> None:
        requests, summary, cluster = await asyncio.gather(
            api.requests(REQUESTS_SHOWN), api.request_summary(), api.cluster(), return_exceptions=True)
        if isinstance(requests, Exception):
            self._set_link(False, str(requests))
        else:
            self._set_link(True)
            self._apply_requests(requests)
        if not isinstance(summary, Exception):
            self._apply_summary(summary)
        if not isinstance(cluster, Exception):
            self._apply_cluster(cluster)

    async def refresh_slow(self) -> None:
        try:
            resources = await api.resources()
        except Exception:  # the link indicator is driven by refresh_fast
            return
        order = {"ASSIGNED": 0, "AVAILABLE": 1, "OFFLINE": 2}
        resources.sort(key=lambda r: (order.get(r["status"], 3), r["name"]))
        new = {r["id"]: r for r in resources}
        events = model.diff_resources(self._resources_by_id, new, now())
        self._resources_by_id = new
        self._resource_names = {rid: r["name"] for rid, r in new.items()}
        if resources != self.resources:
            self.resources = resources
            self.flash |= {e.subject for e in events if e.subject}
            self.resources_table.refresh()
        self._push(events)

    def _apply_requests(self, requests: list[dict]) -> None:
        new = {r["id"]: r for r in requests}
        events = model.diff_requests(self._requests_by_id, new, self._resource_names, now())
        self._requests_by_id = new
        if requests != self.requests:
            self.requests = requests
            self.flash = {e.subject for e in events if e.subject}
            self.requests_table.refresh()
            self.resources_table.refresh()  # its "serving request" column shows the request's stage
        self._push(events)

    def _apply_summary(self, summary: dict[str, int]) -> None:
        if summary != self.summary:
            previous = self.summary or {}
            self.summary_flash = {s for s in summary if previous and summary[s] != previous.get(s)}
            self.summary = summary
            self.pipeline_panel.refresh()

    def _apply_cluster(self, cluster: dict) -> None:
        view = model.confirm_silent(model.cluster_view(cluster), self._silent_streaks)
        events = model.diff_cluster(self.view, view, now())
        if view != self.view:
            self.view = view
            self.cluster_panel.refresh()
            self.queue_panel.refresh()
            health = model.shard_health(view)
            if health != self.shard_health:  # the shard chips in both tables turn orange or red
                self.shard_health = health
                self.requests_table.refresh()
                self.resources_table.refresh()
        self._push(events)

    # -- actions --------------------------------------------------------------------------------------

    async def _act(self, call):
        """Run an API call; show the error if it fails. Returns the answer, or None."""
        try:
            return await call
        except ApiError as exc:
            ui.notify(str(exc), type="negative", position="top", multi_line=True)
            return None

    async def _after(self) -> None:
        await self.refresh_fast()
        await self.refresh_slow()

    async def submit_request(self) -> None:
        body = {"type": self.req_type.value, "location": {"lat": self.req_lat.value, "lon": self.req_lon.value}}
        if self.req_quantity.value:
            body["quantity"] = int(self.req_quantity.value)
        if self.req_details.value:
            body["details"] = self.req_details.value
        created = await self._act(api.create_request(body))
        if created:
            shard = model.shard_of("request", created["id"])
            ui.notify(f"Request {model.short_id(created['id'])} sent to Delta Command and stored on shard {shard}",
                      type="positive")
            self.req_details.value = ""
            await self._after()

    async def submit_resource(self) -> None:
        body = {"type": self.res_type.value, "name": self.res_name.value,
                "location": {"lat": self.res_lat.value, "lon": self.res_lon.value}}
        if self.res_capacity.value:
            body["capacity"] = int(self.res_capacity.value)
        created = await self._act(api.register_resource(body))
        if created:
            shard = model.shard_of("resource", created["id"])
            ui.notify(f"{created['name']} registered, AVAILABLE, stored on shard {shard}", type="positive")
            self.res_name.value = ""
            await self._after()

    async def send_burst(self) -> None:
        async def one():
            lat, lon = random.choice(list(model.CITIES.values()))
            return await api.create_request({
                "type": random.choice(model.TYPES), "details": "stress test",
                "location": {"lat": round(lat + random.uniform(-0.1, 0.1), 4),
                             "lon": round(lon + random.uniform(-0.1, 0.1), 4)},
            })

        self.burst_button.disable()
        try:
            results = await asyncio.gather(*(one() for _ in range(BURST_SIZE)), return_exceptions=True)
        finally:
            self.burst_button.enable()
        failed = sum(isinstance(r, Exception) for r in results)
        ui.notify(f"Sent {BURST_SIZE - failed} requests" + (f", {failed} failed" if failed else ""),
                  type="warning" if failed else "positive")
        await self.refresh_fast()

    async def request_action(self, request: dict, action: model.Action) -> None:
        if action.kind == "match":
            result = await self._act(api.match(request["id"]))
            if result:
                ui.notify(f"{model.short_id(request['id'])}: {result['outcome']}", type="info")
        else:
            result = await self._act(api.set_request_status(request["id"], action.target))
            if result:
                ui.notify(f"{model.short_id(request['id'])} is now {result['status']}", type="info")
        if result:
            await self._after()

    async def resource_action(self, resource: dict, action: model.Action) -> None:
        result = await self._act(api.set_resource_status(resource["id"], action.target))
        if result:
            ui.notify(f"{resource['name']} is now {result['status']}", type="info")
            await self._after()

    async def toggle_node(self, node_id: str, pause: bool) -> None:
        result = await self._act(api.set_node_paused(node_id, pause))
        if result:
            ui.notify(f"{node_id} " + ("paused: it stops answering now. The coordinator marks it DOWN within about "
                                       "10 s, or at once if a request reaches it." if pause
                                       else "resumed: it re-registers and catches up"),
                      type="warning" if pause else "positive")
            await self.refresh_fast()


@ui.page("/")
def index() -> None:
    Dashboard().build()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), title="S.P.D. Emergency: Delta Command",
           dark=True, reload=False, show=False)
