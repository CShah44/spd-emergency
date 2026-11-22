<script>
  import { fly } from 'svelte/transition';
  import { CircleCheck, CircleAlert, X } from '@lucide/svelte';
  import { ui, dismiss } from './live.svelte.js';
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each ui.toasts as t (t.id)}
    <div class="toast {t.tone}" in:fly={{ y: 12, duration: 200 }} out:fly={{ x: 16, duration: 140 }}>
      {#if t.tone === 'bad'}<CircleAlert />{:else}<CircleCheck />{/if}
      <p>{t.text}</p>
      <button aria-label="Dismiss" onclick={() => dismiss(t.id)}><X /></button>
    </div>
  {/each}
</div>

<style>
  .toasts { position: fixed; right: 16px; bottom: 16px; z-index: 50; display: grid; gap: 8px; width: min(400px, calc(100vw - 32px)); }
  .toast { display: grid; grid-template-columns: 18px 1fr 24px; gap: 10px; align-items: start; padding: 12px 12px 12px 14px;
    border-radius: var(--r-2); border: 1px solid var(--seam-3); background: var(--hull-3); box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5); }
  .toast > :global(svg) { width: 18px; height: 18px; margin-top: 1px; }
  .ok > :global(svg) { color: var(--green); }
  .bad { border-color: rgba(255, 51, 71, 0.45); }
  .bad > :global(svg) { color: var(--red); }
  p { font-size: 13px; line-height: 1.45; color: var(--ink); }
  button { display: grid; place-items: center; width: 24px; height: 24px; border: 0; border-radius: 4px; background: none; color: var(--ink-3); cursor: pointer; }
  button:hover { color: var(--ink); background: var(--hull-2); }
  button :global(svg) { width: 14px; height: 14px; }
</style>
