<script>
  // Who can help: assigned first (and what they are serving), then free, then offline.
  import { Droplet, Wheat, Pill, Truck, Tent } from '@lucide/svelte';
  import Panel from './Panel.svelte';
  import ShardChip from './ShardChip.svelte';
  import { live, runAction, isFlashing } from './live.svelte.js';
  import { pretty } from './squad.js';

  const ICONS = { water: Droplet, food: Wheat, medicine: Pill, transport: Truck, shelter: Tent };
  const SHOWN = 120;
  const list = $derived(live.snap?.resources ?? []);
  const health = $derived(live.snap?.shard_health ?? {});
  const counts = $derived(['ASSIGNED', 'AVAILABLE', 'OFFLINE'].map((s) => [s, list.filter((r) => r.status === s).length]));
  let busy = $state({});

  async function run(r, a) {
    busy[r.id] = true;
    await runAction('resource', r, a);
    busy[r.id] = false;
  }
</script>

<Panel id="responders" owner="matching" title="Responders" topic="responder"
  sub={list.length ? `${list.length.toLocaleString()} registered` : 'Nobody registered yet'}>
  {#snippet info()}
    <p>Volunteers, NGOs and vehicles. The matcher only picks <b>AVAILABLE</b> ones of the right type, nearest
      first, within 50 km.</p>
    <p>An <b>ASSIGNED</b> responder is freed when its request is fulfilled or cancelled. <b>Take offline</b> keeps
      a free one out of matching.</p>
  {/snippet}

  <div class="counts">
    {#each counts as [s, n] (s)}
      <span class="pill s-{s}"><span class="num">{n.toLocaleString()}</span> {s.toLowerCase()}</span>
    {/each}
  </div>

  <div class="list">
    {#if !list.length}
      <p class="empty">Register one from the dispatch console, or run <code>python scripts/seed.py</code>.</p>
    {/if}
    {#each list.slice(0, SHOWN) as r (r.id)}
      {@const Icon = ICONS[r.type]}
      <div class="item s-{r.status}" class:flash={isFlashing(r.id)} class:off={r.status === 'OFFLINE'}>
        <span class="icon">{#if Icon}<Icon />{/if}</span>
        <span class="main">
          <span class="name">{r.name}</span>
          <span class="meta"><span class="mono">{r.short}</span> · {r.type}{#if r.capacity} · cap {r.capacity}{/if}</span>
        </span>
        <span class="serving">
          {#if r.serving}
            <span class="mono">→ {r.serving.short}</span>
            {#if r.serving.status}<span class="pill bare s-{r.serving.status}">{pretty(r.serving.status)}</span>{/if}
          {:else}
            <span class="pill s-{r.status}">{r.status}</span>
          {/if}
        </span>
        <ShardChip shard={r.shard} health={health[r.shard] ?? 'ok'} />
        <span class="acts">
          {#each r.actions as a (a.label)}
            <button class="btn sm tone" style="--tone:{a.tone === 'negative' ? 'var(--ink-3)' : 'var(--green)'}"
              disabled={busy[r.id]} onclick={() => run(r, a)}>{a.label}</button>
          {/each}
        </span>
      </div>
    {/each}
    {#if list.length > SHOWN}<p class="empty">Showing {SHOWN} of {list.length.toLocaleString()}</p>{/if}
  </div>
</Panel>

<style>
  .counts { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .list { margin: 0 -16px -16px; border-top: 1px solid var(--seam); max-height: 520px; overflow-y: auto; }
  .item {
    display: grid; grid-template-columns: 32px minmax(0, 1fr) auto auto 112px; align-items: center; gap: 12px;
    padding: 10px 16px; border-bottom: 1px solid var(--seam);
  }
  .item:hover { background: rgba(170, 190, 225, 0.03); }
  .icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: var(--r-1);
    background: color-mix(in srgb, var(--tone) 12%, var(--hull-2)); color: var(--tone); }
  .icon :global(svg) { width: 16px; height: 16px; }
  .main { display: grid; min-width: 0; }
  .name { color: var(--ink); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 11.5px; color: var(--ink-3); text-transform: capitalize; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta .mono { text-transform: none; }
  .serving { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); justify-self: end; }
  .acts { display: flex; justify-content: flex-end; }
  .off { opacity: 0.55; }
  .empty { padding: 24px 16px; text-align: center; color: var(--ink-3); font-size: 13px; }
  code { font-family: var(--f-mono); font-size: 12px; color: var(--ink-2); }

  @media (max-width: 620px) {
    .item { grid-template-columns: 32px minmax(0, 1fr) auto; }
    .item > :global(:nth-child(4)) { display: none; }
    .acts { grid-column: 2 / -1; justify-content: flex-start; }
    .acts:empty { display: none; }
  }
</style>
