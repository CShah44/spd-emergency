<script>
  // The storage cluster: three shards, a primary and a replica each, and the demo's failure controls.
  import { Crown, Pause, Play, LoaderCircle, Check } from '@lucide/svelte';
  import Panel from './Panel.svelte';
  import { live, act } from './live.svelte.js';

  const cluster = $derived(live.snap?.cluster);
  const health = $derived(live.snap?.shard_health ?? {});
  let pending = $state({});

  async function toggle(node, pause) {
    pending[node.id] = true;
    await act('POST', `/api/nodes/${node.id}/${pause ? 'pause' : 'resume'}`);
    pending[node.id] = false;
  }

  function nodeState(n) {
    if (n.silent) return { label: 'Not answering', cls: 's-RECOVERING' };
    return { label: n.state, cls: `s-${n.state}` };
  }

  const HEALTH = {
    ok: { label: 'Two copies', cls: 's-UP' },
    degraded: { label: 'One node', cls: 's-UNMATCHED' },
    down: { label: 'No live node', cls: 's-DOWN' },
  };

  function sync(shard) {
    const [a, b] = shard.nodes;
    if (!a || !b || a.keys == null || b.keys == null) return null;
    return a.keys - b.keys;
  }
</script>

<Panel id="storage" owner="storage" title="Storage vault" topic="pause"
  sub={cluster?.total_keys != null ? `${cluster.total_keys.toLocaleString()} records, each on exactly two nodes` : 'Three shards · a primary and a replica each'}>
  {#snippet info()}
    <p>Each record belongs to one shard: <b>sha1(key) mod 3</b>. Both nodes of that shard hold a copy; the
      <b>crown</b> marks the node the coordinator sends reads and writes to first.</p>
    <p><b>Pause</b> makes a node stop answering, like a crash. Its partner keeps the shard running. <b>Resume</b>
      brings it back: it copies what it missed, then turns UP.</p>
  {/snippet}

  {#if !cluster}
    <div class="empty">Waiting for the coordinator…</div>
  {:else}
    <div class="shards">
      {#each cluster.shards as shard (shard.shard)}
        {@const h = HEALTH[health[shard.shard] ?? 'ok']}
        {@const delta = sync(shard)}
        <div class="shard" style="--sc:var(--shard-{shard.shard % 3})">
          <div class="shard-head">
            <span class="shard-name"><i></i>Shard {shard.shard}</span>
            <span class="pill {h.cls}">{h.label}</span>
          </div>
          {#each shard.nodes as node (node.id)}
            {@const st = nodeState(node)}
            <div class="node {st.cls}" class:leader={node.leader} class:down={node.state === 'DOWN' || node.silent}>
              <div class="node-top">
                <span class="node-id mono">{node.id}</span>
                {#if node.leader}<span class="crown" title="Leads this shard"><Crown /></span>{/if}
                <span class="pill {st.cls}">{st.label}</span>
              </div>
              <div class="node-mid">
                <span class="role">{node.role}{node.leader ? ' · leader' : ''}</span>
                <span class="keys num">{node.keys == null ? '—' : node.keys.toLocaleString()}<small> keys</small></span>
              </div>
              {#if node.recovery}<p class="recovery">{node.recovery}</p>{/if}
              {#if cluster.controls}
                {#if pending[node.id]}
                  <button class="btn sm block" disabled><LoaderCircle class="spin" />Working…</button>
                {:else if node.state === 'UP' && !node.silent}
                  <button class="btn sm block crash" onclick={() => toggle(node, true)}>
                    <Pause />Pause · simulate a crash</button>
                {:else if node.state === 'DOWN' || node.silent}
                  <button class="btn sm block tone" style="--tone:var(--green)" onclick={() => toggle(node, false)}>
                    <Play />Resume</button>
                {:else}
                  <button class="btn sm block" disabled><LoaderCircle class="spin" />Catching up…</button>
                {/if}
              {/if}
            </div>
          {/each}
          <p class="sync">
            {#if delta === 0}<Check />Copies in sync
            {:else if delta != null}{Math.abs(delta)} record{Math.abs(delta) === 1 ? '' : 's'} apart: the other node catches up
            {:else}&nbsp;{/if}
          </p>
        </div>
      {/each}
    </div>
    {#if !cluster.controls}
      <p class="note">Pause and Resume are off (DEMO_CONTROLS is not set on the request service).</p>
    {/if}
  {/if}
</Panel>

<style>
  .shards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .shard { display: grid; gap: 8px; align-content: start; }
  .shard-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 2px 2px; }
  .shard-name { display: inline-flex; align-items: center; gap: 8px; font-family: var(--f-display);
    font-weight: 600; font-size: 13px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink); }
  .shard-name i { width: 10px; height: 10px; border-radius: 2px; background: var(--sc); transform: rotate(45deg); }

  .node {
    display: grid; gap: 8px; padding: 12px;
    border: 1px solid var(--seam-2); border-radius: var(--r-2); background: var(--hull-2);
    transition: border-color 200ms var(--ease-out), background-color 200ms var(--ease-out);
  }
  .node.leader { border-color: rgba(255, 198, 26, 0.35); }
  .node.down { border-color: rgba(255, 51, 71, 0.45); background: color-mix(in srgb, var(--red) 6%, var(--hull-2));
    background-image: repeating-linear-gradient(-45deg, transparent 0 10px, rgba(255, 51, 71, 0.05) 10px 20px); }
  .node-top { display: flex; align-items: center; gap: 6px; }
  .node-id { flex: 1; font-weight: 600; color: var(--ink); font-size: 13px; }
  .crown { color: var(--yellow); display: grid; }
  .crown :global(svg) { width: 15px; height: 15px; }
  .node-mid { display: flex; align-items: baseline; justify-content: space-between; }
  .role { font-size: 12px; color: var(--ink-3); text-transform: capitalize; }
  .keys { font-family: var(--f-display); font-size: var(--t-h3); font-weight: 600; color: var(--ink); }
  .keys small { font-family: var(--f-body); font-size: 11px; font-weight: 400; color: var(--ink-3); }
  .recovery { font-size: 11.5px; color: var(--ink-3); line-height: 1.4; }
  .sync { display: flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--ink-3); min-height: 18px; }
  .sync :global(svg) { width: 13px; height: 13px; color: var(--green); }
  /* six of these sit side by side: quiet until reached for */
  .crash { background: transparent; border-color: var(--seam-2); color: var(--ink-3); }
  .crash :global(svg) { color: var(--red); }
  .crash:hover { color: var(--red); border-color: rgba(255, 51, 71, 0.5); background: rgba(255, 51, 71, 0.08); }
  .note, .empty { font-size: 12px; color: var(--ink-3); }
  .note { margin-top: 12px; }
  .empty { padding: 24px 0; text-align: center; }
  :global(.spin) { animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 760px) { .shards { grid-template-columns: 1fr; } }
</style>
