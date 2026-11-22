<script>
  // The briefing: the squad, what every control does step by step, and demo playbooks.
  import { ArrowRight, Activity, Clock } from '@lucide/svelte';
  import Callsign from './Callsign.svelte';
  import { SQUAD, SQUAD_ORDER } from './squad.js';
  import { OPERATIONS, PLAYBOOKS } from './briefing.js';
  import { ui, point, go } from './live.svelte.js';

  $effect(() => {
    const topic = ui.topic;
    if (!topic) return;
    requestAnimationFrame(() => document.getElementById(`b-${topic}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  });

  const PANEL_NAME = { dispatch: 'Dispatch console', requests: 'Requests board', responders: 'Responders',
    storage: 'Storage vault', flow: 'Mission flow', telemetry: 'Telemetry' };
</script>

<div class="briefing">
  <header class="intro">
    <p class="label">Briefing</p>
    <h1>How Delta Command works, and what every control does</h1>
    <p class="lede">An emergency report enters through the front door, is stored twice on one of three shards,
      waits on a queue, and is paired with the nearest free responder. Five parts do the work; each keeps
      its number and color everywhere on the console.</p>
    <nav class="toc" aria-label="On this page">
      <a href="#/briefing/squad">The squad</a>
      <a href="#/briefing/ops">Operations</a>
      <a href="#/briefing/playbooks">Demo playbooks</a>
    </nav>
  </header>

  <section id="b-squad" aria-labelledby="h-squad">
    <h2 id="h-squad">The squad</h2>
    <div class="squad">
      {#each SQUAD_ORDER as who (who)}
        {@const m = SQUAD[who]}
        <article class="member" id="b-squad-{who}" style="--c:{m.color}" class:lit={ui.topic === `squad-${who}`}>
          <div class="m-top"><Callsign {who} size="lg" /><span class="label">{m.call}</span></div>
          <h3>{m.name}</h3>
          <p>{m.what}</p>
        </article>
      {/each}
    </div>
  </section>

  <section id="b-ops" aria-labelledby="h-ops">
    <h2 id="h-ops">Operations</h2>
    <div class="ops">
      {#each OPERATIONS as op (op.id)}
        <article class="op" id="b-{op.id}" class:lit={ui.topic === op.id}>
          <header>
            <div>
              <h3>{op.title}</h3>
              <p class="dim">{op.summary}</p>
            </div>
            <code>{op.call}</code>
          </header>
          <ol class="steps">
            {#each op.steps as [who, text], i (i)}
              <li><Callsign {who} size="sm" /><span>{text}</span></li>
            {/each}
          </ol>
          <div class="watch">
            <p><span class="label">Watch</span>{op.watch}</p>
            {#if op.grafana.length}
              <p class="graf"><Activity /><span>{op.grafana.join(' · ')}</span></p>
            {/if}
          </div>
          <button class="btn sm" onclick={() => point(op.panel)}>Go to {PANEL_NAME[op.panel]}<ArrowRight /></button>
        </article>
      {/each}
    </div>
  </section>

  <section id="b-playbooks" aria-labelledby="h-play">
    <h2 id="h-play">Demo playbooks</h2>
    <div class="plays">
      {#each PLAYBOOKS as pb (pb.id)}
        <article class="play" style="--c:{pb.color}">
          <header>
            <h3>{pb.title}</h3>
            <span class="time"><Clock />{pb.minutes} min</span>
          </header>
          <p class="dim">{pb.goal}</p>
          <ol>
            {#each pb.steps as [panel, text], i (i)}
              <li>
                <span class="n num">{i + 1}</span>
                <span class="t">{text}</span>
                <button class="go" onclick={() => point(panel)} aria-label="Show {PANEL_NAME[panel] ?? panel}">
                  {PANEL_NAME[panel] ?? panel}<ArrowRight />
                </button>
              </li>
            {/each}
          </ol>
        </article>
      {/each}
    </div>
    <p class="back"><button class="btn" onclick={() => go('command')}>Back to the console <ArrowRight /></button></p>
  </section>
</div>

<style>
  .briefing { display: grid; gap: 48px; max-width: 1180px; margin: 0 auto; padding-bottom: 48px; }
  section { scroll-margin-top: 88px; display: grid; gap: 16px; }
  article { scroll-margin-top: 88px; }
  .intro { display: grid; gap: 12px; padding-top: 16px; max-width: 760px; }
  h1 { font-size: 36px; line-height: 1.1; letter-spacing: -0.02em; }
  .lede { font-size: 16px; color: var(--ink-2); line-height: 1.6; }
  .toc { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
  .toc a { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--seam-2); color: var(--ink-2);
    text-decoration: none; font-size: 13px; }
  .toc a:hover { color: var(--ink); border-color: var(--seam-3); }
  h2 { font-size: var(--t-h2); letter-spacing: -0.01em; }
  h3 { font-size: var(--t-h4); }

  .squad { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
  .member { display: grid; gap: 8px; align-content: start; padding: 16px; border-radius: var(--r-3);
    border: 1px solid var(--seam-2); background: var(--hull-1); position: relative; overflow: hidden; }
  .member::before { content: ''; position: absolute; inset: 0 0 auto; height: 2px; background: var(--c); }
  .member p { font-size: 13px; color: var(--ink-2); line-height: 1.55; }
  .m-top { display: flex; align-items: center; gap: 10px; }
  .lit { animation: lit 2.4s var(--ease-out); }
  @keyframes lit { 0%, 40% { box-shadow: 0 0 0 2px var(--yellow); } 100% { box-shadow: none; } }

  .ops { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .op { display: grid; gap: 14px; align-content: start; padding: 18px; border-radius: var(--r-3);
    border: 1px solid var(--seam-2); background: var(--hull-1); }
  .op header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .op header .dim { font-size: 13px; margin-top: 2px; }
  .op code { flex: none; font-family: var(--f-mono); font-size: 11px; color: var(--ink-2); padding: 3px 7px;
    border-radius: 4px; background: var(--hull-3); white-space: nowrap; }
  .steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 0; counter-reset: s; }
  .steps li { display: grid; grid-template-columns: 16px 1fr; gap: 10px; padding: 7px 0; font-size: 13px;
    color: var(--ink-2); line-height: 1.5; position: relative; }
  .steps li :global(.callsign) { margin-top: 2px; }
  .steps li + li::before { content: ''; position: absolute; left: 7.5px; top: -6px; height: 10px; width: 1px; background: var(--seam-3); }
  .watch { display: grid; gap: 6px; padding: 12px; border-radius: var(--r-2); background: var(--hull-2); font-size: 13px; color: var(--ink-2); }
  .watch .label { margin-right: 8px; color: var(--yellow); }
  .graf { display: flex; gap: 8px; align-items: flex-start; color: var(--ink-3); font-size: 12px; }
  .graf :global(svg) { width: 14px; height: 14px; flex: none; margin-top: 2px; }
  .op > .btn { justify-self: start; }

  .plays { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .play { display: grid; gap: 10px; align-content: start; padding: 18px; border-radius: var(--r-3);
    border: 1px solid var(--seam-2); border-top: 2px solid var(--c); background: var(--hull-1); }
  .play header { display: flex; align-items: center; justify-content: space-between; }
  .play .dim { font-size: 13px; }
  .time { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--ink-3); }
  .time :global(svg) { width: 13px; height: 13px; }
  .play ol { list-style: none; margin: 4px 0 0; padding: 0; display: grid; }
  .play li { display: grid; grid-template-columns: 22px 1fr; gap: 4px 10px; padding: 9px 0; border-top: 1px solid var(--seam); font-size: 13px; }
  .n { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--hull-3);
    font-family: var(--f-display); font-weight: 600; font-size: 12px; color: var(--c); }
  .t { color: var(--ink-2); line-height: 1.45; padding-top: 2px; }
  .go { grid-column: 2; justify-self: start; display: inline-flex; align-items: center; gap: 4px; padding: 0; border: 0;
    background: none; color: var(--ink-3); font-size: 12px; cursor: pointer; }
  .go:hover { color: var(--ink); }
  .go :global(svg) { width: 12px; height: 12px; }
  .back { display: flex; justify-content: center; margin-top: 16px; }

  @media (max-width: 1100px) {
    .squad { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .plays, .ops { grid-template-columns: 1fr; }
  }
  @media (max-width: 560px) { .squad { grid-template-columns: 1fr; } h1 { font-size: var(--t-h1); } }
</style>
