<script>
  // The "?" on a panel: what it shows and what its buttons do, with a link to the full briefing.
  import { CircleHelp } from '@lucide/svelte';
  import { go } from './live.svelte.js';

  let { title, topic = null, children } = $props();
  const uid = `info-${Math.random().toString(36).slice(2, 8)}`;
  let button, pop;

  function place() {
    const r = button.getBoundingClientRect();
    const width = Math.min(340, window.innerWidth - 32);
    const left = Math.max(16, Math.min(r.right - width, window.innerWidth - width - 16));
    pop.style.width = `${width}px`;
    pop.style.left = `${left}px`;
    pop.style.top = `${r.bottom + 8}px`;
    pop.style.transformOrigin = `${r.right - left - 10}px top`;
  }
</script>

<button class="trigger" bind:this={button} popovertarget={uid} aria-label="What is {title}?" onclick={place}>
  <CircleHelp />
</button>
<div class="pop" id={uid} popover bind:this={pop} ontoggle={(e) => e.newState === 'open' && place()}>
  <p class="label">{title}</p>
  <div class="body">{@render children()}</div>
  {#if topic}
    <button class="more" onclick={() => { pop.hidePopover(); go('briefing', topic); }}>
      Step by step in the briefing →
    </button>
  {/if}
</div>

<style>
  .trigger {
    display: grid; place-items: center; width: 28px; height: 28px; margin: -4px -6px -4px 0;
    border: 0; border-radius: var(--r-1); background: transparent; color: var(--ink-3); cursor: pointer;
    transition: color 140ms, background-color 140ms;
  }
  .trigger:hover { color: var(--ink); background: var(--hull-2); }
  .trigger :global(svg) { width: 16px; height: 16px; }

  .pop {
    position: fixed; inset: auto; margin: 0;
    padding: 14px 16px; border-radius: var(--r-2);
    border: 1px solid var(--seam-3); background: var(--hull-3); color: var(--ink-2);
    font-size: 13px; line-height: 1.55;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
    opacity: 0; transform: scale(0.96);
    transition: opacity 160ms var(--ease-out), transform 160ms var(--ease-out), display 160ms allow-discrete, overlay 160ms allow-discrete;
  }
  .pop:popover-open { opacity: 1; transform: none; }
  @starting-style { .pop:popover-open { opacity: 0; transform: scale(0.96); } }
  .pop .label { margin-bottom: 6px; color: var(--ink); }
  .body :global(p + p) { margin-top: 8px; }
  .body :global(b) { color: var(--ink); font-weight: 600; }
  .more {
    margin-top: 12px; padding: 0; border: 0; background: none; cursor: pointer;
    color: var(--yellow); font-family: var(--f-display); font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
  }
  .more:hover { text-decoration: underline; text-underline-offset: 3px; }
</style>
