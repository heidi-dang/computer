<script lang="ts">
	import type { WorkbenchSession } from '$lib/apis/plugin';

	let {
		sessions = [],
		selectedSessionId = null,
		busySessionId = null,
		onselect,
		onrename,
		onarchive,
		requestDelete
	}: {
		sessions?: WorkbenchSession[];
		selectedSessionId?: string | null;
		busySessionId?: string | null;
		onselect: (sessionId: string) => void;
		onrename: (session: WorkbenchSession) => void;
		onarchive: (session: WorkbenchSession) => void;
		requestDelete: (session: WorkbenchSession) => void;
	} = $props();

	function labelForTarget(session: WorkbenchSession) {
		if (!session.active_target_type || !session.active_target_id) return 'No active target';
		return `${session.active_target_type} · ${session.active_target_id.slice(0, 12)}`;
	}
</script>

<nav class="plugin-session-list" aria-label="Plugin Workbench Sessions">
	<div class="plugin-session-list-heading">
		<div>
			<h2>Plugin sessions</h2>
			<p>Durable CPTR activity from ChatGPT.</p>
		</div>
		<span>{sessions.length}</span>
	</div>
	{#if sessions.length === 0}
		<p class="empty-state">No Workbench Sessions yet. Ask ChatGPT to open one before it begins CPTR work.</p>
	{:else}
		<div class="plugin-session-items">
			{#each sessions as session (session.session_id)}
				<div
					class:selected={session.session_id === selectedSessionId}
					class:archived={session.status === 'ARCHIVED'}
					class="plugin-session-item"
				>
					<button class="plugin-session-select" type="button" onclick={() => onselect(session.session_id)}>
						<span class="session-name">{session.name}</span>
						<span class="session-id">{session.session_id}</span>
						<span class="session-target">{labelForTarget(session)}</span>
					</button>
					<div class="plugin-session-actions" aria-label={`Actions for ${session.name}`}>
						<button
							type="button"
							disabled={busySessionId === session.session_id}
							onclick={() => onrename(session)}
							aria-label={`Rename ${session.name}`}
						>
							Rename
						</button>
						{#if session.status !== 'ARCHIVED'}
							<button
								type="button"
								disabled={busySessionId === session.session_id}
								onclick={() => onarchive(session)}
							>
								Archive
							</button>
						{/if}
						<button
							type="button"
							class="danger"
							disabled={busySessionId === session.session_id}
							onclick={() => requestDelete(session)}
						>
							Delete
						</button>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</nav>

<style>
	.plugin-session-list {
		display: flex;
		min-height: 0;
		flex-direction: column;
		border-right: 1px solid var(--border-color, #e5e7eb);
		background: color-mix(in srgb, var(--bg-primary, #fff) 94%, #eef2ff);
	}

	.plugin-session-list-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.9rem 0.9rem 0.7rem;
		border-bottom: 1px solid var(--border-color, #e5e7eb);
	}

	h2,
	p {
		margin: 0;
	}

	h2 {
		font-size: 0.85rem;
		font-weight: 700;
		color: var(--text-primary, #111827);
	}

	.plugin-session-list-heading p,
	.session-id,
	.session-target,
	.empty-state {
		font-size: 0.7rem;
		line-height: 1.35;
		color: var(--text-secondary, #6b7280);
	}

	.plugin-session-list-heading > span {
		display: grid;
		place-items: center;
		min-width: 1.35rem;
		height: 1.35rem;
		border-radius: 999px;
		font: 0.68rem/1 ui-monospace, SFMono-Regular, Menlo, monospace;
		color: #4338ca;
		background: #e0e7ff;
	}

	.plugin-session-items {
		overflow: auto;
		padding: 0.45rem;
	}

	.plugin-session-item {
		margin-bottom: 0.4rem;
		border: 1px solid transparent;
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--bg-primary, #fff) 95%, #f1f5f9);
	}

	.plugin-session-item.selected {
		border-color: #818cf8;
		background: #eef2ff;
	}

	.plugin-session-item.archived {
		opacity: 0.64;
	}

	.plugin-session-select {
		display: grid;
		width: 100%;
		gap: 0.15rem;
		padding: 0.65rem 0.65rem 0.35rem;
		border: 0;
		background: transparent;
		text-align: left;
		cursor: pointer;
	}

	.session-name {
		overflow: hidden;
		font-size: 0.8rem;
		font-weight: 650;
		color: var(--text-primary, #111827);
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.session-id,
	.session-target {
		overflow: hidden;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.plugin-session-actions {
		display: flex;
		gap: 0.25rem;
		padding: 0 0.45rem 0.45rem;
	}

	.plugin-session-actions button {
		padding: 0.22rem 0.36rem;
		border: 0;
		border-radius: 0.3rem;
		font-size: 0.63rem;
		color: var(--text-secondary, #475569);
		background: transparent;
		cursor: pointer;
	}

	.plugin-session-actions button:hover:not(:disabled) {
		background: rgb(148 163 184 / 0.16);
	}

	.plugin-session-actions button.danger {
		color: #b91c1c;
	}

	.plugin-session-actions button:disabled {
		cursor: progress;
		opacity: 0.5;
	}

	.empty-state {
		padding: 0.85rem;
	}

	:global(.dark) .plugin-session-list {
		background: color-mix(in srgb, var(--bg-primary, #111827) 90%, #172554);
	}
	:global(.dark) h2,
	:global(.dark) .session-name {
		color: #e5e7eb;
	}
	:global(.dark) .plugin-session-item.selected {
		background: rgb(67 56 202 / 0.2);
	}

	@media (max-width: 720px) {
		.plugin-session-list {
			border-right: 0;
			border-bottom: 1px solid var(--border-color, #e5e7eb);
		}
		.plugin-session-list-heading {
			padding: 0.65rem 0.75rem 0.45rem;
		}
		.plugin-session-items {
			display: flex;
			gap: 0.45rem;
			overflow-x: auto;
			padding: 0.45rem 0.65rem 0.65rem;
			scroll-snap-type: x proximity;
		}
		.plugin-session-item {
			flex: 0 0 min(16.5rem, calc(100vw - 2rem));
			margin: 0;
			scroll-snap-align: start;
		}
	}
</style>
