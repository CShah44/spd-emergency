<script>
  // What just happened, newest first, each line tagged with the squad member that did it.
  import { fly } from 'svelte/transition';
  import Panel from './Panel.svelte';
  import Callsign from './Callsign.svelte';
  import { live } from './live.svelte.js';
  import { KIND_OWNER } from './squad.js';

  const events = $derived(live.snap?.events ?? []);
  const KIND_LABEL = { request: 'request', responder: 'responder', matching: 'match', storage: 'storage', coordinator: 'leader' };
  let kind = $state(null);
  const shown = $derived(kind ? events.filter((e) => e.kind === kind) : events);
</script>

<Panel id="comms" title="Comms log" sub="What just happened · UTC" class="fill">
  {#snippet info()}
    <p>Found by comparing each refresh with the one before: new requests and responders, every change of
      stage, nodes going down or coming back, and leader changes. The number says which part of the system
      did it.</p>
  {/snippet}
  {#snippet actions()}
    <span class="live" class:on={live.connected}><i></i>{live.connected ? 'Live' : 'Reconnecting'}</span>
  {/snippet}

  <div class="kinds">
    <button class:on={!kind} onclick={() => (kind = null)}>All</button>
    {#each Object.keys(KIND_LABEL) as k (k)}
      <button class:on={kind === k} onclick={() => (kind = kind === k ? null : k)}>{KIND_LABEL[k]}</button>
    {/each}
  </div>

  <div class="box">
  <ol class="feed" aria-live="polite">
    {#each shown as e (e.id)}
      <li in:fly={{ y: -6, duration: 220 }}>
        <span class="when mono">{e.when}</span>
        <Callsign who={KIND_OWNER[e.kind]} size="sm" />
        <span class="text" style="--tone:{e.color}">{e.text}</span>
      </li>
    {:else}
      <li class="empty">Quiet so far. Dispatch a request and watch it travel.</li>
    {/each}
  </ol>
  </div>
</Panel>

<style>
  .live { display: inline-flex; align-items: center; gap: 6px; font-family: var(--f-display); font-size: 11px;
    font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); }
  .live i { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-4); }
  .live.on i { background: var(--green); animation: breathe 2s var(--ease-move) infinite; }
  @keyframes breathe { 50% { box-shadow: 0 0 0 4px rgba(25, 210, 122, 0.18); } }

  .kinds { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
  .kinds button {
    height: 24px; padding: 0 8px; border: 1px solid transparent; border-radius: 999px; background: transparent;
    color: var(--ink-3); font-size: 11.5px; cursor: pointer; text-transform: capitalize;
  }
  .kinds button:hover { color: var(--ink); }
  .kinds button.on { border-color: var(--seam-3); color: var(--ink); background: var(--hull-2); }

  /* the list is taken out of flow so it fills the panel without making the row taller */
  .box { position: relative; flex: 1; min-height: 360px; margin: 0 -16px -16px; border-top: 1px solid var(--seam); }
  .feed { position: absolute; inset: 0; list-style: none; margin: 0; padding: 0; overflow-y: auto; }
  li { display: grid; grid-template-columns: 58px 16px 1fr; gap: 10px; align-items: start; padding: 8px 16px;
    border-bottom: 1px solid var(--seam); font-size: 12.5px; line-height: 1.45; }
  li :global(.callsign) { margin-top: 1px; }
  .when { color: var(--ink-4); font-size: 11px; padding-top: 1px; }
  .text { color: color-mix(in srgb, var(--tone) 55%, var(--ink-2)); word-break: break-word; }
  .empty { display: block; padding: 24px 16px; text-align: center; color: var(--ink-3); }
</style>
