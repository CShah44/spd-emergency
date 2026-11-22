<script>
  // Where: one of the seed data's cities (so there are responders nearby), or exact coordinates.
  let { lat = $bindable(), lon = $bindable(), cities } = $props();
  let city = $state('Bengaluru');
  const names = $derived(Object.keys(cities));

  function pick(name) {
    city = name;
    if (cities[name]) [lat, lon] = cities[name];
  }
  $effect(() => {
    if (lat == null && cities[city]) [lat, lon] = cities[city];
  });
</script>

<div class="field">
  <span class="label">Where</span>
  <div class="cities">
    {#each [...names, 'Custom'] as name (name)}
      <button type="button" class:on={city === name} aria-pressed={city === name} onclick={() => pick(name)}>{name}</button>
    {/each}
  </div>
  {#if city === 'Custom'}
    <div class="coords">
      <label class="field"><span class="sr-only">Latitude</span>
        <input class="input" type="number" step="0.0001" min="-90" max="90" bind:value={lat} placeholder="Latitude" /></label>
      <label class="field"><span class="sr-only">Longitude</span>
        <input class="input" type="number" step="0.0001" min="-180" max="180" bind:value={lon} placeholder="Longitude" /></label>
    </div>
  {:else}
    <p class="at mono">{lat?.toFixed(4)}, {lon?.toFixed(4)}</p>
  {/if}
</div>

<style>
  .cities { display: flex; flex-wrap: wrap; gap: 6px; }
  button {
    height: 30px; padding: 0 10px;
    border: 1px solid var(--seam-2); border-radius: 999px; background: transparent;
    color: var(--ink-2); font-size: 12px; font-weight: 500; cursor: pointer;
    transition: color 140ms, border-color 140ms, background-color 140ms;
  }
  button:hover { color: var(--ink); border-color: var(--seam-3); }
  button.on { background: var(--ink); border-color: var(--ink); color: var(--hull-0); font-weight: 600; }
  .coords { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .at { color: var(--ink-4); font-size: 11px; }
</style>
