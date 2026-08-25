<script lang="ts">
interface Props {
events?: any[];
status?: string;
runId?: string;
}

let { events = [], status = '', runId = '' }: Props = $props();
let open = $state(true);
let follow = $state(true);
let outputEl: HTMLDivElement;

const terminalStatuses = new Set([
'succeeded',
'completed',
'failed',
'cancelled',
'unknown',
'manual_review',
'manual_review_required',
'orphaned'
]);

const normalizedStatus = $derived(String(status || 'queued').toLowerCase().replaceAll('-', '_'));
const isTerminal = $derived(terminalStatuses.has(normalizedStatus));
const statusLabel = $derived(
normalizedStatus === 'manual_review_required' || normalizedStatus === 'manual_review'
? 'manual review'
: normalizedStatus === 'unknown'
? 'reconnecting'
: normalizedStatus
);

function eventKey(event: any, index: number) {
return String(
event?.id ||
event?.event_id ||
`${event?.sequence ?? index}:${event?.kind || event?.type || ''}:${event?.run_id || ''}:${event?.payload?.step_id || ''}`
);
}

function stringify(value: unknown) {
if (value == null) return '';
if (typeof value === 'string') return value;
try {
return JSON.stringify(value, null, 2);
} catch {
return String(value);
}
}

function firstValue(...values: unknown[]) {
return values.find((value) => value !== undefined && value !== null && String(value).trim()) as
| string
| undefined;
}

function eventLine(event: any, index: number) {
const kind = String(event?.kind || event?.type || 'activity').replaceAll('_', ' ').toLowerCase();
const payload = event?.payload || {};
const item = event?.output || {};
const call = item?.type === 'function_call' ? item : null;
const output = item?.type === 'function_call_output' ? item : null;
const tool = firstValue(call?.name, call?.tool_name, payload.tool_name, payload.tool);
const command = firstValue(
call?.arguments?.command,
call?.args?.command,
payload.command,
payload.shell_command
);
const path = firstValue(
call?.arguments?.path,
call?.args?.path,
payload.path,
payload.file,
payload.file_path
);
const stream = firstValue(
output?.output,
payload.stdout,
payload.stderr,
payload.output,
payload.result
);
const safeSummary = firstValue(
payload.summary,
payload.observation,
payload.message,
payload.status,
event?.status
);
const identity = firstValue(
payload.specialist_id,
payload.child_agent_id,
payload.attempt_id,
event?.attempt_id
);
let title = tool ? `tool · ${tool}` : path ? `file · ${path}` : kind;
if (command) title = `shell · ${command}`;
if (output) title = `output · ${tool || 'tool result'}`;
if (kind.includes('validation')) title = `validation · ${safeSummary || kind}`;
if (kind.includes('verif') || kind.includes('review')) title = `verification · ${safeSummary || kind}`;
if (kind.includes('run ') || kind.startsWith('run')) title = `lifecycle · ${kind}`;
return {
key: eventKey(event, index),
sequence: event?.sequence ?? index + 1,
title,
detail: stream ? stringify(stream) : safeSummary || identity || '',
identity,
isError: Boolean(payload.stderr) || /failed|error|rejected/i.test(`${kind} ${safeSummary || ''}`),
isLifecycle: !tool && !command && !path && !output
};
}

const lines = $derived(
events
.map(eventLine)
.filter((line) => line.title || line.detail)
);

$effect(() => {
if (open && follow && outputEl) {
outputEl.scrollTop = outputEl.scrollHeight;
}
});

function toggleFollow() {
follow = !follow;
if (follow && outputEl) outputEl.scrollTop = outputEl.scrollHeight;
}
</script>

