<script>
  // The console's three inputs: report an emergency, register a responder, send a burst.
  import { Siren, UserPlus, Zap, Send } from '@lucide/svelte';
  import Panel from './Panel.svelte';
  import TypePicker from './TypePicker.svelte';
  import PlacePicker from './PlacePicker.svelte';
  import { act, config, go } from './live.svelte.js';

  const TABS = [
    { id: 'report', label: 'Report', icon: Siren },
    { id: 'register', label: 'Responder', icon: UserPlus },
    { id: 'burst', label: 'Stress test', icon: Zap },
  ];
  let tab = $state('report');
  let busy = $state(false);

  let req = $state({ type: 'water', lat: null, lon: null, quantity: null, details: '' });
  let res = $state({ type: 'water', lat: null, lon: null, capacity: null, name: '' });

  async function report(e) {
    e.preventDefault();
    busy = true;
    const body = { type: req.type, location: { lat: Number(req.lat), lon: Number(req.lon) } };
    if (req.quantity) body.quantity = Number(req.quantity);
    if (req.details.trim()) body.details = req.details.trim();
    const ok = await act('POST', '/api/requests', body);
    busy = false;
    if (ok) { req.details = ''; req.quantity = null; }
  }

  async function register(e) {
    e.preventDefault();
    busy = true;
    const body = { type: res.type, name: res.name.trim(), location: { lat: Number(res.lat), lon: Number(res.lon) } };
    if (res.capacity) body.capacity = Number(res.capacity);
    const ok = await act('POST', '/api/resources', body);
    busy = false;
    if (ok) { res.name = ''; res.capacity = null; }
  }

  async function burst() {
    busy = true;
    await act('POST', '/api/burst');
    busy = false;
  }

  function keys(e) {
    const i = TABS.findIndex((t) => t.id === tab);
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const next = TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
      tab = next.id;
      document.getElementById(`tab-${next.id}`)?.focus();
    }
  }
</script>

<Panel id="dispatch" owner="request" title="Dispatch console" sub="Everything enters through the request service"
  topic={tab}>
  {#snippet info()}
    <p><b>Report</b> files an emergency. It is stored on two nodes of one shard, queued, and matched to the
      nearest free responder of the same type within 50 km.</p>
    <p><b>Responder</b> adds a volunteer, NGO or vehicle that can be matched.</p>
    <p><b>Stress test</b> sends {config.burst_size} random requests at once to watch the queue fill and drain.</p>
  {/snippet}

  <div class="tabs" role="tablist" aria-label="Dispatch" tabindex="-1" onkeydown={keys}>
    {#each TABS as t (t.id)}
      <button role="tab" id="tab-{t.id}" aria-selected={tab === t.id} aria-controls="tp-{t.id}"
        tabindex={tab === t.id ? 0 : -1} class:on={tab === t.id} onclick={() => (tab = t.id)}>
        <t.icon />{t.label}
      </button>
    {/each}
  </div>

  {#if tab === 'report'}
    <form id="tp-report" role="tabpanel" aria-labelledby="tab-report" onsubmit={report}>
      <TypePicker bind:value={req.type} types={config.types} label="What is needed" />
      <PlacePicker bind:lat={req.lat} bind:lon={req.lon} cities={config.cities} />
      <div class="row">
        <label class="field qty"><span class="label">Quantity</span>
          <input class="input" type="number" min="1" step="1" placeholder="—" bind:value={req.quantity} /></label>
        <label class="field"><span class="label">Details</span>
          <input class="input" maxlength="500" placeholder="Optional: shown to people, never used to match" bind:value={req.details} /></label>
      </div>
      <button class="btn primary lg block" disabled={busy || req.lat == null}><Send />Dispatch to Delta Command</button>
      <p class="hint">Lands on one shard, then the queue. Watch the flow above light up.</p>
    </form>
  {:else if tab === 'register'}
    <form id="tp-register" role="tabpanel" aria-labelledby="tab-register" onsubmit={register}>
      <label class="field"><span class="label">Name</span>
        <input class="input" required placeholder="A volunteer, NGO, truck…" bind:value={res.name} /></label>
      <TypePicker bind:value={res.type} types={config.types} label="What it offers" />
      <PlacePicker bind:lat={res.lat} bind:lon={res.lon} cities={config.cities} />
      <label class="field qty"><span class="label">Capacity</span>
        <input class="input" type="number" min="1" step="1" placeholder="—" bind:value={res.capacity} /></label>
      <button class="btn primary lg block" disabled={busy || !res.name.trim() || res.lat == null}><UserPlus />Register responder</button>
      <p class="hint">Joins as AVAILABLE. The matcher can pick it for the next request nearby.</p>
    </form>
  {:else}
    <div id="tp-burst" role="tabpanel" aria-labelledby="tab-burst" class="burst">
      <div class="burst-num"><span class="num">{config.burst_size}</span><span class="label">requests at once</span></div>
      <p class="dim">Random types, spread over five cities. The queue fills faster than one matcher drains it;
        on Kubernetes, KEDA adds matcher pods until it is empty again.</p>
      <button class="btn tone lg block" style="--tone:var(--yellow)" disabled={busy} onclick={burst}>
        <Zap />{busy ? 'Sending…' : `Send ${config.burst_size} requests`}
      </button>
      <button class="btn ghost sm" onclick={() => go('telemetry')}>Watch it in Grafana →</button>
    </div>
  {/if}
</Panel>

<style>
  .tabs {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px;
    padding: 3px; margin-bottom: 16px; border-radius: var(--r-2); background: var(--well);
    border: 1px solid var(--seam);
  }
  [role='tab'] {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    height: 32px; border: 0; border-radius: 7px; background: transparent; cursor: pointer;
    color: var(--ink-3); font-family: var(--f-display); font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
    transition: color 140ms, background-color 140ms;
  }
  [role='tab'] :global(svg) { width: 15px; height: 15px; }
  [role='tab']:hover { color: var(--ink); }
  [role='tab'].on { background: var(--hull-3); color: var(--ink); box-shadow: 0 0 0 1px var(--seam-2); }

  form, .burst { display: grid; gap: 16px; }
  .row { display: grid; grid-template-columns: 96px 1fr; gap: 8px; }
  .qty { max-width: 140px; }
  .row .qty { max-width: none; }
  .hint { margin-top: -6px; font-size: 12px; color: var(--ink-3); text-align: center; }

  .burst-num { display: flex; align-items: baseline; gap: 10px; }
  .burst-num .num { font-family: var(--f-display); font-size: 56px; font-weight: 600; line-height: 1; color: var(--yellow); letter-spacing: -0.03em; }
  .burst .ghost { justify-self: center; }
</style>
