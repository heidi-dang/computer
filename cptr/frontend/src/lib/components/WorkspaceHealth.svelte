<script lang="ts">
	import type { WorkspaceProjection } from '$lib/apis/workspace-os';

	interface Props {
		health: Record<string, unknown> | null;
		projection: WorkspaceProjection | null;
		busy: boolean;
		onrefresh: () => void | Promise<void>;
		onreconcile: () => void | Promise<void>;
	}

	let { health, projection, busy, onrefresh, onreconcile }: Props = $props();

	const healthBand = $derived(
		String(health?.health_band ?? projection?.health.status ?? 'unknown')
	);
	const available = $derived(
		typeof health?.available === 'boolean'
			? health.available
			: (projection?.health.checks.path_exists ?? false)
	);
	const gitRepository = $derived(Boolean(health?.is_git_repo));
	const summary = $derived(
		health?.summary && typeof health.summary === 'object'
			? (health.summary as Record<string, unknown>)
			: {}
	);
	const diagnostics = $derived(
		Array.isArray(health?.diagnostics) ? health.diagnostics.map((item) => String(item)) : []
	);
	const projectionChecks = $derived.by(() => {
		const checks = projection?.health.checks;
		if (!checks) return [];
		return [
			{ label: 'Path configured', value: Boolean(checks.path_configured), detail: null },
			{ label: 'Path exists', value: Boolean(checks.path_exists), detail: null },
			{ label: 'Primary repository', value: Boolean(checks.has_primary_repo), detail: null },
			{ label: 'Context cached', value: Boolean(checks.context_cached), detail: null },
			{
				label: 'Repositories healthy',
				value:
					checks.repositories_total === 0 ||
					checks.repositories_healthy >= checks.repositories_total,
				detail: `${checks.repositories_healthy}/${checks.repositories_total}`
			},
			{
				label: 'Workbench sessions',
				value: true,
				detail: String(checks.workbench_sessions_active)
			}
		];
	});

	const metrics = $derived([
		{ label: 'Active workers', value: numberValue(summary.active_overlap), warn: false },
		{ label: 'Ready workers', value: numberValue(summary.ready), warn: false },
		{
			label: 'Stale with changes',
			value: numberValue(summary.stale_with_changes),
			warn: true
		},
		{ label: 'Missing worktrees', value: numberValue(summary.missing_worktree), warn: true },
		{ label: 'Orphan worktrees', value: numberValue(summary.orphan_worktrees), warn: true },
		{
			label: 'Detached worktrees',
			value: numberValue(summary.detached_worktrees),
			warn: true
		},
		{
			label: 'Revision mismatch',
			value: numberValue(summary.origin_branch_mismatch),
			warn: true
		},
		{
			label: 'Orphaned sessions',
			value: numberValue(summary.orphaned_sessions),
			warn: true
		}
	] satisfies Array<{ label: string; value: number; warn: boolean }>);

	function numberValue(value: unknown): number {
		return typeof value === 'number' && Number.isFinite(value) ? value : 0;
	}

	function pretty(value: unknown): string {
		return JSON.stringify(value, null, 2);
	}
</script>

