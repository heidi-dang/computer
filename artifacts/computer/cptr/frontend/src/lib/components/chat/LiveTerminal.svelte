<script lang="ts">
	import FlowDeckAuditReport from './FlowDeckAuditReport.svelte';
	import Icon from '../Icon.svelte';

interface Props {
events?: any[];
status?: string;
runId?: string;
isAudit?: boolean;
onretry?: (command: string) => void;
}

let { events = [], status = '', runId = '', isAudit = false, onretry }: Props = $props();
let open = $state(true);
let follow = $state(true);
let auditReportOpen = $state(false);
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

type TerminalInterruption = 'timed_out' | 'cancelled';

function interruptionFor(event: any): TerminalInterruption | null {
const frame = terminalFrameFor(event);
const kind = String(frame?.frame_kind || event?.kind || event?.type || '').toLowerCase();
const payload = frame?.payload || event?.payload || {};
const reportedStatus = String(payload.status || event?.status || '').toLowerCase();
if (reportedStatus === 'timed_out' || kind.includes('timed_out') || kind.includes('timeout')) {
return 'timed_out';
}
if (
reportedStatus === 'cancelled' ||
reportedStatus === 'canceled' ||
kind.includes('cancelled') ||
kind.includes('canceled')
) {
return 'cancelled';
}
return null;
}

