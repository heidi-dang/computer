<script lang="ts">
import { onDestroy } from 'svelte';
import {
getManagedRuntime,
startManagedRuntime,
stopManagedRuntime,
type ManagedRuntime
} from '$lib/apis/runtime';

interface Props { workspace: string; }
let { workspace }: Props = $props();
let runtime = $state<ManagedRuntime | null>(null);
let error = $state('');
let loading = $state(false);
let timer: ReturnType<typeof setInterval> | null = null;

function beginPolling() {
if (timer) clearInterval(timer);
timer = setInterval(async () => {
if (!runtime?.run_id) return;
try {
runtime = await getManagedRuntime(runtime.run_id, workspace);
if (['running', 'crashed', 'unknown', 'stopped'].includes(runtime.state) && timer) {
clearInterval(timer); timer = null;
}
} catch (e) { error = e instanceof Error ? e.message : 'Preview state unavailable'; }
}, 500);
}

async function start() {
if (!workspace) return;
loading = true; error = '';
try {
runtime = await startManagedRuntime(workspace, `runtime-${crypto.randomUUID()}`);
beginPolling();
} catch (e) { error = e instanceof Error ? e.message : 'Unable to start preview'; }
finally { loading = false; }
}

async function stop() {
if (!runtime?.run_id) return;
try { runtime = await stopManagedRuntime(runtime.run_id, workspace); }
catch (e) { error = e instanceof Error ? e.message : 'Unable to stop preview'; }
}

onDestroy(() => { if (timer) clearInterval(timer); });
</script>

<section class="runtime-panel" aria-labelledby="runtime-title">
<div class="runtime-heading">
<div><span class="eyebrow">MANAGED PREVIEW</span><h2 id="runtime-title">Project runtime</h2></div>
<span class:live={runtime?.state === 'running'} class="state">{runtime?.state ?? 'idle'}</span>
</div>
<p>Start the discovered project command in a bounded, workspace-scoped process.</p>
<div class="runtime-actions">
<button type="button" onclick={start} disabled={loading || !workspace || runtime?.state === 'running'}>{loading ? 'Starting…' : 'Start preview'}</button>
{#if runtime?.run_id}<button type="button" class="secondary" onclick={stop}>Stop</button>{/if}
</div>
{#if runtime?.state === 'running' && runtime.preview_url}
<iframe title="Managed project preview" src={runtime.preview_url} class="preview-frame"></iframe>
{:else if runtime?.state === 'unknown'}
<div role="status" class="notice">Runtime state is unknown after reconnect. No healthy preview is claimed.</div>
{:else if runtime?.state === 'crashed'}
<div role="alert" class="notice error">The managed process crashed. Review runtime logs before restarting.</div>
{/if}
{#if runtime?.logs}<details><summary>Runtime logs</summary><pre>{runtime.logs}</pre></details>{/if}
{#if error}<div role="alert" class="error">{error}</div>{/if}
</section>

<style>
.runtime-panel{margin-top:2rem;padding:1.25rem;border:1px solid #30343c;border-radius:1rem;background:#111318;color:#e7e9ed}
.runtime-heading{display:flex;justify-content:space-between;align-items:start}.eyebrow{font:600 .65rem ui-monospace;color:#8f98a8;letter-spacing:.12em}
h2{margin:.35rem 0;font-size:1.25rem}.state{text-transform:uppercase;font:600 .7rem ui-monospace;color:#d49d55}.state.live{color:#77c69a}
p{color:#9ea7b5}.runtime-actions{display:flex;gap:.6rem}button{border:0;border-radius:.55rem;padding:.55rem .8rem;background:#e8edf4;color:#111318;font-weight:600}button.secondary{background:#252a33;color:#e7e9ed}.preview-frame{display:block;width:100%;height:360px;margin-top:1rem;border:1px solid #30343c;border-radius:.7rem;background:#fff}details{margin-top:1rem}pre{max-height:160px;overflow:auto;color:#b7c1cf}.notice{margin-top:1rem;padding:.75rem;border-radius:.5rem;background:#272b34}.error{color:#ff9d9d}
</style>