<script lang="ts">
	import DesignerResults, { type DesignerAction } from './DesignerResults.svelte';
import { downloadFlowDeckEvidenceReport } from '$lib/apis/flowdeck';

	interface Props {
		status?: string;
		runId?: string;
workspace?: string;
		nativeMessageId?: string | null;
		sending?: boolean;
		isAudit?: boolean;
		events?: any[];
evidenceSummary?: {
run_id?: string;
entries?: any[];
total?: number;
truncated?: boolean;
} | null;
	preservedEvidenceSummary?: {
		run_id?: string;
		entries?: any[];
		total?: number;
		truncated?: boolean;
	} | null;
		oncancel?: () => void;
		onreconnect?: () => void;
		onnewrun?: () => void;
		onaction?: (action: DesignerAction) => void;
		telemetry?: {
			input_tokens: number;
			output_tokens: number;
			total_tokens: number;
			cost: number | null;
		};
	}

	let {
		status = '',
		runId = '',
workspace = '',
		nativeMessageId = '',
		sending = false,
		isAudit = false,
		events = [],
evidenceSummary = null,
		preservedEvidenceSummary = null,
		oncancel,
		onreconnect,
		onnewrun,
		onaction,
		telemetry = { input_tokens: 0, output_tokens: 0, total_tokens: 0, cost: null }
	}: Props = $props();

	const terminalStatuses = new Set([
		'cancelled',
		'succeeded',
		'completed',
		'failed',
		'unknown',
		'manual_review',
		'manual_review_required',
		'orphaned'
	]);

	const isTerminal = $derived(terminalStatuses.has(status.toLowerCase()));
	const isActive = $derived(
		!isTerminal && ['preparing', 'active', 'planning', 'verifying'].includes(status.toLowerCase())
	);
const normalizedStatus = $derived(status.toLowerCase());
const needsRecovery = $derived(
['orphaned', 'unknown', 'manual_review', 'manual_review_required'].includes(normalizedStatus)
);
const latestTerminalReason = $derived.by(() => {
const terminalEvent = [...events].reverse().find((event) =>
['RUN_ORPHANED', 'RUN_UNKNOWN', 'RUN_MANUAL_REVIEW'].includes(
String(event?.kind || event?.type || '').toUpperCase()
)
);
const reason = terminalEvent?.payload?.reason || terminalEvent?.reason;
return typeof reason === 'string' && reason.trim() ? reason : '';
});
	const recoveryMessage = $derived(
normalizedStatus === 'manual_review_required' || normalizedStatus === 'manual_review'
? `This run requires review before it can be resumed. ${latestTerminalReason || 'Its outcome was interrupted before completion could be verified.'} No completion was claimed.`
: normalizedStatus === 'unknown'
? `The outcome could not be verified. ${latestTerminalReason || 'Reconnect to rehydrate the durable run and inspect its evidence.'} No completion was claimed.`
: normalizedStatus === 'orphaned'
? `The worker disconnected before verification. ${latestTerminalReason || 'Reconnect to rehydrate the durable run.'} Do not assume the workspace changed safely.`
: normalizedStatus === 'cancelled'
						? 'Cancellation is authoritative. Any interrupted work remains unverified and cannot be reported as completed.'
			: ''
	);
	const analysis = $derived.by(() => {
		const event = [...events].reverse().find((item) => item?.kind === 'AUDIT_ANALYSIS_CREATED');
		return event?.payload || null;
	});
	const checks = $derived(Array.isArray(analysis?.checks) ? analysis.checks : []);
	const findings = $derived(Array.isArray(analysis?.findings) ? analysis.findings : []);
	const readiness = $derived.by(() => {
		if (!checks.length || checks.some((check: any) => check.status === 'unverified'))
			return 'Unverified';
		const passed = checks.filter((check: any) => check.status === 'passed').length;
		return `${Math.round((passed / checks.length) * 100)}% ready`;
	});
	const priorityFindings = $derived(
		findings.filter((finding: any) =>
			['critical', 'high', 'medium'].includes(String(finding.severity))
		)
	);
	let reportOpen = $state(false);
	let evidenceOpen = $state<Record<string, boolean>>({});
