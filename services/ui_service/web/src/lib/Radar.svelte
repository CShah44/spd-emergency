<script>
  // The patrol grid: requests and responders on the geohash cells the matcher searches.
  // A line joins each matched request to its responder.
  import Panel from './Panel.svelte';
  import { live, config, isFlashing } from './live.svelte.js';
  import { pretty } from './squad.js';

  const CELL = 1.40625; // a precision-3 geohash cell, in degrees, on both axes
  const W = 520;

  const reqs = $derived((live.snap?.requests ?? []).filter((r) => r.lat != null));
  const ress = $derived((live.snap?.resources ?? []).filter((r) => r.lat != null));

  const frame = $derived.by(() => {
    const pts = [...Object.values(config.cities), ...reqs.map((r) => [r.lat, r.lon]), ...ress.map((r) => [r.lat, r.lon])];
    if (!pts.length) pts.push([12.97, 77.59], [22.57, 88.36]);
    let la0 = Math.min(...pts.map((p) => p[0])) - 1.6, la1 = Math.max(...pts.map((p) => p[0])) + 1.6;
    let lo0 = Math.min(...pts.map((p) => p[1])) - 1.6, lo1 = Math.max(...pts.map((p) => p[1])) + 1.6;
    const k = Math.cos(((la0 + la1) / 2) * Math.PI / 180); // keep a degree of longitude its true width
    const scale = W / ((lo1 - lo0) * k);
    const H = Math.min(560, Math.max(260, (la1 - la0) * scale));
    return { la0, la1, lo0, lo1, k, scale, H };
  });

  const x = (lon) => (lon - frame.lo0) * frame.k * frame.scale;
  const y = (lat) => frame.H - (lat - frame.la0) * frame.scale;

  const gridX = $derived.by(() => {
    const out = [];
    for (let v = Math.ceil((frame.lo0 + 180) / CELL) * CELL - 180; v < frame.lo1; v += CELL) out.push(v);
    return out;
  });
  const gridY = $derived.by(() => {
    const out = [];
    for (let v = Math.ceil((frame.la0 + 90) / CELL) * CELL - 90; v < frame.la1; v += CELL) out.push(v);
    return out;
  });

  // cells with an open request: the ones the matcher is searching around right now
  const hot = $derived.by(() => {
    const seen = new Map();
    for (const r of reqs) {
      if (r.finished) continue;
      const lo = Math.floor((r.lon + 180) / CELL) * CELL - 180;
      const la = Math.floor((r.lat + 90) / CELL) * CELL - 90;
      seen.set(`${lo},${la}`, { lo, la, cell: r.cell });
    }
    return [...seen.values()];
  });

  const byId = $derived(Object.fromEntries(ress.map((r) => [r.id, r])));
  const pairs = $derived(reqs.filter((r) => r.assigned && byId[r.assigned.id] && !r.finished)
    .map((r) => ({ r, s: byId[r.assigned.id] })));
  const open = $derived(reqs.filter((r) => !r.finished).length);
</script>

<Panel id="radar" owner="matching" title="Patrol grid" topic="squad-matching"
  sub="Geohash cells, about 156 km a side · {open} open request{open === 1 ? '' : 's'}">
  {#snippet info()}
    <p>Every id carries its <b>geohash cell</b> (req-<b>tdr</b>-…). The matcher only looks in a request’s cell
      and the eight around it, so it asks the few shards that hold those cells instead of all of them.</p>
    <p>Lit cells hold open requests. Dots are requests, squares are responders; a pink line is a match.</p>
  {/snippet}

  <div class="wrap">
    <svg viewBox="0 0 {W} {frame.H}" role="img" aria-label="Requests and responders on the geohash grid">
      {#each hot as c (c.lo + ',' + c.la)}
        <rect class="hot" x={x(c.lo)} y={y(c.la + CELL)} width={CELL * frame.k * frame.scale} height={CELL * frame.scale} />
      {/each}
      {#each gridX as v (v)}<line class="grid" x1={x(v)} x2={x(v)} y1="0" y2={frame.H} />{/each}
      {#each gridY as v (v)}<line class="grid" y1={y(v)} y2={y(v)} x1="0" x2={W} />{/each}

      {#each Object.entries(config.cities) as [name, [la, lo]] (name)}
        <g class="city">
          <circle cx={x(lo)} cy={y(la)} r="26" />
          <text x={x(lo)} y={y(la) - 32}>{name}</text>
        </g>
      {/each}

      {#each pairs as { r, s } (r.id)}
        <line class="pair" x1={x(r.lon)} y1={y(r.lat)} x2={x(s.lon)} y2={y(s.lat)} />
      {/each}

      {#each ress as s (s.id)}
        <rect class="res s-{s.status}" x={x(s.lon) - 3} y={y(s.lat) - 3} width="6" height="6">
          <title>{s.name} · {s.type} · {s.status}</title>
        </rect>
      {/each}
      {#each reqs as r (r.id)}
        <g class="req s-{r.status}" class:finished={r.finished}>
          {#if isFlashing(r.id)}<circle class="ping" cx={x(r.lon)} cy={y(r.lat)} r="4" />{/if}
          <circle cx={x(r.lon)} cy={y(r.lat)} r={r.finished ? 2.5 : 4}>
            <title>{r.short} · {r.type} · {pretty(r.status)}</title>
          </circle>
        </g>
      {/each}
    </svg>
  </div>
  <div class="legend">
    <span><i class="dot"></i>request</span>
    <span><i class="sq"></i>responder</span>
    <span><i class="ln"></i>matched pair</span>
    <span><i class="cellk"></i>cell with open requests</span>
  </div>
</Panel>

<style>
  .wrap { border-radius: var(--r-2); background: var(--well); border: 1px solid var(--seam); overflow: hidden; }
  svg { display: block; width: 100%; height: auto; }
  .grid { stroke: rgba(170, 190, 225, 0.06); stroke-width: 1; }
  .hot { fill: rgba(255, 198, 26, 0.07); stroke: rgba(255, 198, 26, 0.35); stroke-width: 1; }
  .city circle { fill: none; stroke: rgba(170, 190, 225, 0.12); stroke-dasharray: 2 3; }
  .city text { fill: var(--ink-3); font-family: var(--f-display); font-size: 10px; font-weight: 600;
    letter-spacing: 0.12em; text-transform: uppercase; text-anchor: middle; }
  .pair { stroke: var(--pink); stroke-width: 1.25; opacity: 0.7; }
  .res { fill: var(--tone); opacity: 0.85; }
  .res.s-OFFLINE { opacity: 0.35; }
  .req circle { fill: var(--tone); stroke: var(--well); stroke-width: 1.5; }
  .req.finished circle { opacity: 0.35; }
  .req .ping { fill: none; stroke: var(--tone); stroke-width: 1.5; transform-box: fill-box; transform-origin: center;
    animation: ping 1.2s var(--ease-out) forwards; }
  @keyframes ping { from { transform: scale(1); opacity: 1; } to { transform: scale(5); opacity: 0; } }

  .legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; font-size: 11.5px; color: var(--ink-3); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .legend i { display: inline-block; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--yellow); }
  .sq { width: 7px; height: 7px; background: var(--green); }
  .ln { width: 14px; height: 2px; background: var(--pink); }
  .cellk { width: 10px; height: 10px; border: 1px solid rgba(255, 198, 26, 0.5); background: rgba(255, 198, 26, 0.08); }
</style>
