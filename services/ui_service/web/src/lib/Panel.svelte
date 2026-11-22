<script>
  // A console panel: the owner's callsign, a title, an optional "?" and controls on the right.
  import Callsign from './Callsign.svelte';
  import Info from './Info.svelte';
  import { ui } from './live.svelte.js';

  let { id, owner = null, title, sub = '', info = null, topic = null, actions, children, flush = false, class: cls = '' } = $props();
  let lit = $state(false);
  $effect(() => {
    if (ui.spotlight !== id || !ui.spotlightAt) return;
    lit = false;
    const on = requestAnimationFrame(() => (lit = true));
    const off = setTimeout(() => (lit = false), 2500);
    return () => { cancelAnimationFrame(on); clearTimeout(off); };
  });
</script>

<section class="panel {cls}" class:spotlight={lit} id="panel-{id}" aria-labelledby="h-{id}">
  <header>
    {#if owner}<Callsign who={owner} />{/if}
    <div class="titles">
      <h2 id="h-{id}">{title}</h2>
      {#if sub}<p class="sub">{sub}</p>{/if}
    </div>
    <div class="right">
      {#if actions}{@render actions()}{/if}
      {#if info}<Info title={title} {topic}>{@render info()}</Info>{/if}
    </div>
  </header>
  <div class:panel-body={!flush}>{@render children()}</div>
</section>

<style>
  header {
    display: flex; align-items: center; gap: 10px;
    padding: 14px 16px 0;
  }
  .titles { min-width: 0; flex: 1; }
  h2 {
    font-size: 13px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink);
    line-height: 20px;
  }
  .sub { font-size: 12px; color: var(--ink-3); line-height: 1.4; }
  .right { display: flex; align-items: center; gap: 8px; flex: none; }
</style>
