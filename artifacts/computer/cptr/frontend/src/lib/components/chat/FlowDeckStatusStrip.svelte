<script lang="ts">
	import DesignerResults, { type DesignerAction } from './DesignerResults.svelte';

	interface Props {
		status?: string;
		runId?: string;
		nativeMessageId?: string | null;
		sending?: boolean;
		isAudit?: boolean;
		events?: any[];
		oncancel?: () => void;
		onreconnect?: () => void;
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
		nativeMessageId = '',
		sending = false,
		isAudit = false,
		events = [],
		oncancel,
		onreconnect,
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
	const recoveryMessage = $derived(
		status.toLowerCase() === 'manual_review_required'
			? 'The model provider was unavailable. Check the provider connection and review the workspace before retrying. No completion was claimed.'
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

	const label = $derived.by(() => {
		switch (status.toLowerCase()) {
			case 'preparing':
				return isAudit ? 'Preparing repository audit…' : 'Preparing FlowDeck…';
			case 'active':
				return 'FlowDeck activity';
			case 'planning':
				return 'Planning';
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
	</div>
	{#if recoveryMessage}
		<p class="flowdeck-status-recovery">{recoveryMessage}</p>
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
	.flowdeck-evidence-toggle {
		border-radius: 0.35rem;
		padding: 0.2rem 0.45rem;
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-cancel:hover,
	.flowdeck-evidence-toggle:hover {
		background: color-mix(in oklab, var(--app-fg) 8%, transparent);
		color: var(--app-fg);
	}
	.flowdeck-report {
		margin: 0.35rem 0 0.65rem 1.1rem;
		max-width: 42rem;
		border: 1px solid color-mix(in oklab, #a78bfa 28%, transparent);
		border-radius: 0.75rem;
		background: color-mix(in oklab, var(--app-surface) 95%, #312e81);
	}
	.flowdeck-report-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		width: 100%;
		padding: 0.55rem 0.7rem;
		text-align: left;
		color: var(--app-fg);
	}
	.flowdeck-report-subtitle {
		display: block;
		margin-top: 0.15rem;
		color: color-mix(in oklab, var(--app-fg) 52%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-report-body {
		border-top: 1px solid color-mix(in oklab, #a78bfa 18%, transparent);
		padding: 0.65rem 0.7rem;
	}
	.flowdeck-finding {
		margin-top: 0.5rem;
		border-top: 1px solid color-mix(in oklab, var(--app-fg) 8%, transparent);
		padding-top: 0.5rem;
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
	}

	@media (prefers-reduced-motion: reduce) {
		.flowdeck-status-strip,
		.flowdeck-status-mark {
			animation: none;
			transition: none;
		}
	}
</style>
