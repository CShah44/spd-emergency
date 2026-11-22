// The five parts of the system, as the five members of an S.P.D. squad.
// Each keeps its number and color everywhere on the page: in the mission flow, on the panel
// it owns, in the feed, and in every step of the briefing.

export const SQUAD = {
  request: {
    n: 1, color: 'var(--red)', name: 'Request service', call: 'Front door',
    what: 'Takes every report and every responder over REST, checks it, gives it an id tagged with its geohash cell, stores it through the coordinator, and puts new requests on the queue.',
  },
  coordinator: {
    n: 2, color: 'var(--blue)', name: 'Coordinator', call: 'Router',
    what: 'Knows which shard owns each record (sha1 of the key, mod 3) and which node leads that shard. Watches heartbeats: a node silent for 10 s is marked DOWN and its partner takes over. Runs as a leader-elected pair.',
  },
  storage: {
    n: 3, color: 'var(--green)', name: 'Storage nodes', call: 'Vault',
    what: 'Three shards, each a primary and a replica. Every write lands on both. A node that comes back catches up from its partner’s log before it serves again.',
  },
  queue: {
    n: 4, color: 'var(--yellow)', name: 'RabbitMQ queue', call: 'Dispatch line',
    what: 'Holds new requests until a matcher takes one. A message that keeps failing goes to the dead-letter queue; a sweeper re-queues anything that was lost.',
  },
  matching: {
    n: 5, color: 'var(--pink)', name: 'Matching service', call: 'Pairing',
    what: 'Takes a request off the queue and looks for the nearest AVAILABLE responder of the same type, in the request’s geohash cell and its neighbours (up to 50 km). Assigns both in storage. KEDA adds matchers as the queue grows.',
  },
};

export const SQUAD_ORDER = ['request', 'coordinator', 'storage', 'queue', 'matching'];

// Feed event kinds -> the squad member that produced them.
export const KIND_OWNER = {
  request: 'request', responder: 'request', matching: 'matching', storage: 'storage', coordinator: 'coordinator',
};

// Which parts light up, in order, when something of this kind happens.
export const KIND_PATH = {
  request: ['request', 'coordinator', 'storage', 'queue'],
  responder: ['request', 'coordinator', 'storage'],
  matching: ['queue', 'matching', 'coordinator', 'storage'],
  storage: ['coordinator', 'storage'],
  coordinator: ['coordinator', 'storage'],
};

export const STAGES = ['PENDING', 'MATCHED', 'IN_PROGRESS', 'FULFILLED'];
export const SIDE = ['UNMATCHED', 'CANCELLED'];
export const STAGE_NOTE = {
  PENDING: 'stored, waiting on the queue',
  MATCHED: 'paired with a responder',
  IN_PROGRESS: 'responder on the way',
  FULFILLED: 'done; responder freed',
  UNMATCHED: 'no responder in range',
  CANCELLED: 'called off',
};

export const pretty = (s) => (s || '').replaceAll('_', ' ');
