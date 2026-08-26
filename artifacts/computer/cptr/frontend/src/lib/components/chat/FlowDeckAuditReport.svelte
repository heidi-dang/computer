<script lang="ts">
	interface Props {
		events?: any[];
		open?: boolean;
		ontoggle?: () => void;
	}

	let { events = [], open = false, ontoggle }: Props = $props();

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
	let evidenceOpen = $state<Record<string, boolean>>({});
</script>

{#if analysis}
	<section class="flowdeck-report" aria-label="Audit report">
		<button
			type="button"
			class="flowdeck-report-toggle"
			aria-expanded={open}
			onclick={ontoggle}
		>
			<span>
				<strong>Audit report</strong>
				<span class="flowdeck-report-subtitle"
					>{readiness} · {checks.length} risk areas reviewed</span
				>
			</span>
			<span aria-hidden="true">{open ? '−' : '+'}</span>
		</button>
		{#if open}
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

<style>
	.flowdeck-report {
		margin: 0.4rem 0 0;
		border: 1px solid color-mix(in oklab, #a78bfa 20%, transparent);
		border-radius: 0.65rem;
		background: color-mix(in oklab, var(--app-surface) 98%, #312e81);
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
	.flowdeck-evidence-toggle {
		border-radius: 0.35rem;
		padding: 0.2rem 0.45rem;
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-evidence-toggle:hover {
		background: color-mix(in oklab, var(--app-fg) 8%, transparent);
		color: var(--app-fg);
	}
	.flowdeck-evidence-list {
		margin: 0.35rem 0 0;
		padding-left: 1rem;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font: 0.62rem/1.35 ui-monospace, monospace;
		overflow-wrap: anywhere;
	}
	.flowdeck-check-summary {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		margin-top: 0.7rem;
		color: color-mix(in oklab, var(--app-fg) 55%, transparent);
		font-size: 0.62rem;
	}
	.flowdeck-report-empty {
		margin: 0;
		color: color-mix(in oklab, var(--app-fg) 58%, transparent);
		font-size: 0.68rem;
	}
	@media (max-width: 640px) {
		.flowdeck-report {
			margin-top: 0.35rem;
			border-radius: 0.55rem;
		}
		.flowdeck-report-toggle {
			min-height: 2.55rem;
			padding-inline: 0.6rem;
		}
	}
</style>