"""Tell the coordinator this node exists and is alive. DESIGN.md section 8.1.

On startup: POST /internal/nodes/register  {node_id, shard, role, url, position}
Then every HEARTBEAT_INTERVAL_S: POST /internal/nodes/heartbeat  {node_id}
Both answers may carry the coordinator's leadership epoch, which we adopt for
fencing (main.py).

This is push-based liveness: the node announces itself and the coordinator
only watches the clock. If no heartbeat arrives for HEARTBEAT_TIMEOUT_S, the
coordinator marks the node DOWN.

We register again when:
  * the coordinator answers 404 to a heartbeat (it restarted and forgot us), or
  * we were paused (a simulated failure). Registering again is how a returning
    node tells the coordinator "I'm back, catch me up" (recovery, Phase 5).
"""

import asyncio
import logging

import httpx

log = logging.getLogger("storage_node.heartbeat")


async def heartbeat_loop(state, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Runs forever as a background task. `state` is the node's NodeState.

    `transport` lets tests swap in a fake coordinator without any network.
    """
    s = state.settings
    reachable: bool | None = None  # only log when this changes, not every 3 s

    async with httpx.AsyncClient(base_url=s.coordinator_url, timeout=2.0, transport=transport) as http:
        while True:
            if not state.paused:
                try:
                    if state.needs_register:
                        resp = await http.post("/internal/nodes/register", json=state.registration())
                        resp.raise_for_status()
                        state.needs_register = False
                        log.info("registered with coordinator as %s", s.node_id)
                    else:
                        resp = await http.post("/internal/nodes/heartbeat", json={"node_id": s.node_id})
                        if resp.status_code == 404:
                            state.needs_register = True
                            continue  # register right away, don't wait a full interval
                        resp.raise_for_status()
                    try:
                        state.learn_epoch(resp.json())
                    except ValueError:
                        pass  # no JSON body: nothing to learn
                    state.last_heartbeat_ok = True
                    if reachable is False:
                        log.info("coordinator reachable again")
                    reachable = True
                except httpx.HTTPError as exc:
                    # The coordinator being down must never crash the node: keep
                    # serving data and keep retrying.
                    state.last_heartbeat_ok = False
                    if reachable is not False:
                        log.warning("coordinator unreachable (%r), will keep retrying", exc)
                    reachable = False
            await asyncio.sleep(s.heartbeat_interval)