<section class="live-terminal" data-testid="heidi-live-terminal" aria-label="Heidi live terminal">
<header class="terminal-header">
<div class="terminal-title">
<span class:terminal-pulse={!isTerminal} class:terminal-done={isTerminal} class="terminal-dot" aria-hidden="true"></span>
<div class="min-w-0">
<div class="terminal-name">Live terminal</div>
<div class="terminal-subtitle">
{statusLabel}
{#if runId}<span class="terminal-id" title={runId}>run {runId.slice(0, 10)}</span>{/if}
</div>
</div>
</div>
<div class="terminal-actions">
{#if !isTerminal}
<span class="terminal-live">LIVE</span>
{/if}
<button type="button" class="terminal-action" onclick={toggleFollow} aria-pressed={follow}>
{follow ? 'Pause' : 'Resume'}
</button>
<button
type="button"
class="terminal-action terminal-collapse"
onclick={() => (open = !open)}
aria-expanded={open}
>
{open ? 'Collapse' : 'Expand'}
</button>
</div>
</header>

{#if open}
<div class="terminal-output" bind:this={outputEl} onscroll={() => {
if (outputEl && outputEl.scrollHeight - outputEl.scrollTop - outputEl.clientHeight > 24) follow = false;
}} tabindex="0">
{#if !lines.length}
<div class="terminal-empty"><span class="prompt-mark">$</span> waiting for authenticated FlowDeck activity…</div>
{:else}
{#each lines as line (line.key)}
<div class:error-line={line.isError} class:lifecycle-line={line.isLifecycle} class="terminal-line">
<span class="line-sequence">{String(line.sequence).padStart(3, '0')}</span>
<span class="line-content">
<span class="line-title">{line.title}</span>
{#if line.identity}<span class="line-identity">{line.identity}</span>{/if}
{#if line.detail}<code class="line-detail">{line.detail}</code>{/if}
</span>
</div>
{/each}
{/if}
</div>
{/if}
</section>

<style>
.live-terminal {
margin: 0 0 .65rem;
overflow: hidden;
border: 1px solid color-mix(in oklab, #22d3ee 24%, transparent);
border-radius: .85rem;
background: #080d12;
color: #d5e4e8;
box-shadow: 0 -10px 28px color-mix(in oklab, #020617 12%, transparent);
}
.terminal-header {
display: flex;
align-items: center;
justify-content: space-between;
gap: .65rem;
min-height: 2.65rem;
padding: .5rem .7rem;
border-bottom: 1px solid color-mix(in oklab, #94a3b8 14%, transparent);
background: linear-gradient(90deg, #0b151b, #0a1117);
}
.terminal-title, .terminal-actions { display: flex; align-items: center; gap: .5rem; min-width: 0; }
.terminal-dot { width: .45rem; height: .45rem; flex: 0 0 auto; border-radius: 999px; background: #64748b; }
.terminal-pulse { background: #22d3ee; box-shadow: 0 0 0 4px color-mix(in oklab, #22d3ee 12%, transparent); animation: terminal-pulse 1.7s ease-in-out infinite; }
.terminal-done { background: #34d399; }
.terminal-name { color: #f8fafc; font: 600 .7rem ui-sans-serif, system-ui, sans-serif; letter-spacing: .04em; text-transform: uppercase; }
.terminal-subtitle { color: #7f9ba4; font: .62rem ui-monospace, SFMono-Regular, monospace; text-transform: capitalize; }
.terminal-id { margin-left: .5rem; color: #5d7881; }
.terminal-live { color: #67e8f9; font: 700 .56rem ui-monospace, SFMono-Regular, monospace; letter-spacing: .12em; }
.terminal-action { border: 1px solid transparent; border-radius: .35rem; padding: .25rem .4rem; color: #8eaab2; font: .62rem ui-sans-serif, system-ui, sans-serif; }
.terminal-action:hover, .terminal-action[aria-pressed="true"] { border-color: #24505b; background: #10252c; color: #c7f9ff; }
.terminal-output { max-height: 14rem; overflow: auto; padding: .6rem .7rem .7rem; scrollbar-color: #27434b transparent; }
.terminal-line { display: flex; gap: .65rem; min-width: 0; padding: .24rem 0; font: .69rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
.line-sequence { flex: 0 0 1.8rem; color: #45616a; text-align: right; user-select: none; }
.line-content { min-width: 0; flex: 1; }
.line-title { display: inline; color: #a7d6dc; overflow-wrap: anywhere; }
.line-identity { margin-left: .55rem; color: #718b93; font-size: .6rem; }
.line-detail { display: block; max-height: 5.5rem; overflow: auto; margin-top: .18rem; color: #8ca7ad; white-space: pre-wrap; overflow-wrap: anywhere; }
.error-line .line-title { color: #fca5a5; }
.error-line .line-detail { color: #fda4af; }
.lifecycle-line .line-title { color: #c4b5fd; }
.terminal-empty { padding: 1.25rem .25rem; color: #71909a; font: .7rem ui-monospace, SFMono-Regular, monospace; }
.prompt-mark { margin-right: .5rem; color: #34d399; }
@keyframes terminal-pulse { 50% { opacity: .5; box-shadow: 0 0 0 6px color-mix(in oklab, #22d3ee 0%, transparent); } }
@media (max-width: 640px) {
.terminal-header { align-items: flex-start; }
.terminal-actions { flex-wrap: wrap; justify-content: flex-end; gap: .25rem; }
.terminal-collapse { max-width: 4.8rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.terminal-output { max-height: 12rem; padding-inline: .5rem; }
.terminal-line { gap: .4rem; font-size: .62rem; }
.line-sequence { flex-basis: 1.5rem; }
}
@media (prefers-reduced-motion: reduce) { .terminal-pulse { animation: none; } }
</style>