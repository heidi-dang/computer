<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		archiveWorkbenchSession,
		getWorkbenchSessionEvents,
		listWorkbenchSessions,
		renameWorkbenchSession,
		type WorkbenchSessionEventView,
		type WorkbenchSessionView
	} from '$lib/apis/workbench';

	interface Props {
		workspaceId: string;
		stale?: boolean;
		onfresh?: () => void;
	}

	let { workspaceId, stale = false, onfresh = () => {} }: Props = $props();

	let sessions = $state<WorkbenchSessionView[]>([]);
	let selectedSessionId = $state('');
	let events = $state<WorkbenchSessionEventView[]>([]);
	let renameDraft = $state('');
	let loading = $state(false);
	let busy = $state('');
	let error = $state('');
	let loadGeneration = 0;
	let eventGeneration = 0;
	let attemptedStaleRefresh = false;

	const selected = $derived(
		sessions.find((session) => session.session_id === selectedSessionId) ?? null
	);

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Workbench operation failed';
	}

	function belongsToWorkspace(session: WorkbenchSessionView): boolean {
		return session.workspace_id === workspaceId || session.active_workspace_id === workspaceId;
	}

	function time(value: number | null): string {
		if (!value) return 'Never';
		return new Date(value).toLocaleString();
	}

	function label(value: string): string {
		return value.replace(/^workbench\./, '').replaceAll('.', ' ');
	}

	async function load(preserveSelection = true) {
		const generation = ++loadGeneration;
		loading = true;
		error = '';
		try {
			const result = await listWorkbenchSessions(false, 100);
			if (generation !== loadGeneration) return;
			sessions = result.sessions.filter(belongsToWorkspace);
			const current = preserveSelection ? selectedSessionId : '';
			selectedSessionId =
				(current && sessions.some((session) => session.session_id === current)
					? current
					: sessions[0]?.session_id) ?? '';
			if (selectedSessionId) {
				await loadEvents(selectedSessionId);
				renameDraft = sessions.find((item) => item.session_id === selectedSessionId)?.name ?? '';
			} else {
				events = [];
				renameDraft = '';
			}
			onfresh();
		} catch (cause) {
			if (generation !== loadGeneration) return;
			error = message(cause);
		} finally {
			if (generation === loadGeneration) loading = false;
		}
	}

	async function loadEvents(sessionId: string) {
		const generation = ++eventGeneration;
		if (!sessionId) {
			events = [];
			return;
		}
		try {
			const result = await getWorkbenchSessionEvents(sessionId, 0, 200);
			if (generation !== eventGeneration || selectedSessionId !== sessionId) return;
			events = [...result.events].sort((a, b) => b.sequence - a.sequence);
		} catch (cause) {
			if (generation !== eventGeneration || selectedSessionId !== sessionId) return;
			error = message(cause);
		}
	}

	async function selectSession(sessionId: string) {
		selectedSessionId = sessionId;
		renameDraft = sessions.find((item) => item.session_id === sessionId)?.name ?? '';
		await loadEvents(sessionId);
	}

	async function rename() {
		if (!selected || !renameDraft.trim() || renameDraft.trim() === selected.name) return;
		busy = 'rename';
		try {
			await renameWorkbenchSession(selected.session_id, renameDraft.trim());
			await load(true);
			toast.success('Workbench renamed');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	async function archive() {
		if (!selected) return;
		const confirmed = window.confirm(
			'Archive this Workbench session? Active Workbench authority is revoked, but linked CPTR work is preserved.'
		);
		if (!confirmed) return;
		busy = 'archive';
		try {
			await archiveWorkbenchSession(selected.session_id);
			await load(false);
			toast.success('Workbench archived');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	$effect(() => {
		if (!stale) {
			attemptedStaleRefresh = false;
			return;
		}
		if (attemptedStaleRefresh || loading || busy) return;
		attemptedStaleRefresh = true;
		void load(true);
	});

	onMount(() => {
		void load(false);
	});

	onDestroy(() => {
		loadGeneration += 1;
		eventGeneration += 1;
	});
</script>

<section class="space-y-4" aria-label="Workspace Workbench sessions">
	<div class="workbench-heading">
		<div>
			<h3>Workbench sessions</h3>
			<p>
				Owner-scoped CPTR Workbench state for this Workspace. Session events are server-redacted and
				bounded.
			</p>
		</div>
		<button class="secondary" disabled={loading || Boolean(busy)} onclick={() => void load(true)}>
			{loading ? 'Refreshing…' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="workbench-error" role="alert">{error}</div>
	{/if}

	<div class="workbench-layout">
		<aside class="session-list" aria-label="Workbench sessions">
			<div class="session-list-header">
				<strong>{sessions.length}</strong>
				<span>active Workspace session{sessions.length === 1 ? '' : 's'}</span>
			</div>
			{#if sessions.length === 0}
				<div class="empty">No active Workbench sessions are bound to this Workspace.</div>
			{:else}
				{#each sessions as session (session.session_id)}
					<button
						class="session-row"
						class:selected={session.session_id === selectedSessionId}
						aria-pressed={session.session_id === selectedSessionId}
						onclick={() => void selectSession(session.session_id)}
					>
						<div class="session-row-top">
							<strong>{session.name}</strong>
							<span class="status-chip" data-status={session.status}>{session.status}</span>
						</div>
						<p>
							{session.active_target_type && session.active_target_id
								? session.active_target_type + ' · ' + session.active_target_id.slice(0, 10)
								: 'No active target'}
						</p>
						<small>{session.event_count} events · updated {time(session.updated_at)}</small>
					</button>
				{/each}
			{/if}
		</aside>

		<div class="session-detail">
			{#if !selected}
				<div class="empty">Select a Workbench session to inspect its state and activity.</div>
			{:else}
				<div class="detail-heading">
					<div class="min-w-0">
						<div class="flex flex-wrap items-center gap-2">
							<h4>{selected.name}</h4>
							<span class="status-chip" data-status={selected.status}>{selected.status}</span>
						</div>
						<p class="mono">{selected.session_id}</p>
					</div>
					<button class="danger" disabled={Boolean(busy)} onclick={() => void archive()}>
						{busy === 'archive' ? 'Archiving…' : 'Archive'}
					</button>
				</div>

				<div class="session-stats">
					<div><span>Events</span><strong>{selected.event_count}</strong></div>
					<div><span>Target</span><strong>{selected.active_target_type ?? 'none'}</strong></div>
					<div>
						<span>Environment</span><strong>{selected.environment_profile_id ?? 'default'}</strong>
					</div>
					<div><span>Admin role</span><strong>{selected.admin_role ?? 'none'}</strong></div>
				</div>

				<div class="rename-row">
					<label>
						<span>Session name</span>
						<input bind:value={renameDraft} maxlength="120" />
					</label>
					<button
						class="secondary"
						disabled={Boolean(busy) || !renameDraft.trim() || renameDraft.trim() === selected.name}
						onclick={() => void rename()}
					>
						{busy === 'rename' ? 'Saving…' : 'Rename'}
					</button>
				</div>

				<div class="activity-header">
					<div>
						<h5>Session activity</h5>
						<p>{events.length} bounded event{events.length === 1 ? '' : 's'} loaded</p>
					</div>
					<button class="secondary compact" onclick={() => void loadEvents(selected.session_id)}>
						Refresh events
					</button>
				</div>

				{#if events.length === 0}
					<div class="empty">No Workbench events recorded yet.</div>
				{:else}
					<div class="event-list">
						{#each events as event (event.event_id)}
							<article class="event-row">
								<div class="sequence">#{event.sequence}</div>
								<div class="min-w-0 flex-1">
									<div class="event-title">
										<strong>{label(event.event_type)}</strong>
										{#if event.state}<span class="status-chip">{event.state}</span>{/if}
										{#if event.tool_name}<span class="status-chip">{event.tool_name}</span>{/if}
									</div>
									<p>{event.summary}</p>
									<small>{event.source} · {event.actor} · {time(event.created_at)}</small>
								</div>
							</article>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	</div>
</section>

<style>
	.workbench-heading,
	.detail-heading,
	.activity-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.8rem;
	}

	.workbench-heading h3 {
		font-size: 1rem;
		font-weight: 650;
	}

	.workbench-heading p,
	.detail-heading p,
	.activity-header p {
		margin-top: 0.2rem;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	.workbench-layout {
		display: grid;
		grid-template-columns: minmax(14rem, 0.38fr) minmax(0, 1fr);
		gap: 0.8rem;
		align-items: start;
	}

	.session-list,
	.session-detail {
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
	}

	.session-list-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		border-bottom: 1px solid var(--app-border);
		padding: 0.65rem 0.75rem;
	}

	.session-list-header strong {
		font-size: 0.9rem;
	}

	.session-list-header span,
	.session-row p,
	.session-row small,
	.event-row p,
	.event-row small,
	.rename-row label > span {
		font-size: 0.64rem;
		color: var(--app-muted-fg);
	}

	.session-row {
		display: block;
		width: 100%;
		min-height: 4.5rem;
		padding: 0.7rem 0.75rem;
		text-align: left;
	}

	.session-row + .session-row {
		border-top: 1px solid var(--app-border);
	}

	.session-row:hover,
	.session-row.selected {
		background: var(--app-hover);
	}

	.session-row-top,
	.event-title {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.45rem;
	}

	.session-row strong,
	.detail-heading h4,
	.activity-header h5,
	.event-row strong {
		font-size: 0.75rem;
		font-weight: 650;
	}

	.session-row p,
	.session-row small,
	.event-row p,
	.event-row small {
		display: block;
		margin-top: 0.22rem;
	}

	.status-chip {
		display: inline-flex;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.08rem 0.38rem;
		font-size: 0.58rem;
		color: var(--app-muted-fg);
		white-space: nowrap;
	}

	.status-chip[data-status='OPEN'],
	.status-chip[data-status='RUNNING'],
	.status-chip[data-status='COMPLETE'] {
		border-color: rgb(34 197 94 / 0.28);
		color: rgb(34 197 94);
	}

	.status-chip[data-status='FAILED'],
	.status-chip[data-status='ERROR'] {
		border-color: rgb(239 68 68 / 0.28);
		color: rgb(239 68 68);
	}

	.session-stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		border-top: 1px solid var(--app-border);
	}

	.session-stats > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.22rem;
		padding: 0.7rem;
	}

	.session-stats > div + div {
		border-left: 1px solid var(--app-border);
	}

	.session-stats span {
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.session-stats strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.73rem;
		white-space: nowrap;
	}

	.detail-heading,
	.activity-header,
	.rename-row {
		padding: 0.75rem 0.85rem;
	}

	.rename-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: end;
		gap: 0.6rem;
		border-top: 1px solid var(--app-border);
	}

	.rename-row label {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	.rename-row input {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.6rem;
		background: var(--app-bg);
		padding: 0.5rem 0.62rem;
		font-size: 0.74rem;
		outline: none;
	}

	.activity-header {
		border-top: 1px solid var(--app-border);
		border-bottom: 1px solid var(--app-border);
	}

	.event-row {
		display: flex;
		gap: 0.7rem;
		padding: 0.72rem 0.85rem;
	}

	.event-row + .event-row {
		border-top: 1px solid var(--app-border);
	}

	.sequence {
		min-width: 2.6rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.64rem;
		color: var(--app-muted-fg);
	}

	.secondary,
	.danger {
		min-height: 2.75rem;
		border-radius: 0.62rem;
		padding: 0.45rem 0.72rem;
		font-size: 0.7rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.danger {
		border: 1px solid rgb(239 68 68 / 0.35);
		color: rgb(239 68 68);
	}

	.secondary:disabled,
	.danger:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.compact {
		min-height: 2.25rem;
		padding: 0.3rem 0.52rem;
		font-size: 0.64rem;
	}

	.empty {
		padding: 1rem;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	.workbench-error {
		border: 1px solid rgb(239 68 68 / 0.28);
		border-radius: 0.75rem;
		padding: 0.75rem;
		font-size: 0.72rem;
		color: rgb(239 68 68);
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		word-break: break-all;
	}

	@media (max-width: 820px) {
		.workbench-layout {
			grid-template-columns: 1fr;
		}

		.session-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.session-stats > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.session-stats > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}
	}

	@media (max-width: 540px) {
		.workbench-heading,
		.detail-heading,
		.activity-header {
			align-items: stretch;
			flex-direction: column;
		}

		.rename-row {
			grid-template-columns: 1fr;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.session-row,
		.secondary,
		.danger {
			transition: none;
		}
	}
</style>
