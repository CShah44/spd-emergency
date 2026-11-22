<script>
  // What is needed / offered: one of five, as a row of keys rather than a dropdown, since it is
  // picked on almost every report.
  import { Droplet, Wheat, Pill, Truck, Tent } from '@lucide/svelte';
  let { value = $bindable(), types, label } = $props();
  const ICONS = { water: Droplet, food: Wheat, medicine: Pill, transport: Truck, shelter: Tent };
</script>

<div class="field">
  <span class="label" id="tp-{label}">{label}</span>
  <div class="keys" role="radiogroup" aria-labelledby="tp-{label}">
    {#each types as t (t)}
      {@const Icon = ICONS[t]}
      <button type="button" role="radio" aria-checked={value === t} class:on={value === t} onclick={() => (value = t)}>
        {#if Icon}<Icon />{/if}<span>{t}</span>
      </button>
    {/each}
  </div>
</div>

<style>
  .keys { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px; }
  button {
    display: grid; justify-items: center; gap: 4px;
    padding: 9px 2px 7px;
    border: 1px solid var(--seam-2); border-radius: var(--r-1);
    background: var(--well); color: var(--ink-3);
    font-size: 11px; font-weight: 500; text-transform: capitalize; cursor: pointer;
    transition: color 140ms, border-color 140ms, background-color 140ms, transform 120ms var(--ease-out);
  }
  button :global(svg) { width: 18px; height: 18px; }
  button:hover { color: var(--ink); border-color: var(--seam-3); }
  button:active { transform: scale(0.97); }
  button.on { color: var(--ink); border-color: var(--ink-2); background: var(--hull-3); }
  span { overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
</style>
