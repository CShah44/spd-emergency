<script>
  // Grafana, inside the page: the provisioned dashboard in kiosk mode, plus the other consoles.
  import { ExternalLink, RefreshCw, Activity, Database, Rabbit, BookOpen } from '@lucide/svelte';
  import { config, grafanaLink } from './live.svelte.js';

  const RANGES = ['5m', '15m', '1h', '6h'];
  let range = $state('15m');
  let reach = $state('checking'); // checking | up | down
  let frameKey = $state(0);

  async function probe() {
    reach = 'checking';
    try {
      // no-cors: an opaque answer still proves Grafana is there; a refused connection throws
      await fetch(`${config.grafana_url}/api/health`, { mode: 'no-cors', cache: 'no-store' });
      reach = 'up';
    } catch {
      reach = 'down';
    }
  }
  $effect(() => { config.grafana_url; probe(); });

  const consoles = $derived([
    { name: 'Grafana', href: grafanaLink(false, range), icon: Activity, note: 'Edit panels: admin / admin' },
    { name: 'Prometheus', href: config.prometheus_url, icon: Database, note: 'Raw metrics and queries' },
    { name: 'RabbitMQ', href: config.rabbitmq_url, icon: Rabbit, note: 'Queues · guest / guest' },
    { name: 'API docs', href: config.api_docs_url, icon: BookOpen, note: 'Swagger for the request service' },
  ]);
</script>

<div class="telemetry">
  <div class="bar">
    <div class="titles">
      <h1>Telemetry</h1>
      <p class="dim">The Grafana dashboard: Prometheus scrapes every service every 5 s.</p>
    </div>
    <div class="ranges" role="group" aria-label="Time range">
      {#each RANGES as r (r)}
        <button class:on={range === r} aria-pressed={range === r} onclick={() => (range = r)}>{r}</button>
      {/each}
    </div>
    <button class="btn sm" onclick={() => { frameKey++; probe(); }}><RefreshCw />Reload</button>
    <a class="btn sm primary" href={grafanaLink(false, range)} target="_blank" rel="noopener">Open in Grafana <ExternalLink /></a>
  </div>

  <div class="stage">
    {#if reach === 'down'}
      <div class="down">
        <Activity />
        <h2>Grafana is not answering at {config.grafana_url}</h2>
        <p class="dim">With Docker Compose it starts with the stack. On Kubernetes, keep
          <code>python scripts/k8s_forward.py</code> running so localhost:3000 reaches it.</p>
        <button class="btn" onclick={probe}><RefreshCw />Try again</button>
      </div>
    {:else if reach === 'checking'}
      <div class="down"><RefreshCw class="spin" /><p class="dim">Reaching Grafana…</p></div>
    {:else}
      {#key `${frameKey}-${range}`}
        <iframe title="Grafana: S.P.D. Emergency dashboard" src={grafanaLink(true, range)}></iframe>
      {/key}
    {/if}
  </div>
  <p class="hint">Blank frame? Grafana must allow embedding (GF_SECURITY_ALLOW_EMBEDDING=true, set in
    docker-compose.yml and k8s/observability/grafana.yaml). “Open in Grafana” always works.</p>

  <div class="consoles">
    {#each consoles as c (c.name)}
      <a class="console" href={c.href} target="_blank" rel="noopener">
        <c.icon />
        <span><b>{c.name}</b><small>{c.note}</small></span>
        <ExternalLink />
      </a>
    {/each}
  </div>
</div>

<style>
  .telemetry { display: grid; gap: 16px; }
  .bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .titles { flex: 1; min-width: 240px; }
  h1 { font-size: var(--t-h1); letter-spacing: -0.01em; }
  .bar .dim { font-size: 13px; }
  .ranges { display: flex; padding: 3px; gap: 2px; border-radius: var(--r-2); background: var(--well); border: 1px solid var(--seam); }
  .ranges button { height: 28px; padding: 0 10px; border: 0; border-radius: 7px; background: none; cursor: pointer;
    color: var(--ink-3); font-family: var(--f-mono); font-size: 12px; }
  .ranges button:hover { color: var(--ink); }
  .ranges button.on { background: var(--hull-3); color: var(--ink); box-shadow: 0 0 0 1px var(--seam-2); }

  .stage { position: relative; height: calc(100vh - 290px); min-height: 520px; border-radius: var(--r-3);
    border: 1px solid var(--seam-2); background: var(--hull-1); overflow: hidden; }
  iframe { width: 100%; height: 100%; border: 0; display: block; }
  .down { height: 100%; display: grid; place-content: center; justify-items: center; gap: 12px; text-align: center; padding: 24px; }
  .down :global(svg) { width: 28px; height: 28px; color: var(--ink-3); }
  .down h2 { font-size: var(--t-h3); }
  .down p { max-width: 460px; font-size: 13px; }
  code { font-family: var(--f-mono); font-size: 12px; color: var(--ink); }

  .hint { margin-top: -8px; font-size: 11.5px; color: var(--ink-4); }
  .consoles { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
  .console { display: flex; align-items: center; gap: 12px; padding: 12px 14px; text-decoration: none;
    border: 1px solid var(--seam-2); border-radius: var(--r-2); background: var(--hull-1);
    transition: border-color 140ms, background-color 140ms; }
  .console:hover { border-color: var(--seam-3); background: var(--hull-2); }
  .console :global(svg) { width: 18px; height: 18px; color: var(--ink-3); flex: none; }
  .console span { flex: 1; display: grid; }
  .console b { font-family: var(--f-display); font-weight: 600; font-size: 14px; }
  .console small { font-size: 12px; color: var(--ink-3); }
</style>
