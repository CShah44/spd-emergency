// What every control on the page does, step by step through the system.
// `panel` is the id of the panel the control lives on (the briefing can jump to it);
// each step names the squad member doing it; `grafana` names the dashboard panels to watch.

export const OPERATIONS = [
  {
    id: 'report', title: 'Report an emergency', panel: 'dispatch', call: 'POST /requests',
    summary: 'Someone needs water, food, medicine, transport or shelter at a place.',
    steps: [
      ['request', 'Checks the body and gives the request an id that carries its geohash cell, e.g. req-tdr-…'],
      ['coordinator', 'Hashes the key to pick its shard, and finds that shard’s leader.'],
      ['storage', 'Writes it to the primary and the replica. It is now PENDING.'],
      ['queue', 'The request service publishes the id. It waits here for a matcher.'],
      ['matching', 'A matcher takes it, searches nearby cells for a free responder of the same type, and assigns both: MATCHED. If nobody is in range: UNMATCHED.'],
    ],
    watch: 'The mission flow lights up left to right; a new row appears on the requests board and turns pink within a second.',
    grafana: ['Requests created per second', 'Match results per second', 'Time from request created to answered'],
  },
  {
    id: 'register', title: 'Register a responder', panel: 'dispatch', call: 'POST /resources',
    summary: 'A volunteer, NGO or truck offers one type of help at a place.',
    steps: [
      ['request', 'Checks it and gives it an id tagged with its geohash cell.'],
      ['coordinator', 'Routes it to the shard that owns that key.'],
      ['storage', 'Stored on two nodes as AVAILABLE. The matcher can use it for the next request.'],
    ],
    watch: 'It appears on the responders list. Requests that were UNMATCHED stay so until you press Retry.',
    grafana: ['Records per storage node'],
  },
  {
    id: 'burst', title: 'Stress test', panel: 'dispatch', call: '20 × POST /requests',
    summary: 'Twenty random requests across five cities, sent at the same moment.',
    steps: [
      ['request', 'All twenty are accepted in parallel and stored.'],
      ['queue', 'The queue fills faster than one matcher drains it.'],
      ['matching', 'On Kubernetes, KEDA sees the queue depth and adds matcher pods; they drain it together.'],
    ],
    watch: '“Waiting” on the queue card climbs and falls back to zero; the stage bar fills with pink.',
    grafana: ['Matching pods and the queue', 'Requests created per second'],
  },
  {
    id: 'match', title: 'Match now', panel: 'requests', call: 'POST /match/{id}',
    summary: 'Ask the matcher to try one PENDING request straight away, without waiting for the queue.',
    steps: [
      ['request', 'Passes the call on to the matching service.'],
      ['matching', 'Runs the same search as for a queued request and answers matched, unmatched or skipped (already handled).'],
      ['storage', 'On a match, the request and the responder are updated together, with a version check so two matchers never take the same responder.'],
    ],
    watch: 'The toast says the outcome; the row changes color.',
    grafana: ['Match results per second'],
  },
  {
    id: 'lifecycle', title: 'Start, Fulfil, Cancel, Retry', panel: 'requests', call: 'PUT /requests/{id}/status',
    summary: 'Move a request along its life. The page only offers the moves the API allows.',
    steps: [
      ['request', 'Checks the move is allowed: MATCHED → IN PROGRESS → FULFILLED, or CANCELLED from any open stage.'],
      ['storage', 'Writes the new status with a version check.'],
      ['request', 'When a request ends (FULFILLED or CANCELLED), its responder goes back to AVAILABLE.'],
      ['queue', 'Retry turns UNMATCHED back into PENDING and queues it again.'],
    ],
    watch: 'The four-step bar on the row advances; the freed responder turns green.',
    grafana: [],
  },
  {
    id: 'responder', title: 'Take a responder offline', panel: 'responders', call: 'PUT /resources/{id}/status',
    summary: 'A responder that cannot help for now. ASSIGNED ones are freed only when their request ends.',
    steps: [
      ['request', 'AVAILABLE ↔ OFFLINE. An OFFLINE responder is never picked by the matcher.'],
      ['storage', 'Stored like any other change.'],
    ],
    watch: 'The responder moves down the list and greys out.',
    grafana: [],
  },
  {
    id: 'pause', title: 'Pause or resume a storage node', panel: 'storage', call: 'POST /cluster/nodes/{id}/pause',
    summary: 'Simulate a crash: the node stops answering but keeps its data.',
    steps: [
      ['storage', 'The node stops answering every call, heartbeats included.'],
      ['coordinator', 'After 10 s without a heartbeat (or at once, if a request reaches it) it marks the node DOWN and its partner leads the shard alone.'],
      ['request', 'Reads and writes keep working: they go to the surviving node. Records on that shard show an amber “!”.'],
      ['storage', 'On Resume the node is RECOVERING: it copies what it missed from its partner’s log, then turns UP.'],
    ],
    watch: 'The node card turns red, the leader crown moves to its partner; on resume it goes yellow, shows how much it copied, then green.',
    grafana: ['Storage nodes', 'Records per storage node', 'Shards available'],
  },
];

export const PLAYBOOKS = [
  {
    id: 'failover', title: 'Failover drill', minutes: 2, color: 'var(--green)',
    goal: 'Show that losing a storage node loses no data and stops no one.',
    steps: [
      ['storage', 'Pause s1-primary in the storage vault.'],
      ['storage', 'Watch it go quiet, then DOWN. The crown moves to s1-replica.'],
      ['dispatch', 'Report an emergency. It is still accepted, even if it lands on shard 1.'],
      ['storage', 'Resume s1-primary. It catches up and shows how many records it copied.'],
      ['telemetry', 'Open Telemetry: the “Storage nodes” panel shows the dip and the recovery.'],
    ],
  },
  {
    id: 'surge', title: 'Surge', minutes: 1, color: 'var(--yellow)',
    goal: 'Show the queue absorbing a burst and the matchers draining it.',
    steps: [
      ['dispatch', 'Press Send 20 in the stress test.'],
      ['flow', 'Watch “waiting” on the queue card climb, then drain.'],
      ['requests', 'The stage bar fills with pink as matches land.'],
      ['telemetry', 'On Kubernetes, “Matching pods and the queue” shows KEDA adding pods.'],
    ],
  },
  {
    id: 'life', title: 'One request, end to end', minutes: 1, color: 'var(--red)',
    goal: 'Walk one request through every stage.',
    steps: [
      ['dispatch', 'Report an emergency in a city that has responders.'],
      ['requests', 'It arrives PENDING and is MATCHED almost at once.'],
      ['requests', 'Press Start, then Fulfil.'],
      ['responders', 'Its responder goes back to AVAILABLE.'],
    ],
  },
];