<section class="space-y-4" aria-label="Workspace health">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h3 class="text-base font-semibold">Health & reconciliation</h3>
				<span class="health-band" data-band={healthBand}>{healthBand}</span>
			</div>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Read-only health classification with conservative reconciliation. Reconcile never deletes
				worktrees, branches, or uncommitted changes.
			</p>
		</div>
		<div class="flex gap-2">
			<button class="workspace-secondary" disabled={busy} onclick={() => void onrefresh()}
				>Refresh</button
			>
			<button class="workspace-primary" disabled={busy} onclick={() => void onreconcile()}>
				{busy ? 'Reconciling…' : 'Reconcile'}
			</button>
		</div>
	</div>

	{#if projectionChecks.length > 0}
		<div class="health-checks" aria-label="Workspace health checks">
			{#each projectionChecks as check (check.label)}
				<div class="health-check" data-ok={check.value}>
					<span class="health-check-dot" aria-hidden="true"></span>
					<div>
						<strong>{check.label}</strong>
						<small>{check.detail ?? (check.value ? 'Pass' : 'Needs attention')}</small>
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<div class="grid gap-3 sm:grid-cols-3">
		<div class="health-card">
			<span>Workspace path</span>
			<strong>{available ? 'Available' : 'Unavailable'}</strong>
			<small>{projection?.path ?? 'No path configured'}</small>
		</div>
		<div class="health-card">
			<span>Git repository</span>
			<strong>{gitRepository ? 'Detected' : 'Not detected'}</strong>
			<small
				>{projection?.health.checks.has_primary_repo
					? 'Primary repository assigned'
					: 'No primary repository'}</small
			>
		</div>
		<div class="health-card">
			<span>Canonical repositories</span>
			<strong
				>{projection?.health.checks.repositories_healthy ?? 0}/{projection?.health.checks
					.repositories_total ?? 0}</strong
			>
			<small>healthy canonical checkouts</small>
		</div>
	</div>

	<div class="health-metrics">
		{#each metrics as metric}
			<div>
				<span>{metric.label}</span>
				<strong class:health-warning={metric.warn && metric.value > 0}>
					{metric.value}
				</strong>
			</div>
		{/each}
	</div>

	<div class="health-panel">
		<div class="health-panel-heading">
			<div>
				<h4>Diagnostics</h4>
				<p>Concrete conditions reported by the backend health classifier.</p>
			</div>
			<span>{diagnostics.length}</span>
		</div>
		{#if diagnostics.length === 0}
			<p class="health-empty">No health diagnostics are currently reported.</p>
		{:else}
			<ul class="health-diagnostics">
				{#each diagnostics as diagnostic}
					<li>{diagnostic}</li>
				{/each}
			</ul>
		{/if}
	</div>

	<details class="health-advanced">
		<summary>Advanced health details</summary>
		<div class="grid gap-3 pt-3 lg:grid-cols-2">
			<pre>{pretty(health?.git_health ?? {})}</pre>
			<pre>{pretty(health?.fdx_staleness ?? {})}</pre>
			<pre>{pretty(health?.origin_branch_check ?? {})}</pre>
			<pre>{pretty(health?.session_check ?? {})}</pre>
		</div>
	</details>
</section>

<style>
	.workspace-primary,
	.workspace-secondary {
		border-radius: 0.65rem;
		padding: 0.5rem 0.8rem;
		font-size: 0.8rem;
		font-weight: 600;
	}

	.workspace-primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}

	.workspace-secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.workspace-primary:disabled,
	.workspace-secondary:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}

	.health-band {
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.18rem 0.5rem;
		font-size: 0.68rem;
		font-weight: 700;
		text-transform: uppercase;
		color: var(--app-muted-fg);
	}

	.health-band[data-band='healthy'],
	.health-band[data-band='active'] {
		color: rgb(34 197 94);
		border-color: rgb(34 197 94 / 0.28);
		background: rgb(34 197 94 / 0.07);
	}

	.health-band[data-band='degraded'] {
		color: rgb(234 179 8);
		border-color: rgb(234 179 8 / 0.28);
		background: rgb(234 179 8 / 0.07);
	}

	.health-band[data-band='unhealthy'] {
		color: rgb(239 68 68);
		border-color: rgb(239 68 68 / 0.28);
		background: rgb(239 68 68 / 0.07);
	}

	.health-card,
	.health-panel,
	.health-metrics,
	.health-advanced {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
	}

	.health-checks {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.8rem;
	}

	.health-check {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		padding: 0.65rem 0.75rem;
	}

	.health-check:nth-child(n + 4) {
		border-top: 1px solid var(--app-border);
	}

	.health-check:not(:nth-child(3n + 1)) {
		border-left: 1px solid var(--app-border);
	}

	.health-check-dot {
		width: 0.48rem;
		height: 0.48rem;
		flex: none;
		border-radius: 999px;
		background: rgb(239 68 68);
	}

	.health-check[data-ok='true'] .health-check-dot {
		background: rgb(34 197 94);
	}

	.health-check strong,
	.health-check small {
		display: block;
	}

	.health-check strong {
		font-size: 0.68rem;
	}

	.health-check small {
		margin-top: 0.15rem;
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.health-card {
		display: flex;
		min-height: 6.5rem;
		flex-direction: column;
		padding: 0.85rem;
	}

	.health-card span,
	.health-card small,
	.health-metrics span,
	.health-panel-heading p {
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.health-card strong {
		margin-top: 0.55rem;
		font-size: 1rem;
	}

	.health-card small {
		margin-top: auto;
		padding-top: 0.55rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.health-metrics {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		overflow: hidden;
	}

	.health-metrics > div {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding: 0.75rem;
	}

	.health-metrics > div:nth-child(n + 5) {
		border-top: 1px solid var(--app-border);
	}

	.health-metrics > div:not(:nth-child(4n + 1)) {
		border-left: 1px solid var(--app-border);
	}

	.health-metrics strong {
		font-size: 0.95rem;
	}

	.health-warning {
		color: rgb(234 179 8);
	}

	.health-panel {
		overflow: hidden;
	}

	.health-panel-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
		border-bottom: 1px solid var(--app-border);
		padding: 0.8rem 0.9rem;
	}

	.health-panel-heading h4 {
		font-size: 0.82rem;
		font-weight: 650;
	}

	.health-panel-heading p {
		margin-top: 0.2rem;
	}

	.health-panel-heading > span {
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.15rem 0.45rem;
		font-size: 0.65rem;
		color: var(--app-muted-fg);
	}

	.health-empty {
		padding: 1rem;
		font-size: 0.78rem;
		color: var(--app-muted-fg);
	}

	.health-diagnostics {
		display: grid;
		gap: 0;
	}

	.health-diagnostics li {
		padding: 0.75rem 0.9rem;
		font-size: 0.78rem;
		line-height: 1.4;
	}

	.health-diagnostics li + li {
		border-top: 1px solid var(--app-border);
	}

	.health-advanced {
		padding: 0.75rem 0.9rem;
	}

	.health-advanced summary {
		cursor: pointer;
		font-size: 0.78rem;
		font-weight: 600;
	}

	.health-advanced pre {
		max-height: 14rem;
		overflow: auto;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--app-bg) 94%, black);
		padding: 0.75rem;
		font-size: 0.68rem;
		line-height: 1.4;
		white-space: pre-wrap;
		word-break: break-word;
	}

	@media (max-width: 720px) {
		.health-checks {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.health-check:nth-child(n + 3) {
			border-top: 1px solid var(--app-border);
		}

		.health-check:not(:nth-child(3n + 1)) {
			border-left: 0;
		}

		.health-check:nth-child(even) {
			border-left: 1px solid var(--app-border);
		}

		.health-metrics {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.health-metrics > div:nth-child(n + 3) {
			border-top: 1px solid var(--app-border);
		}

		.health-metrics > div:not(:nth-child(4n + 1)) {
			border-left: 0;
		}

		.health-metrics > div:nth-child(even) {
			border-left: 1px solid var(--app-border);
		}
	}
</style>