function eventKey(event: any, index: number) {
const frame = terminalFrameFor(event);
return String(
event?.id ||
event?.event_id ||
  `${frame?.sequence ?? event?.sequence ?? index}:${frame?.frame_kind || event?.kind || event?.type || ''}:${event?.run_id || frame?.terminal_run_id || ''}:${event?.payload?.step_id || ''}`
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

function terminalFrameFor(event: any) {
const nested = event?.payload?.kind === 'terminal_frame' ? event.payload : null;
if (nested) return nested;
if (event?.kind === 'terminal_frame' && event?.frame_kind) return event;
return null;
}

function isTerminalEvent(event: any) {
const frame = terminalFrameFor(event);
if (frame) {
return [
'command_start',
'command_output',
'command_exit',
'action_start',
'action_output',
'action_exit'
].includes(frame.frame_kind);
}
return String(event?.kind || event?.type || '').startsWith('AGENT_TERMINAL_');
}

function isCommandExitEvent(event: any) {
const frame = terminalFrameFor(event);
if (frame) return frame.frame_kind === 'command_exit';
return String(event?.kind || event?.type || '').toUpperCase().startsWith('AGENT_TERMINAL_COMMAND_');
}

function retryCommandFor(event: any): string | null {
const interruption = interruptionFor(event);
if (!interruption) return null;
const frame = terminalFrameFor(event);
const payload = frame?.payload || event?.payload || {};
if (payload.session_discarded !== true || payload.next_command_starts_fresh !== true) return null;
const command = firstValue(payload.command, event?.command);
if (!command) return null;
const value = String(command).trim();
// A redacted command is not the original command and must not be replayed.
if (!value || value.includes('[REDACTED]')) return null;
return value;
}

function eventLine(event: any, index: number) {
const frame = terminalFrameFor(event);
const kind = String(frame?.frame_kind || event?.kind || event?.type || 'activity').replaceAll('_', ' ').toLowerCase();
const payload = frame?.payload || event?.payload || {};
const item = event?.output || {};
const call = item?.type === 'function_call' ? item : null;
const output = item?.type === 'function_call_output' ? item : null;
const safeOutputSummary = firstValue(
item?.type === 'reasoning' ? item?.summary : undefined,
item?.type === 'reasoning' ? item?.status : undefined,
item?.type === 'message' ? item?.summary : undefined
);
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
payload.text,
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
event?.status,
safeOutputSummary
);
const identities = [
firstValue(payload.specialist_id, payload.child_agent_id),
firstValue(payload.attempt_id, event?.attempt_id),
firstValue(payload.step_id, event?.step_id),
firstValue(payload.session_id, payload.terminal_id)
].filter(Boolean);
const identity = identities.join(' · ');
let title = tool ? `tool · ${tool}` : path ? `file · ${path}` : kind;
if (command) title = `shell · ${command}`;
if (output) title = `output · ${tool || 'tool result'}`;
if (frame?.frame_kind === 'command_start') title = 'shell · command started';
if (frame?.frame_kind === 'command_output') title = `shell · ${payload.stream || 'output'}`;
if (frame?.frame_kind === 'command_exit') title = `shell · exited (${payload.exit_code ?? 'unknown'})`;
if (frame?.frame_kind === 'action_start') title = `action · ${payload.tool_name || 'started'}`;
if (frame?.frame_kind === 'action_output') title = `action · ${payload.tool_name || 'output'}`;
if (frame?.frame_kind === 'action_exit') title = `action · ${payload.tool_name || 'completed'}`;
const interruption = interruptionFor(event);
if (interruption === 'timed_out') title = 'terminal · command timed out';
if (interruption === 'cancelled') title = 'terminal · command cancelled';
if (event?.delta) title = 'agent update · native transcript activity';
if (item?.type === 'reasoning') title = `agent activity · ${safeOutputSummary || 'safe summary'}`;
if (item?.type === 'message') title = `agent update · ${safeOutputSummary || 'native transcript activity'}`;
if (kind.includes('validation')) title = `validation · ${safeSummary || kind}`;
if (kind.includes('verif') || kind.includes('review')) title = `verification · ${safeSummary || kind}`;
if (kind.includes('run ') || kind.startsWith('run')) title = `lifecycle · ${kind}`;
return {
key: eventKey(event, index),
sequence: event?.sequence ?? index + 1,
title,
detail:
interruption === 'timed_out' || interruption === 'cancelled'
? 'PTY discarded · the next command starts fresh'
 : frame?.frame_kind === 'command_start' && command
 ? command
: stream
? stringify(stream)
: safeSummary || identity || '',
identity,
isError:
interruption === 'timed_out' ||
Boolean(payload.stderr) ||
payload.status === 'failed' ||
/failed|error|rejected/i.test(`${kind} ${safeSummary || ''}`),
isLifecycle: Boolean(frame) || Boolean(interruption) || (!tool && !command && !path && !output),
interruption
};
}

const uniqueEvents = $derived.by(() => {
const seen = new Map<string, any>();
events.forEach((event, index) => seen.set(eventKey(event, index), event));
return [...seen.values()].sort((a, b) => {
const left = Number(a?.sequence);
const right = Number(b?.sequence);
if (Number.isFinite(left) && Number.isFinite(right)) return left - right;
return 0;
});
});

const terminalEvents = $derived(uniqueEvents.filter(isTerminalEvent));

const latestInterruption = $derived.by(() => {
for (const event of [...terminalEvents].reverse()) {
if (!isCommandExitEvent(event)) continue;
return interruptionFor(event);
}
return null;
});

const retryCommand = $derived.by(() => {
for (const event of [...terminalEvents].reverse()) {
if (!isCommandExitEvent(event)) continue;
return retryCommandFor(event);
}
return null;
});

const statusLabel = $derived(
latestInterruption === 'timed_out'
? 'command timed out'
: latestInterruption === 'cancelled'
? 'command cancelled'
: normalizedStatus === 'manual_review_required' || normalizedStatus === 'manual_review'
? 'manual review'
: normalizedStatus === 'unknown'
? 'reconnecting'
: normalizedStatus
);

const lines = $derived(
terminalEvents
.map(eventLine)
.filter((line) => line.title || line.detail)
);
const hasActivity = $derived(uniqueEvents.length > 0);
const hasAuditAnalysis = $derived(
isAudit && uniqueEvents.some((event) => event?.kind === 'AUDIT_ANALYSIS_CREATED')
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
<div class="terminal-name">Heidi Live Terminal</div>
<div class="terminal-subtitle">
<span class="terminal-phase">{statusLabel}</span>
{#if runId}<span class="terminal-id" title={runId}>run {runId.slice(0, 10)}</span>{/if}
</div>
</div>
</div>
<div class="terminal-actions">
{#if !isTerminal}
<span class="terminal-live">LIVE</span>
{/if}
{#if hasAuditAnalysis}
<button
type="button"
class="terminal-audit-button"
class:is-selected={auditReportOpen}
onclick={() => (auditReportOpen = !auditReportOpen)}
aria-expanded={auditReportOpen}
aria-label={auditReportOpen ? 'Hide audit report' : 'Show audit report'}
title={auditReportOpen ? 'Hide audit report' : 'Show audit report'}
>
<Icon name="page-text" size={12} />
</button>
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

{#if latestInterruption}
<div
class="terminal-interruption"
class:is-cancelled={latestInterruption === 'cancelled'}
role="alert"
data-testid={`heidi-terminal-${latestInterruption}`}
>
<strong>{latestInterruption === 'timed_out' ? 'Command timed out' : 'Command cancelled'}</strong>
<span>
{latestInterruption === 'timed_out'
? 'The command exceeded its time limit. The PTY was discarded; the next command starts fresh.'
: 'The command was intentionally stopped. The PTY was discarded; the next command starts fresh.'}
</span>
{#if retryCommand && onretry}
<button
type="button"
class="terminal-retry"
data-testid="heidi-terminal-retry"
onclick={() => onretry?.(retryCommand)}
>
Retry command
</button>
{/if}
</div>
{/if}

{#if open}
<div class="terminal-output" role="log" aria-live="polite" bind:this={outputEl} onscroll={() => {
if (outputEl && outputEl.scrollHeight - outputEl.scrollTop - outputEl.clientHeight > 24) follow = false;
}}>
{#if !lines.length && normalizedStatus === 'preparing'}
<div class="terminal-empty"><span class="prompt-mark">$</span> starting Heidi run…</div>
{:else if !lines.length}
<div class="terminal-empty">
<span class="prompt-mark">$</span>
{hasActivity
? 'read-only planning is active; terminal output starts with the authorized coding step…'
: 'waiting for authenticated FlowDeck activity…'}
</div>
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
{#if hasAuditAnalysis}
<FlowDeckAuditReport
events={uniqueEvents}
open={auditReportOpen}
ontoggle={() => (auditReportOpen = !auditReportOpen)}
/>
{/if}
</section>

<style>
.live-terminal {
margin: 0 0 .7rem;
overflow: hidden;
border: 1px solid color-mix(in oklab, #22d3ee 28%, transparent);
border-radius: 1rem;
background:
linear-gradient(145deg, color-mix(in oklab, #10232a 72%, #080d12), #080d12 55%);
color: #d5e4e8;
box-shadow:
0 -12px 30px color-mix(in oklab, #020617 16%, transparent),
inset 0 1px 0 color-mix(in oklab, #67e8f9 10%, transparent);
}
.terminal-header {
display: flex;
align-items: center;
justify-content: space-between;
gap: .65rem;
min-height: 2.85rem;
padding: .55rem .75rem;
border-bottom: 1px solid color-mix(in oklab, #94a3b8 14%, transparent);
background: linear-gradient(90deg, color-mix(in oklab, #12303a 55%, transparent), #0a1117);
}
.terminal-title, .terminal-actions { display: flex; align-items: center; gap: .5rem; min-width: 0; }
.terminal-dot { width: .5rem; height: .5rem; flex: 0 0 auto; border-radius: 999px; background: #64748b; }
.terminal-pulse { background: #22d3ee; box-shadow: 0 0 0 4px color-mix(in oklab, #22d3ee 12%, transparent); animation: terminal-pulse 1.7s ease-in-out infinite; }
.terminal-done { background: #34d399; }
.terminal-name { color: #f8fafc; font: 650 .69rem ui-sans-serif, system-ui, sans-serif; letter-spacing: .07em; text-transform: uppercase; }
.terminal-subtitle { display: flex; align-items: center; gap: .35rem; color: #7f9ba4; font: .61rem ui-monospace, SFMono-Regular, monospace; text-transform: capitalize; }
.terminal-phase { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.terminal-id { margin-left: .15rem; color: #5d7881; }
.terminal-live { color: #67e8f9; font: 750 .56rem ui-monospace, SFMono-Regular, monospace; letter-spacing: .12em; }
.terminal-audit-button {
display: grid;
width: 1.3rem;
height: 1.3rem;
place-items: center;
border: 1px solid color-mix(in oklab, #a78bfa 34%, transparent);
border-radius: .35rem;
color: #c4b5fd;
}
.terminal-audit-button:hover, .terminal-audit-button.is-selected {
background: color-mix(in oklab, #a78bfa 16%, transparent);
border-color: color-mix(in oklab, #a78bfa 62%, transparent);
color: #ede9fe;
}
.terminal-action { border: 1px solid color-mix(in oklab, #94a3b8 12%, transparent); border-radius: .45rem; padding: .28rem .45rem; color: #8eaab2; font: .62rem ui-sans-serif, system-ui, sans-serif; }
.terminal-action:hover, .terminal-action[aria-pressed="true"] { border-color: #24505b; background: #10252c; color: #c7f9ff; }
.terminal-output { max-height: 14rem; overflow: auto; padding: .65rem .75rem .75rem; scrollbar-color: #27434b transparent; }
.terminal-line { display: flex; gap: .65rem; min-width: 0; padding: .24rem 0; font: .69rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
.line-sequence { flex: 0 0 1.8rem; color: #45616a; text-align: right; user-select: none; }
.line-content { min-width: 0; flex: 1; }
.line-title { display: inline; color: #a7d6dc; overflow-wrap: anywhere; }
.line-identity { margin-left: .55rem; color: #718b93; font-size: .6rem; }
.line-detail { display: block; max-height: 5.5rem; overflow: auto; margin-top: .18rem; color: #8ca7ad; white-space: pre-wrap; overflow-wrap: anywhere; }
.error-line .line-title { color: #fca5a5; }
.error-line .line-detail { color: #fda4af; }
.lifecycle-line .line-title { color: #c4b5fd; }
.terminal-interruption {
display: flex;
flex-direction: column;
gap: .2rem;
margin: .65rem .75rem .1rem;
padding: .65rem .7rem;
border: 1px solid color-mix(in oklab, #fb7185 48%, transparent);
border-radius: .6rem;
background: color-mix(in oklab, #4c0519 48%, transparent);
color: #fecdd3;
font: .67rem/1.4 ui-sans-serif, system-ui, sans-serif;
}
.terminal-interruption strong {
color: #fda4af;
font-weight: 750;
letter-spacing: .02em;
}
.terminal-interruption.is-cancelled {
border-color: color-mix(in oklab, #fbbf24 44%, transparent);
background: color-mix(in oklab, #451a03 42%, transparent);
color: #fde68a;
}
.terminal-interruption.is-cancelled strong { color: #fcd34d; }
.terminal-retry {
align-self: flex-start;
margin-top: .25rem;
border: 1px solid color-mix(in oklab, #fda4af 48%, transparent);
border-radius: .45rem;
padding: .28rem .5rem;
background: color-mix(in oklab, #881337 32%, transparent);
color: #ffe4e6;
font: 700 .63rem ui-sans-serif, system-ui, sans-serif;
}
.terminal-retry:hover {
border-color: #fda4af;
background: color-mix(in oklab, #be123c 38%, transparent);
color: #fff1f2;
}
.terminal-empty { padding: 1.25rem .25rem; color: #71909a; font: .7rem ui-monospace, SFMono-Regular, monospace; }
.prompt-mark { margin-right: .5rem; color: #34d399; }
@keyframes terminal-pulse { 50% { opacity: .5; box-shadow: 0 0 0 6px color-mix(in oklab, #22d3ee 0%, transparent); } }
@media (max-width: 640px) {
.live-terminal { margin-bottom: .55rem; border-radius: .85rem; }
.terminal-header { align-items: flex-start; gap: .45rem; padding: .5rem .6rem; }
.terminal-title { gap: .4rem; }
.terminal-actions { flex-wrap: wrap; justify-content: flex-end; gap: .2rem; }
.terminal-collapse { max-width: 4.8rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.terminal-output { max-height: 12rem; padding: .5rem .55rem .6rem; }
.terminal-line { gap: .4rem; font-size: .62rem; }
.line-sequence { flex-basis: 1.5rem; }
}
@media (prefers-reduced-motion: reduce) { .terminal-pulse { animation: none; } }
</style>