let auditTrailOpen = $state(false);
	let preservedEvidenceOpen = $state(false);
let exportingEvidence = $state(false);
let exportError = $state('');

async function exportEvidence() {
if (!runId || !workspace || exportingEvidence) return;
exportingEvidence = true;
exportError = '';
try {
const blob = await downloadFlowDeckEvidenceReport(runId, workspace);
const url = URL.createObjectURL(blob);
const link = document.createElement('a');
link.href = url;
link.download = `flowdeck-evidence-${runId.slice(0, 8)}.json`;
document.body.appendChild(link);
link.click();
link.remove();
URL.revokeObjectURL(url);
} catch (error) {
exportError = error instanceof Error ? error.message : 'Unable to export evidence';
} finally {
exportingEvidence = false;
}
}

	const label = $derived.by(() => {
		switch (status.toLowerCase()) {
			case 'preparing':
				return isAudit ? 'Preparing repository audit…' : 'Preparing FlowDeck…';
			case 'active':
				return 'FlowDeck activity';
			case 'planning':
				return 'Planning';
			case 'validating':
				return 'Validating task against the implementation and platform boundaries';
			case 'verifying':
				return 'Verifying';
			case 'succeeded':
			case 'completed':
				return 'Completed';
			case 'cancelled':
				return 'Cancelled';
			case 'manual_review':
			case 'manual_review_required':
				return 'Manual review required';
			case 'unknown':
				return 'Unknown outcome';
			case 'orphaned':
				return 'Orphaned';
			case 'failed':
				return 'Failed';
			default:
				return sending ? 'Starting orchestration…' : 'FlowDeck';
		}
	});
</script>

