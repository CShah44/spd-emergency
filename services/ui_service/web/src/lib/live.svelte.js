// The page's one source of truth: the snapshot the server streams (GET /api/stream), the
// page's own UI state (which view, which panel the briefing points at), toasts, and actions.

import { KIND_PATH } from './squad.js';

export const live = $state({
  snap: null, // the latest snapshot, or null before the first one
  connected: false,
  lastAt: 0, // Date.now() of the last snapshot
  flash: {}, // record id -> time it last changed, for the row flash
  pulses: [], // {id, path, at}: the mission flow lights these up
});

export const config = $state({
  grafana_url: 'http://localhost:3000',
  grafana_dashboard: 'spd-overview',
  prometheus_url: 'http://localhost:9090',
  rabbitmq_url: 'http://localhost:15672',
  api_docs_url: 'http://localhost:8080/docs',
  burst_size: 20,
  types: ['water', 'food', 'medicine', 'transport', 'shelter'],
  cities: {},
});

export const ui = $state({
  view: 'command', // command | telemetry | briefing
  topic: null, // briefing section to scroll to
  spotlight: null, // panel id to ring
  spotlightAt: 0,
  toasts: [],
});

let lastEventId = 0;
let source = null;
let pollTimer = null;

export function connect() {
  fetch('/api/config').then((r) => r.ok && r.json()).then((c) => c && Object.assign(config, c)).catch(() => {});
  open();
}

function open() {
  source?.close();
  source = new EventSource('/api/stream');
  source.onopen = () => { live.connected = true; stopPolling(); };
  source.onmessage = (e) => take(JSON.parse(e.data));
  source.onerror = () => {
    live.connected = false;
    // EventSource retries on its own; poll meanwhile so the page never freezes.
    startPolling();
  };
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/state');
      if (r.ok) take(await r.json());
    } catch { /* the server is away: the link pill says so */ }
  }, 2000);
}

function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

function take(snap) {
  const now = Date.now();
  const fresh = (snap.events || []).filter((e) => e.id > lastEventId);
  if (live.snap && fresh.length) {
    for (const e of fresh) if (e.subject) live.flash[e.subject] = now;
    // one pulse per kind per snapshot is enough to show where things moved
    const kinds = [...new Set(fresh.map((e) => e.kind))];
    live.pulses = [...live.pulses.filter((p) => now - p.at < 2500),
      ...kinds.map((k, i) => ({ id: `${now}-${i}`, path: KIND_PATH[k] || [], at: now + i * 180 }))];
  }
  if (snap.events?.length) lastEventId = Math.max(lastEventId, snap.events[0].id);
  live.snap = snap;
  live.lastAt = now;
}

export function isFlashing(id) {
  const at = live.flash[id];
  return at && Date.now() - at < 1600;
}

// -- toasts ---------------------------------------------------------------------------------------

let toastId = 0;
export function toast(text, tone = 'ok', ms = 5000) {
  const id = ++toastId;
  ui.toasts = [...ui.toasts, { id, text, tone }];
  setTimeout(() => dismiss(id), ms);
}
export function dismiss(id) { ui.toasts = ui.toasts.filter((t) => t.id !== id); }

// -- actions --------------------------------------------------------------------------------------

/** Call the server; toast its message or its error. Returns the answer, or null. */
export async function act(method, path, body) {
  try {
    const r = await fetch(path, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast(data.detail || `${r.status} ${r.statusText}`, 'bad', 8000);
      return null;
    }
    if (data.message) toast(data.message, 'ok');
    return data;
  } catch {
    toast('Cannot reach the Delta Command server', 'bad', 8000);
    return null;
  }
}

export function runAction(kindOf, record, action) {
  if (action.kind === 'match') return act('POST', `/api/requests/${record.id}/match`);
  const base = kindOf === 'request' ? 'requests' : 'resources';
  return act('PUT', `/api/${base}/${record.id}/status`, { status: action.target });
}

// -- navigation -----------------------------------------------------------------------------------

export function go(view, topic = null) {
  ui.view = view;
  ui.topic = topic;
  const hash = topic ? `#/${view}/${topic}` : `#/${view}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

/** Jump to the command view and ring a panel. */
export function point(panel) {
  if (panel === 'telemetry') return go('telemetry');
  go('command');
  ui.spotlight = panel;
  ui.spotlightAt = Date.now();
  requestAnimationFrame(() =>
    document.getElementById(`panel-${panel}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
}

export function readHash() {
  const [, view, topic] = location.hash.split('/');
  if (['command', 'telemetry', 'briefing'].includes(view)) { ui.view = view; ui.topic = topic || null; }
}

export function grafanaLink(kiosk = false, range = '15m') {
  const base = `${config.grafana_url}/d/${config.grafana_dashboard}/`;
  const q = `?orgId=1&from=now-${range}&to=now&refresh=5s&theme=dark`;
  return base + q + (kiosk ? '&kiosk' : '');
}
