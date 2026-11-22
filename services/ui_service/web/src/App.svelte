<script>
  import { LayoutDashboard, Activity, BookOpen, ExternalLink } from '@lucide/svelte';
  import MissionFlow from './lib/MissionFlow.svelte';
  import Dispatch from './lib/Dispatch.svelte';
  import StorageVault from './lib/StorageVault.svelte';
  import RequestsBoard from './lib/RequestsBoard.svelte';
  import Responders from './lib/Responders.svelte';
  import Comms from './lib/Comms.svelte';
  import Radar from './lib/Radar.svelte';
  import Telemetry from './lib/Telemetry.svelte';
  import Briefing from './lib/Briefing.svelte';
  import Toasts from './lib/Toasts.svelte';
  import { live, ui, go, readHash, grafanaLink } from './lib/live.svelte.js';

  const VIEWS = [
    { id: 'command', label: 'Command', icon: LayoutDashboard, key: '1' },
    { id: 'telemetry', label: 'Telemetry', icon: Activity, key: '2' },
    { id: 'briefing', label: 'Briefing', icon: BookOpen, key: '3' },
  ];

  readHash();
  $effect(() => {
    const onHash = () => readHash();
    const onKey = (e) => {
      if (e.target.closest('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
      const v = VIEWS.find((x) => x.key === e.key);
      if (v) go(v.id);
      if (e.key === '?') go('briefing');
    };
    addEventListener('hashchange', onHash);
    addEventListener('keydown', onKey);
    return () => { removeEventListener('hashchange', onHash); removeEventListener('keydown', onKey); };
  });

  // a clock that ticks, so "last update" can say how fresh the picture is
  let now = $state(Date.now());
  $effect(() => { const t = setInterval(() => (now = Date.now()), 1000); return () => clearInterval(t); });
  const utc = $derived(new Date(now).toISOString().slice(11, 19));
  const link = $derived.by(() => {
    if (!live.snap) return { cls: 'wait', text: 'Connecting' };
    if (!live.connected && now - live.lastAt > 5000) return { cls: 'bad', text: 'Page offline' };
    if (!live.snap.link.ok) return { cls: 'bad', text: 'API unreachable' };
    return { cls: 'ok', text: 'Link OK' };
  });
</script>

<header class="top">
  <div class="inner">
    <a class="brand" href="#/command" onclick={() => go('command')} aria-label="Delta Command home">
      <img src="/badge.svg" alt="" width="30" height="30" />
      <span class="word">
        <b>Delta Command</b>
        <small>S.P.D. Emergency Response</small>
      </span>
    </a>

    <nav aria-label="Views">
      {#each VIEWS as v (v.id)}
        <a href="#/{v.id}" class:on={ui.view === v.id} aria-current={ui.view === v.id ? 'page' : undefined}
          onclick={(e) => { e.preventDefault(); go(v.id); }}>
          <v.icon /><span>{v.label}</span><kbd>{v.key}</kbd>
        </a>
      {/each}
    </nav>

    <div class="status">
      <span class="link {link.cls}" title={live.snap?.link.note || ''}><i></i>{link.text}</span>
      <span class="clock mono num" title="UTC">{utc}</span>
      <a class="btn sm grafana" href={grafanaLink()} target="_blank" rel="noopener" title="Open the Grafana dashboard">
        <Activity /><span>Grafana</span><ExternalLink />
      </a>
    </div>
  </div>
  <div class="band" aria-hidden="true"></div>
</header>

<main class="view-{ui.view}">
  {#if ui.view === 'command'}
    <MissionFlow />
    <div class="row a">
      <Dispatch />
      <StorageVault />
    </div>
    <div class="row b">
      <RequestsBoard />
      <Comms />
    </div>
    <div class="row c">
      <Responders />
      <Radar />
    </div>
    <footer>
      Press <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> to switch views, <kbd>?</kbd> for the briefing.
      Every number above comes from the request service, refreshed every second.
    </footer>
  {:else if ui.view === 'telemetry'}
    <Telemetry />
  {:else}
    <Briefing />
  {/if}
</main>

<Toasts />

<style>
  .top { position: sticky; top: 0; z-index: 20; background: color-mix(in srgb, var(--hull-0) 88%, transparent);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
  .inner { max-width: 1680px; margin: 0 auto; height: 60px; padding: 0 24px; display: flex; align-items: center; gap: 24px; }
  /* the S.P.D. patrol band: a checker strip in the hull's own greys */
  .band { height: 4px; opacity: 0.9;
    background: repeating-linear-gradient(90deg, var(--hull-3) 0 8px, var(--hull-0) 8px 16px),
      var(--hull-0);
    border-top: 1px solid var(--seam); border-bottom: 1px solid var(--seam-2); }

  .brand { display: flex; align-items: center; gap: 10px; text-decoration: none; flex: none; }
  .word { display: grid; line-height: 1.15; }
  .word b { font-family: var(--f-display); font-size: 16px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
  .word small { font-size: 11px; color: var(--ink-3); letter-spacing: 0.04em; }

  nav { display: flex; gap: 2px; padding: 3px; border-radius: var(--r-2); background: var(--hull-1); border: 1px solid var(--seam); }
  nav a { display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 12px; border-radius: 7px;
    text-decoration: none; color: var(--ink-3); font-family: var(--f-display); font-size: 13px; font-weight: 600; letter-spacing: 0.04em;
    transition: color 140ms, background-color 140ms; }
  nav a:hover { color: var(--ink); }
  nav a.on { background: var(--hull-3); color: var(--ink); box-shadow: 0 0 0 1px var(--seam-2); }
  nav :global(svg) { width: 15px; height: 15px; }
  kbd { font-family: var(--f-mono); font-size: 10px; color: var(--ink-4); padding: 0 4px; border: 1px solid var(--seam-2); border-radius: 3px; line-height: 15px; }
  nav a.on kbd { color: var(--ink-3); }

  .status { margin-left: auto; display: flex; align-items: center; gap: 14px; }
  .link { display: inline-flex; align-items: center; gap: 7px; font-family: var(--f-display); font-size: 12px; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-2); }
  .link i { width: 8px; height: 8px; border-radius: 50%; background: var(--ink-4); }
  .link.ok i { background: var(--green); box-shadow: 0 0 0 3px rgba(25, 210, 122, 0.15); }
  .link.bad { color: var(--red); }
  .link.bad i { background: var(--red); animation: blink 1s steps(2) infinite; }
  @keyframes blink { 50% { opacity: 0.25; } }
  .clock { color: var(--ink-3); font-size: 12px; }
  .grafana :global(svg:last-child) { width: 12px; height: 12px; color: var(--ink-3); }

  main { max-width: 1680px; margin: 0 auto; padding: 20px 24px 40px; display: grid; gap: 16px; }
  .row { display: grid; gap: 16px; align-items: start; }
  .row.a { grid-template-columns: minmax(340px, 4fr) 8fr; }
  .row.b { grid-template-columns: 8.5fr minmax(320px, 3.5fr); align-items: stretch; }
  .row.c { grid-template-columns: 7fr 5fr; }
  footer { padding: 16px 0 0; text-align: center; font-size: 12px; color: var(--ink-4); }
  footer kbd { color: var(--ink-3); }

  @media (max-width: 1280px) {
    .row.a, .row.b, .row.c { grid-template-columns: 1fr; }
  }
  @media (max-width: 860px) {
    .inner { padding: 0 16px; gap: 12px; }
    .word small, .clock, nav kbd, .grafana span, .grafana :global(svg:last-child) { display: none; }
    nav a span { display: none; }
    main { padding: 16px 16px 32px; }
  }
  @media (max-width: 520px) {
    .word { display: none; }
    .link { font-size: 0; gap: 0; }
  }
</style>