{#if status || sending}
	<div
		class="flowdeck-status-strip"
		class:is-terminal={isTerminal}
		class:is-active={isActive}
		role="status"
		aria-live="polite"
	>
		<span class="flowdeck-status-mark" aria-hidden="true">{isTerminal ? '·' : '›'}</span>
		<span class="flowdeck-status-label">{label}</span>
		{#if runId}
			<span class="flowdeck-status-run" title={runId}>run {runId.slice(0, 8)}</span>
		{/if}
		{#if telemetry.total_tokens > 0}
			<span class="flowdeck-status-telemetry" title="Authoritative provider-reported token usage">
				{telemetry.total_tokens.toLocaleString()} tokens{telemetry.cost !== null
					? ` · $${telemetry.cost.toFixed(4)}`
					: ' · cost unknown'}
			</span>
		{/if}
		{#if isAudit && !isTerminal}
			<span class="flowdeck-audit-honesty">No percentage estimates</span>
		{/if}
		{#if oncancel && !isTerminal}
			<button type="button" class="flowdeck-cancel" onclick={oncancel}>Cancel</button>
		{/if}
{#if needsRecovery && onreconnect}
<button
type="button"
class="flowdeck-reconnect"
data-testid="button-flowdeck-reconnect"
onclick={onreconnect}
>
Reconnect &amp; rehydrate
</button>
{/if}
	</div>
	{#if recoveryMessage}
<div class="flowdeck-status-recovery" role="alert" data-testid="flowdeck-recovery-explanation">
<strong>Recovery status:</strong> {recoveryMessage}
{#if needsRecovery}
<span class="flowdeck-recovery-note">Reconnecting only replays the existing run and evidence; it does not create a second terminal event.</span>
{/if}
</div>
	{/if}
	{#if isAudit && analysis}
		<section class="flowdeck-report" aria-label="Audit report">
			<button
				type="button"
				class="flowdeck-report-toggle"
				aria-expanded={reportOpen}
				onclick={() => (reportOpen = !reportOpen)}
			>
				<span>
					<strong>Audit report</strong>
					<span class="flowdeck-report-subtitle"
						>{readiness} · {checks.length} risk areas reviewed</span
					>
				</span>
				<span aria-hidden="true">{reportOpen ? '−' : '+'}</span>
			</button>
			{#if reportOpen}
				<div class="flowdeck-report-body">
					{#if priorityFindings.length}
						<div class="flowdeck-section-label">Findings</div>
						{#each priorityFindings as finding (finding.id)}
							<article class="flowdeck-finding">
								<div class="flowdeck-finding-heading">
									<span class="flowdeck-severity severity-{finding.severity}">
										{finding.severity === 'critical'
											? 'P0'
											: finding.severity === 'high'
												? 'P1'
												: 'P2'}
									</span>
									<strong>{finding.title}</strong>
								</div>
								<div class="flowdeck-finding-meta">
									{finding.confidence} confidence · {finding.status}
								</div>
								<p>{finding.impact}</p>
								<button
									type="button"
									class="flowdeck-evidence-toggle"
									aria-expanded={evidenceOpen[finding.id] ?? false}
									onclick={() =>
										(evidenceOpen = {
											...evidenceOpen,
											[finding.id]: !(evidenceOpen[finding.id] ?? false)
										})}
								>
									{evidenceOpen[finding.id] ? 'Hide evidence' : 'Show evidence'}
								</button>
								{#if evidenceOpen[finding.id]}
									<ul class="flowdeck-evidence-list">
										{#each finding.evidence || [] as item}
											<li>{item}</li>
										{/each}
									</ul>
								{/if}
							</article>
						{/each}
					{:else}
						<p class="flowdeck-report-empty">No P0–P2 findings are recorded.</p>
					{/if}
					<div class="flowdeck-check-summary">
						<span>Passed {checks.filter((check: any) => check.status === 'passed').length}</span>
						<span
							>Unverified {checks.filter((check: any) => check.status === 'unverified')
								.length}</span
						>
						<span>Failed {checks.filter((check: any) => check.status === 'failed').length}</span>
					</div>
				</div>
			{/if}
		</section>
	{/if}
{#if evidenceSummary?.entries?.length}
<section class="flowdeck-audit-trail" aria-label="FlowDeck evidence summary">
<div class="flowdeck-audit-header">
<button
type="button"
class="flowdeck-report-toggle"
aria-expanded={auditTrailOpen}
onclick={() => (auditTrailOpen = !auditTrailOpen)}
>
<span>
<strong>Evidence trail</strong>
<span class="flowdeck-report-subtitle">
{evidenceSummary.total || evidenceSummary.entries.length} deduplicated entries
{evidenceSummary.truncated ? ' · showing the latest 100' : ''}
</span>
</span>
<span aria-hidden="true">{auditTrailOpen ? '−' : '+'}</span>
</button>
<button
type="button"
class="flowdeck-export-button"
data-testid="button-flowdeck-export-evidence"
disabled={!workspace || exportingEvidence}
aria-label="Download safe FlowDeck evidence report"
onclick={exportEvidence}
>
{exportingEvidence ? 'Preparing…' : 'Download report'}
</button>
</div>
{#if exportError}
<p class="flowdeck-export-error" role="alert">{exportError}</p>
{/if}
{#if auditTrailOpen}
<ol class="flowdeck-audit-list">
{#each evidenceSummary.entries as entry (entry.id)}
<li class="flowdeck-audit-entry">
<span class:authoritative={entry.authority === 'authoritative'} class="flowdeck-audit-badge">
{entry.authority === 'authoritative' ? 'Authoritative' : 'Advisory'}
</span>
<span class="flowdeck-audit-kind">{entry.kind}</span>
<span class="flowdeck-audit-sequence">#{entry.sequence}</span>
</li>
{/each}
		</ol>
		{#if onnewrun}
			<div class="flowdeck-new-run">
				<p>Review complete: the evidence above belongs to the interrupted run and is read-only.</p>
				<button
					type="button"
					class="flowdeck-new-run-button"
					data-testid="button-flowdeck-new-run"
					onclick={onnewrun}
				>
					Start a new run
				</button>
			</div>
		{/if}
{/if}
</section>
{/if}
{#if preservedEvidenceSummary?.entries?.length}
	<section class="flowdeck-audit-trail flowdeck-preserved-evidence" aria-label="Preserved FlowDeck evidence">
		<button
			type="button"
			class="flowdeck-report-toggle"
			aria-expanded={preservedEvidenceOpen}
			onclick={() => (preservedEvidenceOpen = !preservedEvidenceOpen)}
		>
			<span>
				<strong>Preserved evidence</strong>
				<span class="flowdeck-report-subtitle">
					Read-only record from run {preservedEvidenceSummary.run_id?.slice(0, 8) || 'unknown'}
				</span>
			</span>
			<span aria-hidden="true">{preservedEvidenceOpen ? '−' : '+'}</span>
		</button>
		{#if preservedEvidenceOpen}
			<ol class="flowdeck-audit-list">
				{#each preservedEvidenceSummary.entries as entry (entry.id)}
					<li class="flowdeck-audit-entry">
						<span class:authoritative={entry.authority === 'authoritative'} class="flowdeck-audit-badge">
							{entry.authority === 'authoritative' ? 'Authoritative' : 'Advisory'}
						</span>
						<span class="flowdeck-audit-kind">{entry.kind}</span>
						<span class="flowdeck-audit-sequence">#{entry.sequence}</span>
					</li>
				{/each}
			</ol>
		{/if}
	</section>
{/if}
	<DesignerResults
		{events}
		{status}
		{runId}
		{nativeMessageId}
		{oncancel}
		{onreconnect}
		{onaction}
	/>
{/if}

<style>
	.flowdeck-status-strip {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		min-width: 0;
		min-height: 1.75rem;
		padding: 0.15rem 0.2rem;
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
		font-size: 0.6875rem;
		line-height: 1.2;
	}

	.flowdeck-status-mark {
		flex: 0 0 auto;
		color: color-mix(in oklab, var(--app-fg) 45%, transparent);
		font-size: 1rem;
		line-height: 1;
	}

	.flowdeck-status-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.flowdeck-status-run {
		flex: 0 0 auto;
		margin-left: auto;
		color: color-mix(in oklab, var(--app-fg) 38%, transparent);
		font:
			0.6rem ui-monospace,
			SFMono-Regular,
			monospace;
	}

	.flowdeck-status-telemetry {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: color-mix(in oklab, var(--app-fg) 45%, transparent);
	}
	.flowdeck-audit-honesty {
		color: color-mix(in oklab, var(--app-fg) 40%, transparent);
		font-size: 0.6rem;
	}
.flowdeck-cancel,
.flowdeck-reconnect,
	.flowdeck-evidence-toggle {
		border-radius: 0.35rem;
		padding: 0.2rem 0.45rem;
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
		font-size: 0.62rem;
	}
.flowdeck-cancel:hover,
.flowdeck-reconnect:hover,
	.flowdeck-evidence-toggle:hover {
		background: color-mix(in oklab, var(--app-fg) 8%, transparent);
		color: var(--app-fg);
	}
	.flowdeck-new-run {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		border-top: 1px solid color-mix(in oklab, var(--app-fg) 10%, transparent);
		padding: 0.6rem 0.7rem 0.65rem;
	}
	.flowdeck-new-run p {
		margin: 0;
		color: color-mix(in oklab, var(--app-fg) 55%, transparent);
		font-size: 0.62rem;
		line-height: 1.35;
	}
	.flowdeck-new-run-button {
		flex: 0 0 auto;
		border: 1px solid color-mix(in oklab, #a78bfa 42%, transparent);
		border-radius: 0.35rem;
		padding: 0.28rem 0.5rem;
		color: var(--app-fg);
		font-size: 0.62rem;
	}
	.flowdeck-new-run-button:hover {
		background: color-mix(in oklab, #a78bfa 15%, transparent);
	}
	.flowdeck-report {
		margin: 0.35rem 0 0.65rem 1.1rem;
		max-width: 42rem;
		border: 1px solid color-mix(in oklab, #a78bfa 20%, transparent);
		border-radius: 0.65rem;
		background: color-mix(in oklab, var(--app-surface) 98%, #312e81);
	}
.flowdeck-audit-trail {
		margin: 0.35rem 0 0.65rem 1.1rem;
		max-width: 42rem;
		border: 1px solid color-mix(in oklab, var(--app-fg) 11%, transparent);
		border-radius: 0.65rem;
		background: color-mix(in oklab, var(--app-surface) 99%, transparent);
	}
	.flowdeck-audit-list {
		display: grid;
		gap: 0.12rem;
		max-height: 14rem;
		overflow-y: auto;
		border-top: 1px solid color-mix(in oklab, var(--app-fg) 8%, transparent);
		padding: 0.45rem 0.65rem 0.5rem 2rem;
		scrollbar-width: thin;
		font-size: 0.62rem;
	}
	.flowdeck-audit-entry {
		display: grid;
		grid-template-columns: 0.55rem minmax(0, 1fr) auto;
		align-items: center;
		gap: 0.45rem;
		min-width: 0;
		min-height: 1.45rem;
		border-bottom: 1px solid color-mix(in oklab, var(--app-fg) 5%, transparent);
	}
	.flowdeck-audit-entry:last-child {
		border-bottom: 0;
	}
	.flowdeck-audit-badge {
		display: block;
		width: 0.38rem;
		height: 0.38rem;
		border-radius: 999px;
		background: #f59e0b;
		box-shadow: 0 0 0 3px color-mix(in oklab, #f59e0b 12%, transparent);
		font-size: 0;
	}
	.flowdeck-audit-badge.authoritative {
		background: #34d399;
		box-shadow: 0 0 0 3px color-mix(in oklab, #34d399 12%, transparent);
	}
	.flowdeck-audit-kind {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: color-mix(in oklab, var(--app-fg) 66%, transparent);
	}
	.flowdeck-audit-sequence {
		color: color-mix(in oklab, var(--app-fg) 34%, transparent);
		font-family: ui-monospace, SFMono-Regular, monospace;
		font-size: 0.57rem;
}
.flowdeck-audit-header {
display: flex;
align-items: stretch;
gap: 0.35rem;
}
.flowdeck-audit-header .flowdeck-report-toggle {
flex: 1 1 auto;
}
.flowdeck-export-button {
align-self: center;
margin-right: 0.55rem;
border: 1px solid color-mix(in oklab, var(--app-fg) 18%, transparent);
border-radius: 0.35rem;
padding: 0.3rem 0.45rem;
background: transparent;
color: color-mix(in oklab, var(--app-fg) 72%, transparent);
font-size: 0.58rem;
white-space: nowrap;
}
.flowdeck-export-button:hover:not(:disabled) {
background: color-mix(in oklab, var(--app-fg) 8%, transparent);
}
.flowdeck-export-button:disabled {
cursor: not-allowed;
opacity: 0.55;
}
.flowdeck-export-error {
margin: 0 0.7rem 0.45rem;
color: #fca5a5;
font-size: 0.62rem;
}
.flowdeck-audit-entry {
display: flex;
align-items: center;
gap: 0.4rem;
min-width: 0;
}
.flowdeck-audit-badge {
flex: 0 0 auto;
border-radius: 0.25rem;
padding: 0.12rem 0.28rem;
background: color-mix(in oklab, #f59e0b 18%, transparent);
color: color-mix(in oklab, #f59e0b 80%, var(--app-fg));
font-size: 0.55rem;
}
.flowdeck-audit-badge.authoritative {
background: color-mix(in oklab, #34d399 18%, transparent);
color: color-mix(in oklab, #34d399 80%, var(--app-fg));
}
.flowdeck-audit-kind {
overflow: hidden;
text-overflow: ellipsis;
white-space: nowrap;
color: color-mix(in oklab, var(--app-fg) 70%, transparent);
}
.flowdeck-audit-sequence {
margin-left: auto;
color: color-mix(in oklab, var(--app-fg) 42%, transparent);
font-family: ui-monospace, SFMono-Regular, monospace;
}
	.flowdeck-report-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		width: 100%;
		min-height: 2.75rem;
		padding: 0.55rem 0.75rem;
		text-align: left;
		color: var(--app-fg);
	}
	.flowdeck-report-toggle > span:first-child {
		min-width: 0;
	}
	.flowdeck-report-toggle > span:last-child {
		flex: 0 0 auto;
		display: grid;
		width: 1.25rem;
		height: 1.25rem;
		place-items: center;
		border: 1px solid color-mix(in oklab, var(--app-fg) 14%, transparent);
		border-radius: 999px;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font-size: 0.8rem;
	}
	.flowdeck-report-subtitle {
		display: block;
		margin-top: 0.15rem;
		color: color-mix(in oklab, var(--app-fg) 52%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-report-body {
		border-top: 1px solid color-mix(in oklab, #a78bfa 13%, transparent);
		padding: 0.7rem 0.75rem 0.75rem;
	}
	.flowdeck-finding {
		margin-top: 0.5rem;
		border: 1px solid color-mix(in oklab, var(--app-fg) 8%, transparent);
		border-radius: 0.45rem;
		padding: 0.55rem 0.6rem;
	}
	.flowdeck-finding:first-of-type {
		margin-top: 0.35rem;
	}
	.flowdeck-finding-heading {
		display: flex;
		align-items: flex-start;
		gap: 0.45rem;
		font-size: 0.7rem;
		line-height: 1.35;
	}
	.flowdeck-severity {
		flex: 0 0 auto;
		border-radius: 0.3rem;
		padding: 0.12rem 0.3rem;
		font-size: 0.58rem;
		font-weight: 700;
	}
	.severity-critical {
		background: #7f1d1d;
		color: #fecaca;
	}
	.severity-high {
		background: #9a3412;
		color: #fed7aa;
	}
	.severity-medium {
		background: #854d0e;
		color: #fef08a;
	}
	.flowdeck-finding-meta {
		margin-top: 0.2rem;
		color: color-mix(in oklab, var(--app-fg) 48%, transparent);
		font-size: 0.6rem;
		text-transform: capitalize;
	}
	.flowdeck-finding p {
		margin: 0.3rem 0;
		color: color-mix(in oklab, var(--app-fg) 68%, transparent);
		font-size: 0.65rem;
		line-height: 1.35;
	}
	.flowdeck-evidence-list {
		margin: 0.35rem 0 0;
		padding-left: 1rem;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font:
			0.62rem/1.35 ui-monospace,
			monospace;
		overflow-wrap: anywhere;
	}
	.flowdeck-check-summary {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		margin-top: 0.7rem;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-report-empty {
		margin: 0;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font-size: 0.68rem;
	}

	.flowdeck-status-strip.is-active .flowdeck-status-mark {
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
	}

	.flowdeck-status-strip.is-terminal {
		color: color-mix(in oklab, var(--app-fg) 52%, transparent);
	}

	.flowdeck-status-recovery {
		margin: -0.1rem 0 0.35rem 1.15rem;
		max-width: 42rem;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font-size: 0.6875rem;
		line-height: 1.35;
	}

	@media (max-width: 430px) {
		.flowdeck-status-strip {
			padding-inline: 0.1rem;
		}

		.flowdeck-status-run {
			display: none;
		}
		.flowdeck-status-telemetry {
			max-width: 12rem;
		}
		.flowdeck-audit-honesty {
			display: none;
		}
		.flowdeck-report {
			margin-left: 0.1rem;
		}
		.flowdeck-new-run {
			align-items: stretch;
			flex-direction: column;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.flowdeck-status-strip,
		.flowdeck-status-mark {
			animation: none;
			transition: none;
		}
	}
</style>
