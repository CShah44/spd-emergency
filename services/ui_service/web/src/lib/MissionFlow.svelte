<script>
  // The hero: the five parts of the system in the order a request meets them, each with its live
  // number. When something happens, the parts it passed through light up one after another.
  import { ArrowUpRight } from '@lucide/svelte';
  import Callsign from './Callsign.svelte';
  import { SQUAD, SQUAD_ORDER } from './squad.js';
  import { live, go } from './live.svelte.js';

  const snap = $derived(live.snap);

  const cards = $derived.by(() => {
    const s = snap;
    const summary = s?.summary;
    const cluster = s?.cluster;
    const nodes = cluster?.shards.flatMap((sh) => sh.nodes) ?? [];
    const up = nodes.filter((n) => n.state === 'UP' && !n.silent).length;
    const shardsOk = cluster?.shards.filter((sh) => sh.available).length ?? 0;
    const degraded = Object.values(s?.shard_health ?? {}).filter((h) => h !== 'ok').length;
    const q = cluster?.queue;
    const paired = summary ? (summary.MATCHED ?? 0) + (summary.IN_PROGRESS ?? 0) + (summary.FULFILLED ?? 0) : null;
    const none = '—';
    return {
      request: {
        value: summary?.total ?? none, unit: 'requests on file',
        sub: s ? (s.link.ok ? 'Answering' : 'Not answering') : 'Connecting…',
        health: !s ? 'idle' : s.link.ok ? 'ok' : 'bad',
      },
      coordinator: {
        value: cluster ? `${shardsOk}/${cluster.shards.length}` : none, unit: 'shards serving',
        sub: !cluster ? 'Waiting…' : degraded ? `${degraded} shard${degraded > 1 ? 's' : ''} on one node` : 'Every shard has a leader and a copy',
        health: !cluster ? 'idle' : shardsOk < cluster.shards.length ? 'bad' : degraded ? 'warn' : 'ok',
      },
      storage: {
        value: cluster ? `${up}/${nodes.length}` : none, unit: 'nodes answering',
        sub: cluster?.total_keys != null ? `${cluster.total_keys.toLocaleString()} records · 2 copies each` : 'Key counts need the real coordinator',
        health: !cluster ? 'idle' : up === nodes.length ? 'ok' : up === 0 ? 'bad' : 'warn',
      },
      queue: {
        value: q ? q.waiting : none, unit: 'waiting to be matched',
        sub: q ? `${q.dead_lettered} dead-lettered` : cluster ? 'RabbitMQ unreachable: the sweeper catches up' : 'Waiting…',
        health: !cluster ? 'idle' : !q ? 'bad' : q.dead_lettered ? 'warn' : 'ok',
      },
      matching: {
        value: q ? q.consumers : none, unit: q?.consumers === 1 ? 'matcher connected' : 'matchers connected',
        sub: paired != null ? `${paired.toLocaleString()} requests paired` : 'Waiting…',
        health: !cluster ? 'idle' : !q || !q.consumers ? 'bad' : 'ok',
      },
    };
  });

  // light-up timing: each part of a pulse's path lights 220 ms after the one before
  let glow = $state({});
  let seen = new Set();
  $effect(() => {
    for (const p of live.pulses) {
      if (seen.has(p.id)) continue;
      seen.add(p.id);
      const delay = Math.max(0, p.at - Date.now());
      p.path.forEach((who, i) => setTimeout(() => (glow[who] = (glow[who] || 0) + 1), delay + i * 220));
    }
    if (seen.size > 200) seen = new Set(live.pulses.map((p) => p.id));
  });
</script>

