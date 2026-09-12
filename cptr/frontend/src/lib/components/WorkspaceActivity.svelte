<script lang="ts">
	import type { WorkspaceLiveEvent, WorkspaceProjection } from '$lib/apis/workspace-os';

	interface Props {
		projection: WorkspaceProjection | null;
		connectionStatus: string;
		onrefresh: () => void | Promise<void>;
	}

	let { projection, connectionStatus, onrefresh }: Props = $props();
	let filter = $state('all');

	const events = $derived(
		projection ? [...projection.recent_events].sort((a, b) => b.sequence - a.sequence) : []
	);
	const eventTypes = $derived(Array.from(new Set(events.map((event) => event.type))).sort());
	const visibleEvents = $derived(
		filter === 'all' ? events : events.filter((event) => event.type === filter)
	);

	function label(type: string): string {
		return type.replace(/^workspace\./, '').replaceAll('.', ' ');
	}

	function time(timestamp: string): string {
		const value = Date.parse(timestamp);
		return Number.isFinite(value) ? new Date(value).toLocaleString() : timestamp;
	}

	function payloadSummary(event: WorkspaceLiveEvent): string {
		const keys = Object.keys(event.payload ?? {});
		if (keys.length === 0) return 'No event details';
		return keys.slice(0, 4).join(' · ');
	}
</script>

<section class="space-y-4" aria-label="Workspace activity">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h3 class="text-base font-semibold">Activity</h3>
				<span class="activity-status" data-status={connectionStatus}>
					<span></span>
					{connectionStatus === 'live' ? 'Live stream' : connectionStatus}
				</span>
			</div>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Server-authoritative Workspace OS events. Sequence numbers expose ordering and replay
				recovery.
			</p>
		</div>
		<button class="workspace-secondary" onclick={() => void onrefresh()}>Refresh</button>
	</div>

	<div class="activity-toolbar">
		<label>
			<span>Event type</span>
			<select bind:value={filter}>
				<option value="all">All events</option>
				{#each eventTypes as type}
					<option value={type}>{label(type)}</option>
				{/each}
			</select>
		</label>
		<div class="activity-count">
			<strong>{visibleEvents.length}</strong>
			<span>shown</span>
		</div>
		<div class="activity-count">
			<strong>{events.at(0)?.sequence ?? 0}</strong>
			<span>latest sequence</span>
		</div>
	</div>

	{#if !projection}
		<div class="activity-empty">Workspace activity is loading.</div>
	{:else if visibleEvents.length === 0}
		<div class="activity-empty">No Workspace events match this filter.</div>
	{:else}
		<div class="activity-list">
			{#each visibleEvents as event (event.event_id)}
				<article class="activity-row">
					<div class="activity-sequence" aria-label={'Sequence ' + event.sequence}>
						#{event.sequence}
					</div>
					<div class="min-w-0 flex-1">
						<div class="flex flex-wrap items-center gap-2">
							<strong class="text-sm capitalize">{label(event.type)}</strong>
							{#if event.redaction_applied}
								<span class="activity-chip">redacted</span>
							{/if}
						</div>
						<p class="mt-1 truncate text-xs text-[var(--app-muted-fg)]">
							{payloadSummary(event)}
						</p>
						<div
							class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[0.68rem] text-[var(--app-muted-fg)]"
						>
							<time datetime={event.timestamp}>{time(event.timestamp)}</time>
							<span>{event.event_id.slice(0, 12)}</span>
							{#if event.task_id}<span>task {event.task_id.slice(0, 10)}</span>{/if}
							{#if event.worker_task_id}<span>worker {event.worker_task_id.slice(0, 10)}</span>{/if}
						</div>
					</div>
				</article>
			{/each}
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

	.activity-status,
	.activity-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.18rem 0.5rem;
		font-size: 0.68rem;
		font-weight: 600;
		color: var(--app-muted-fg);
		text-transform: capitalize;
	}

	.activity-status > span {
		width: 0.4rem;
		height: 0.4rem;
		border-radius: 999px;
		background: currentColor;
		opacity: 0.7;
	}

	.activity-status[data-status='live'] {
		color: rgb(34 197 94);
		border-color: rgb(34 197 94 / 0.28);
		background: rgb(34 197 94 / 0.07);
	}

	.activity-status[data-status='connecting'],
	.activity-status[data-status='reconnecting'] {
		color: rgb(234 179 8);
		border-color: rgb(234 179 8 / 0.28);
		background: rgb(234 179 8 / 0.07);
	}

	.activity-status[data-status='failed'] {
		color: rgb(239 68 68);
		border-color: rgb(239 68 68 / 0.28);
		background: rgb(239 68 68 / 0.07);
	}

	.activity-toolbar {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 0.75rem;
		align-items: end;
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		padding: 0.8rem;
	}

	.activity-toolbar label {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.35rem;
	}

	.activity-toolbar label > span,
	.activity-count span {
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.activity-toolbar select {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.6rem;
		background: var(--app-bg);
		padding: 0.45rem 0.55rem;
		font-size: 0.78rem;
	}

	.activity-count {
		display: flex;
		min-width: 6.5rem;
		flex-direction: column;
		border-left: 1px solid var(--app-border);
		padding-left: 0.75rem;
	}

	.activity-count strong {
		font-size: 0.9rem;
	}

	.activity-list {
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
	}

	.activity-row {
		display: flex;
		gap: 0.8rem;
		padding: 0.85rem;
	}

	.activity-row + .activity-row {
		border-top: 1px solid var(--app-border);
	}

	.activity-row:hover {
		background: var(--app-hover);
	}

	.activity-sequence {
		min-width: 3.3rem;
		padding-top: 0.1rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	.activity-chip {
		padding: 0.1rem 0.4rem;
		font-size: 0.62rem;
	}

	.activity-empty {
		border: 1px dashed var(--app-border);
		border-radius: 0.85rem;
		padding: 2.5rem 1rem;
		text-align: center;
		font-size: 0.8rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 640px) {
		.activity-toolbar {
			grid-template-columns: 1fr 1fr;
		}

		.activity-toolbar label {
			grid-column: 1 / -1;
		}

		.activity-count {
			border-left: 0;
			padding-left: 0;
		}

		.activity-count + .activity-count {
			border-left: 1px solid var(--app-border);
			padding-left: 0.75rem;
		}
	}
</style>
