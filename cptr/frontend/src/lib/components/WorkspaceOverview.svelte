<script lang="ts">
	import type { WorkspaceProjection } from '$lib/apis/workspace-os';

	interface Props {
		projection: WorkspaceProjection | null;
		connectionStatus: string;
		staleDomains: readonly string[];
		onrefresh: () => void | Promise<void>;
	}

	let { projection, connectionStatus, staleDomains, onrefresh }: Props = $props();

	const connectionLabel = $derived(
		connectionStatus === 'live'
			? 'Live'
			: connectionStatus === 'snapshot'
				? 'Snapshot'
				: connectionStatus === 'reconnecting'
					? 'Reconnecting'
					: connectionStatus === 'connecting'
						? 'Connecting'
						: connectionStatus === 'failed'
							? 'Offline'
							: 'Loading'
	);

	function shortRevision(revision: string | null): string {
		return revision ? revision.slice(0, 8) : 'unknown';
	}

	function eventLabel(type: string): string {
		return type.replace(/^workspace\./, '').replaceAll('.', ' ');
	}

	function eventTime(timestamp: string): string {
		const value = Date.parse(timestamp);
		return Number.isFinite(value) ? new Date(value).toLocaleTimeString() : timestamp;
	}
</script>

<section class="space-y-5" aria-label="Workspace overview">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h3 class="text-base font-semibold">Overview</h3>
				<span class="workspace-live-badge" data-status={connectionStatus} aria-live="polite">
					<span class="workspace-live-dot"></span>
					{connectionLabel}
				</span>
				{#if staleDomains.length > 0}
					<span class="workspace-stale-badge"
						>{staleDomains.length} stale surface{staleDomains.length === 1 ? '' : 's'}</span
					>
				{/if}
			</div>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Server-authoritative Workspace identity, repository state, Workbench activity, context cache
				and recent events.
			</p>
		</div>
		<button class="workspace-secondary" onclick={() => void onrefresh()}>Refresh</button>
	</div>

	{#if !projection}
		<div class="workspace-empty">
			<div class="workspace-empty-pulse"></div>
			<p>Workspace projection is loading.</p>
		</div>
	{:else}
		<div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
			<div class="workspace-stat-card">
				<span>Health</span>
				<strong>{projection.health.status}</strong>
				<small
					>{projection.health.checks.repositories_healthy}/{projection.health.checks
						.repositories_total} repositories healthy</small
				>
			</div>
			<div class="workspace-stat-card">
				<span>Repositories</span>
				<strong>{projection.repositories.length}</strong>
				<small
					>{projection.health.checks.has_primary_repo
						? 'Primary repository assigned'
						: 'No primary repository'}</small
				>
			</div>
			<div class="workspace-stat-card">
				<span>Workbench</span>
				<strong>{projection.workbench.active_sessions_count}</strong>
				<small>active session{projection.workbench.active_sessions_count === 1 ? '' : 's'}</small>
			</div>
			<div class="workspace-stat-card">
				<span>Context cache</span>
				<strong>{projection.context_cache.is_cached ? 'Ready' : 'Cold'}</strong>
				<small
					>{projection.context_cache.access_count} access{projection.context_cache.access_count ===
					1
						? ''
						: 'es'}</small
				>
			</div>
		</div>

		<div class="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(18rem,0.65fr)]">
			<div class="workspace-panel">
				<div class="workspace-panel-heading">
					<div>
						<h4>Repositories & checkouts</h4>
						<p>Logical repository membership and canonical checkout revision.</p>
					</div>
				</div>
				{#if projection.repositories.length === 0}
					<p class="workspace-panel-empty">No repositories are attached to this Workspace.</p>
				{:else}
					<div class="divide-y divide-[var(--app-border)]">
						{#each projection.repositories as repository}
							<div class="workspace-repository-row">
								<div class="min-w-0">
									<div class="flex flex-wrap items-center gap-2">
										<strong class="truncate text-sm">{repository.name}</strong>
										{#if repository.primary}
											<span class="workspace-chip">Primary</span>
										{/if}
										<span class="workspace-chip">{repository.role}</span>
									</div>
									<p class="mt-1 truncate text-xs text-[var(--app-muted-fg)]">
										{repository.canonical_remote}
									</p>
								</div>
								<div class="workspace-checkout">
									{#if repository.checkout}
										<span class:workspace-unavailable={!repository.checkout.available}>
											{repository.checkout.branch || repository.default_branch || 'detached'}
										</span>
										<code>{shortRevision(repository.checkout.revision)}</code>
									{:else}
										<span class="workspace-unavailable">No canonical checkout</span>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<div class="workspace-panel">
				<div class="workspace-panel-heading">
					<div>
						<h4>Recent activity</h4>
						<p>Latest Workspace OS events from the backend projection.</p>
					</div>
				</div>
				{#if projection.recent_events.length === 0}
					<p class="workspace-panel-empty">No recent Workspace events.</p>
				{:else}
					<div class="space-y-1.5 p-3">
						{#each [...projection.recent_events].reverse().slice(0, 8) as event (event.event_id)}
							<div class="workspace-event-row">
								<div class="min-w-0">
									<div class="truncate text-xs font-medium capitalize">
										{eventLabel(event.type)}
									</div>
									<div class="mt-0.5 text-[0.68rem] text-[var(--app-muted-fg)]">
										sequence {event.sequence}
									</div>
								</div>
								<time
									class="shrink-0 text-[0.68rem] text-[var(--app-muted-fg)]"
									datetime={event.timestamp}
								>
									{eventTime(event.timestamp)}
								</time>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>

		<div class="workspace-runtime-strip">
			<div>
				<span>Workspace ID</span>
				<code>{projection.workspace_id}</code>
			</div>
			<div>
				<span>Context fingerprint</span>
				<code>{projection.context_cache.fingerprint?.slice(0, 12) ?? 'none'}</code>
			</div>
			<div>
				<span>Cache hit ratio</span>
				<strong>{Math.round(projection.metrics.hit_ratio * 100)}%</strong>
			</div>
			<div>
				<span>Invalidations</span>
				<strong>{projection.metrics.total_invalidations}</strong>
			</div>
		</div>
	{/if}
</section>

<style>
	.workspace-secondary {
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		background: var(--app-hover);
		padding: 0.5rem 0.8rem;
		font-size: 0.8rem;
		font-weight: 600;
	}

	.workspace-live-badge,
	.workspace-stale-badge,
	.workspace-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.18rem 0.5rem;
		font-size: 0.68rem;
		font-weight: 600;
		color: var(--app-muted-fg);
	}

	.workspace-live-dot {
		width: 0.42rem;
		height: 0.42rem;
		border-radius: 999px;
		background: currentColor;
		opacity: 0.7;
	}

	.workspace-live-badge[data-status='live'] {
		color: rgb(34 197 94);
		border-color: rgb(34 197 94 / 0.28);
		background: rgb(34 197 94 / 0.07);
	}

	.workspace-live-badge[data-status='reconnecting'],
	.workspace-live-badge[data-status='connecting'] {
		color: rgb(234 179 8);
		border-color: rgb(234 179 8 / 0.28);
		background: rgb(234 179 8 / 0.07);
	}

	.workspace-live-badge[data-status='failed'] {
		color: rgb(239 68 68);
		border-color: rgb(239 68 68 / 0.28);
		background: rgb(239 68 68 / 0.07);
	}

	.workspace-stale-badge {
		color: rgb(234 179 8);
	}

	.workspace-stat-card,
	.workspace-panel,
	.workspace-runtime-strip {
		border: 1px solid var(--app-border);
		background: color-mix(in srgb, var(--app-bg) 96%, var(--app-hover));
	}

	.workspace-stat-card {
		display: flex;
		min-height: 6.5rem;
		flex-direction: column;
		border-radius: 0.9rem;
		padding: 0.9rem;
	}

	.workspace-stat-card span,
	.workspace-runtime-strip span {
		font-size: 0.7rem;
		color: var(--app-muted-fg);
	}

	.workspace-stat-card strong {
		margin-top: 0.55rem;
		font-size: 1.15rem;
		text-transform: capitalize;
	}

	.workspace-stat-card small {
		margin-top: auto;
		padding-top: 0.55rem;
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.workspace-panel {
		overflow: hidden;
		border-radius: 0.9rem;
	}

	.workspace-panel-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
		border-bottom: 1px solid var(--app-border);
		padding: 0.85rem 0.9rem;
	}

	.workspace-panel-heading h4 {
		font-size: 0.82rem;
		font-weight: 650;
	}

	.workspace-panel-heading p {
		margin-top: 0.18rem;
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.workspace-panel-empty,
	.workspace-empty {
		padding: 1rem;
		font-size: 0.78rem;
		color: var(--app-muted-fg);
	}

	.workspace-empty {
		display: flex;
		min-height: 12rem;
		align-items: center;
		justify-content: center;
		gap: 0.6rem;
		border: 1px dashed var(--app-border);
		border-radius: 0.9rem;
	}

	.workspace-empty-pulse {
		width: 0.48rem;
		height: 0.48rem;
		border-radius: 999px;
		background: currentColor;
		animation: workspacePulse 1.2s ease-in-out infinite;
	}

	.workspace-repository-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.75rem;
		padding: 0.85rem 0.9rem;
	}

	.workspace-checkout {
		display: flex;
		align-items: flex-end;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.7rem;
	}

	.workspace-checkout code,
	.workspace-runtime-strip code {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: var(--app-muted-fg);
	}

	.workspace-unavailable {
		color: rgb(239 68 68);
	}

	.workspace-event-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		border-radius: 0.55rem;
		padding: 0.55rem 0.6rem;
	}

	.workspace-event-row:hover {
		background: var(--app-hover);
	}

	.workspace-runtime-strip {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0;
		overflow: hidden;
		border-radius: 0.9rem;
	}

	.workspace-runtime-strip > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.75rem 0.85rem;
	}

	.workspace-runtime-strip > div + div {
		border-left: 1px solid var(--app-border);
	}

	.workspace-runtime-strip code,
	.workspace-runtime-strip strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.72rem;
	}

	@keyframes workspacePulse {
		0%,
		100% {
			opacity: 0.25;
		}
		50% {
			opacity: 1;
		}
	}

	@media (max-width: 640px) {
		.workspace-runtime-strip {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.workspace-runtime-strip > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.workspace-runtime-strip > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}

		.workspace-repository-row {
			grid-template-columns: minmax(0, 1fr);
		}

		.workspace-checkout {
			align-items: flex-start;
		}
	}
</style>