<section class="flow" aria-label="The system, part by part">
  {#each SQUAD_ORDER as who, i (who)}
    {@const m = SQUAD[who]}
    {@const c = cards[who]}
    <button class="member {c.health}" style="--c:{m.color}" onclick={() => go('briefing', `squad-${who}`)}
      title={m.what}>
      {#key glow[who]}<span class="glow" class:on={glow[who]} aria-hidden="true"></span>{/key}
      <span class="top">
        <Callsign {who} size="lg" />
        <span class="call label">{m.call}</span>
        <span class="dot" aria-label={c.health}></span>
      </span>
      <span class="name">{m.name}</span>
      <span class="value num">{c.value}</span>
      <span class="unit">{c.unit}</span>
      <span class="sub">{c.sub}</span>
      <span class="more" aria-hidden="true">How it works <ArrowUpRight /></span>
    </button>
    {#if i < SQUAD_ORDER.length - 1}
      <span class="link" aria-hidden="true">
        {#key glow[SQUAD_ORDER[i + 1]]}<span class="spark" class:on={glow[SQUAD_ORDER[i + 1]]}></span>{/key}
      </span>
    {/if}
  {/each}
</section>

<style>
  .flow {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr) 20px) minmax(0, 1fr);
    align-items: stretch;
  }

  .member {
    position: relative; overflow: hidden;
    display: grid; grid-template-rows: auto auto 1fr auto auto auto; gap: 2px;
    padding: 14px 16px 14px;
    border: 1px solid var(--seam-2); border-radius: var(--r-3);
    background: var(--hull-1);
    text-align: left; cursor: pointer;
    transition: border-color 160ms var(--ease-out), background-color 160ms var(--ease-out);
  }
  /* the member's color, as a thin plate along the top */
  .member::before {
    content: ''; position: absolute; inset: 0 0 auto; height: 2px; background: var(--c); opacity: 0.85;
  }
  .member:hover { border-color: var(--seam-3); background: var(--hull-2); }
  .member:hover .more { opacity: 1; transform: none; }

  .top { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .call { flex: 1; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ink-4); }
  .ok .dot { background: var(--green); box-shadow: 0 0 0 3px rgba(25, 210, 122, 0.15); }
  .warn .dot { background: var(--orange); box-shadow: 0 0 0 3px rgba(255, 138, 43, 0.18); }
  .bad .dot { background: var(--red); box-shadow: 0 0 0 3px rgba(255, 51, 71, 0.2); animation: blink 1.2s steps(2) infinite; }
  .bad { border-color: rgba(255, 51, 71, 0.45); }
  .warn { border-color: rgba(255, 138, 43, 0.35); }
  @keyframes blink { 50% { opacity: 0.3; } }

  .name { font-family: var(--f-display); font-size: 15px; font-weight: 600; color: var(--ink); }
  .value {
    margin-top: 10px;
    font-family: var(--f-display); font-size: var(--t-hero); font-weight: 600; line-height: 1;
    letter-spacing: -0.02em; color: var(--ink);
  }
  .unit { font-size: 12px; color: var(--ink-2); }
  .sub { margin-top: 8px; font-size: 12px; color: var(--ink-3); line-height: 1.35; min-height: 2.7em; }
  .more {
    position: absolute; right: 14px; bottom: 12px;
    display: inline-flex; align-items: center; gap: 2px;
    font-family: var(--f-display); font-size: 11px; font-weight: 600; letter-spacing: 0.06em; color: var(--c);
    opacity: 0; transform: translateY(2px); transition: opacity 160ms var(--ease-out), transform 160ms var(--ease-out);
  }
  .more :global(svg) { width: 13px; height: 13px; }

  /* the light-up: a ring and a wash in the member's color */
  .glow { position: absolute; inset: 0; border-radius: inherit; pointer-events: none; opacity: 0; }
  .glow.on { animation: glow 900ms var(--ease-out); }
  @keyframes glow {
    0% { opacity: 1; box-shadow: inset 0 0 0 1px var(--c); background: color-mix(in srgb, var(--c) 12%, transparent); }
    100% { opacity: 0; }
  }

  /* the track between two members, and the spark that runs along it */
  .link { position: relative; align-self: center; height: 2px; margin: 0 2px; background: var(--seam-2); overflow: hidden; }
  .spark { position: absolute; top: 0; left: -40%; width: 40%; height: 2px; opacity: 0; }
  .spark.on { animation: spark 260ms var(--ease-move) both;
    background: linear-gradient(90deg, transparent, var(--ink)); }
  @keyframes spark { from { opacity: 1; transform: translateX(0); } to { opacity: 1; transform: translateX(350%); } }

  @media (max-width: 1180px) {
    .flow { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
    .link, .call, .more { display: none; }
    .value { font-size: var(--t-h1); }
    .member { padding: 12px; }
  }
  /* narrow: one row that scrolls sideways, in flow order, rather than a ragged wrap */
  @media (max-width: 760px) {
    .flow { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; margin: 0 -16px; padding: 0 16px 4px;
      scroll-padding: 0 16px; }
    .member { flex: 0 0 168px; scroll-snap-align: start; }
    .call { display: inline; }
  }
</style>
