<script>
  // A squad member's mark: its number on a chip in its color. Used on panels, in the feed and the briefing.
  import { SQUAD } from './squad.js';
  let { who, size = 'md', label = false } = $props();
  const m = $derived(SQUAD[who]);
</script>

{#if m}
  <span class="callsign {size}" style="--c:{m.color}" title="{m.n} · {m.name}">
    <span class="n">{m.n}</span>
    {#if label}<span class="name">{m.name}</span>{/if}
  </span>
{/if}

<style>
  .callsign { display: inline-flex; align-items: center; gap: 8px; flex: none; }
  .n {
    display: grid; place-items: center;
    width: 20px; height: 20px;
    /* the badge's cut corner: an S.P.D. number plate, not a circle */
    clip-path: polygon(0 0, 100% 0, 100% 70%, 70% 100%, 0 100%);
    background: var(--c);
    color: var(--hull-0);
    font-family: var(--f-display); font-size: 12px; font-weight: 700; line-height: 1;
  }
  .sm .n { width: 16px; height: 16px; font-size: 10px; }
  .lg .n { width: 28px; height: 28px; font-size: 16px; }
  .name { font-family: var(--f-display); font-weight: 600; font-size: 13px; color: var(--ink); letter-spacing: 0.02em; }
</style>
