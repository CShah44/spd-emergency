<script>
  // Which shard holds a record. Amber "!" when that shard runs on one node, red when it has none,
  // so a failure shows on every record it touches.
  let { shard, health = 'ok' } = $props();
  const TIP = {
    ok: (s) => `Stored on s${s}-primary and s${s}-replica`,
    degraded: (s) => `Shard ${s} is running on one node; the other will catch up`,
    down: (s) => `Shard ${s} has no live node: this record cannot be read or changed`,
  };
</script>

<span class="chip {health}" style="--sc:var(--shard-{shard % 3})" title={TIP[health](shard)}>
  S{shard}{#if health !== 'ok'}<b>!</b>{/if}
</span>

<style>
  .chip {
    display: inline-flex; align-items: center; gap: 2px;
    height: 20px; padding: 0 6px; border-radius: 4px;
    border: 1px solid color-mix(in srgb, var(--sc) 45%, transparent);
    color: var(--sc); font-family: var(--f-mono); font-size: 11px; font-weight: 600;
  }
  .degraded { --sc: var(--orange); }
  .down { --sc: var(--red); background: rgba(255, 51, 71, 0.12); }
  b { font-weight: 700; }
</style>
