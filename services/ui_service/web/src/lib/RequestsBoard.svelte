<script>
  // Every request: a stage bar that doubles as a filter, then the list (active ones first).
  import { Search, Droplet, Wheat, Pill, Truck, Tent, X } from '@lucide/svelte';
  import Panel from './Panel.svelte';
  import ShardChip from './ShardChip.svelte';
  import { live, runAction, isFlashing } from './live.svelte.js';
  import { STAGES, SIDE, STAGE_NOTE, pretty } from './squad.js';

  const ICONS = { water: Droplet, food: Wheat, medicine: Pill, transport: Truck, shelter: Tent };
  const STATUSES = [...STAGES, ...SIDE];

  let filter = $state(null);
  let query = $state('');
  let busy = $state({});

  const summary = $derived(live.snap?.summary);
  const health = $derived(live.snap?.shard_health ?? {});
  const all = $derived(live.snap?.requests ?? []);
  const rows = $derived.by(() => {
    const q = query.trim().toLowerCase();
    return all.filter((r) => (!filter || r.status === filter) && (!q ||
      [r.id, r.type, r.details, r.assigned?.name, r.cell].some((v) => v && v.toLowerCase().includes(q))));
  });
  const active = $derived(rows.filter((r) => !r.finished));
  const finished = $derived(rows.filter((r) => r.finished));
  const total = $derived(summary?.total ?? 0);

  async function run(r, a) {
    busy[r.id] = true;
    await runAction('request', r, a);
    busy[r.id] = false;
  }

  const TONE = { primary: 'var(--blue)', positive: 'var(--green)', warning: 'var(--yellow)', negative: 'var(--red)' };
</script>

<Panel id="requests" owner="request" title="Requests board" topic="lifecycle"
  sub={summary ? `${total.toLocaleString()} in all · showing the latest ${all.length}` : 'Waiting for requests…'}>
  {#snippet info()}
    <p>The bar shows every request by stage. A request’s color says whose hands it is in:
      <b style="color:var(--yellow)">yellow</b> on the queue, <b style="color:var(--pink)">pink</b> paired by the matcher,
      <b style="color:var(--cyan)">cyan</b> responder on the way, <b style="color:var(--green)">green</b> done.</p>
    <p>Click a stage to filter. Each row offers only the moves the API allows: <b>Match now</b>, <b>Start</b>,
      <b>Fulfil</b>, <b>Retry</b>, <b>Cancel</b>.</p>
  {/snippet}

  <!-- the stage bar: proportional, then the counters that filter the list -->
  <div class="bar" aria-hidden="true">
    {#each STATUSES as s (s)}
      {@const n = summary?.[s] ?? 0}
      {#if n}<span class="seg s-{s}" style="flex:{n}"></span>{/if}
    {/each}
    {#if !total}<span class="seg none"></span>{/if}
  </div>
  <div class="stages" role="group" aria-label="Filter by stage">
    {#each STATUSES as s, i (s)}
      {#if i === STAGES.length}<span class="sep" aria-hidden="true"></span>{/if}
      <button class="stage s-{s}" class:on={filter === s} class:side={SIDE.includes(s)} aria-pressed={filter === s}
        onclick={() => (filter = filter === s ? null : s)} title={STAGE_NOTE[s]}>
        <span class="count num">{(summary?.[s] ?? 0).toLocaleString()}</span>
        <span class="name">{pretty(s)}</span>
        <span class="note">{STAGE_NOTE[s]}</span>
      </button>
    {/each}
  </div>

  <div class="tools">
    <label class="search">
      <Search />
      <span class="sr-only">Search requests</span>
      <input placeholder="Search id, type, cell, responder…" bind:value={query} />
    </label>
    {#if filter || query}
      <button class="btn ghost sm" onclick={() => { filter = null; query = ''; }}><X />Clear</button>
    {/if}
  </div>

  <div class="table" role="table" aria-label="Requests">
    <div class="tr th" role="row">
      <span role="columnheader">Request</span><span role="columnheader">Needs</span>
      <span role="columnheader">Stage</span><span role="columnheader">Shard</span>
      <span role="columnheader">Filed</span><span role="columnheader">Responder / details</span>
      <span role="columnheader" class="sr-only">Actions</span>
    </div>
    <div class="scroll">
      {#if !all.length}
        <p class="empty">No requests yet. File one from the dispatch console.</p>
      {:else if !rows.length}
        <p class="empty">Nothing matches.</p>
      {/if}
      {#snippet row(r)}
        {@const Icon = ICONS[r.type]}
        <div class="tr s-{r.status}" class:flash={isFlashing(r.id)} class:done={r.finished} role="row">
          <span class="id" role="cell" title={r.id}>
            {#if r.cell}<span class="cell mono" title="geohash cell">{r.cell}</span><span class="mono">{r.id.split('-')[2].slice(0, 8)}</span>
            {:else}<span class="mono">{r.short}</span>{/if}
          </span>
          <span class="type" role="cell">{#if Icon}<Icon />{/if}{r.type}{#if r.quantity}<small class="num">×{r.quantity}</small>{/if}</span>
          <span class="stage-cell" role="cell">
            <span class="pill s-{r.status}">{pretty(r.status)}</span>
            <span class="steps" aria-hidden="true">
              {#each r.lifecycle as st, i (i)}<i class="step {st} s-{STAGES[i]}"></i>{/each}
            </span>
          </span>
          <span role="cell"><ShardChip shard={r.shard} health={health[r.shard] ?? 'ok'} /></span>
          <span class="mono muted" role="cell">{r.clock}</span>
          <span class="who" role="cell">
            {#if r.assigned}<span class="assigned">{r.assigned.name}</span>
            {:else}<span class="muted">{r.details || '—'}</span>{/if}
          </span>
          <span class="acts" role="cell">
            {#each r.actions as a (a.label)}
              <button class="btn sm tone" style="--tone:{TONE[a.tone]}" disabled={busy[r.id]} onclick={() => run(r, a)}>{a.label}</button>
            {/each}
          </span>
        </div>
      {/snippet}
      {#each active as r (r.id)}{@render row(r)}{/each}
      {#if finished.length}
        <div class="divider"><span class="label">Finished · fulfilled or cancelled</span></div>
        {#each finished as r (r.id)}{@render row(r)}{/each}
      {/if}
    </div>
  </div>
</Panel>

<style>
  .bar { display: flex; gap: 2px; height: 6px; border-radius: 3px; overflow: hidden; margin-bottom: 12px; }
  .seg { background: var(--tone); transition: flex-grow 600ms var(--ease-move); min-width: 3px; }
  .seg.none { flex: 1; background: var(--hull-3); }

  .stages { display: grid; grid-template-columns: repeat(4, 1fr) 1px repeat(2, 1fr); gap: 6px; }
  .sep { background: var(--seam-2); margin: 6px 2px; }
  .stage {
    display: grid; gap: 1px; text-align: left; padding: 10px 12px;
    border: 1px solid var(--seam); border-radius: var(--r-2); background: var(--hull-2);
    cursor: pointer; transition: border-color 140ms, background-color 140ms;
  }
  .stage:hover { border-color: var(--seam-3); }
  .stage.on { border-color: var(--tone); background: color-mix(in srgb, var(--tone) 10%, var(--hull-2)); }
  .count { font-family: var(--f-display); font-size: var(--t-h1); font-weight: 600; line-height: 1.1; color: var(--tone); letter-spacing: -0.02em; }
  .side .count { font-size: var(--t-h2); }
  .name { font-family: var(--f-display); font-size: 11px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink); }
  .note { font-size: 11px; color: var(--ink-3); line-height: 1.3; }

  .tools { display: flex; align-items: center; gap: 8px; margin: 16px 0 8px; }
  .search {
    flex: 1; display: flex; align-items: center; gap: 8px; height: 36px; padding: 0 12px;
    border: 1px solid var(--seam-2); border-radius: var(--r-1); background: var(--well); color: var(--ink-3);
  }
  .search:focus-within { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(61, 132, 255, 0.18); }
  .search :global(svg) { width: 15px; height: 15px; }
  .search input { flex: 1; min-width: 0; border: 0; background: none; outline: none; color: var(--ink); }
  .search input::placeholder { color: var(--ink-4); }

  .table { margin: 0 -16px -16px; border-top: 1px solid var(--seam); }
  .scroll { max-height: 560px; overflow-y: auto; }
  .tr {
    display: grid; align-items: center; gap: 12px;
    grid-template-columns: 124px 116px minmax(196px, 1fr) 44px 66px minmax(120px, 1.2fr) 150px;
    min-height: 52px; padding: 8px 16px;
    border-bottom: 1px solid var(--seam);
    transition: background-color 140ms;
  }
  .tr:not(.th):hover { background: rgba(170, 190, 225, 0.03); }
  .th { min-height: 0; padding-top: 10px; padding-bottom: 10px; position: sticky; top: 0; z-index: 1; background: var(--hull-1);
    font-family: var(--f-display); font-size: var(--t-cap); font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-3); }
  .done { opacity: 0.55; }
  .done:hover { opacity: 0.85; }

  .id { display: flex; align-items: center; gap: 6px; color: var(--ink); font-weight: 600; min-width: 0; }
  .cell { font-size: 10.5px; color: var(--ink-2); padding: 1px 5px; border-radius: 4px; background: var(--hull-3); }
  .type { display: inline-flex; align-items: center; gap: 6px; text-transform: capitalize; color: var(--ink); font-weight: 500; }
  .type :global(svg) { width: 15px; height: 15px; color: var(--ink-3); }
  .type small { color: var(--ink-3); font-size: 11px; }
  .stage-cell { display: flex; align-items: center; gap: 10px; }
  .steps { display: inline-flex; gap: 3px; }
  .step { width: 14px; height: 4px; border-radius: 2px; background: var(--hull-3); }
  .step.done { background: color-mix(in srgb, var(--tone) 55%, transparent); }
  .step.current { background: var(--tone); height: 6px; }
  .s-UNMATCHED .step.current { background: var(--orange); }
  .who { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .assigned { color: var(--pink); font-weight: 500; }
  .acts { display: flex; gap: 6px; justify-content: flex-end; }

  .divider { padding: 14px 16px 8px; border-bottom: 1px solid var(--seam); background: var(--hull-1); }
  .empty { padding: 32px 16px; text-align: center; color: var(--ink-3); font-size: 13px; }

  @media (max-width: 1100px) {
    .stages { grid-template-columns: repeat(3, 1fr); }
    .sep { display: none; }
    .note { display: none; }
  }
  @media (max-width: 900px) {
    .th { display: none; }
    .tr {
      grid-template-columns: 1fr auto; grid-template-areas: 'id shard' 'type clock' 'stage stage' 'who who' 'acts acts';
      gap: 6px 12px; padding: 12px 16px;
    }
    .tr > :nth-child(1) { grid-area: id; } .tr > :nth-child(2) { grid-area: type; }
    .tr > :nth-child(3) { grid-area: stage; } .tr > :nth-child(4) { grid-area: shard; justify-self: end; }
    .tr > :nth-child(5) { grid-area: clock; justify-self: end; } .tr > :nth-child(6) { grid-area: who; }
    .tr > :nth-child(7) { grid-area: acts; justify-content: flex-start; }
  }
  @media (max-width: 520px) { .stages { grid-template-columns: repeat(2, 1fr); } }
</style>
