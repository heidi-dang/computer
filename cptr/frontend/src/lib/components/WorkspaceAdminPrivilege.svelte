<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		getWorkbenchPrivilege,
		grantWorkbenchAdmin,
		listWorkbenchSessions,
		revokeWorkbenchAdmin,
		type WorkbenchPrivilegeView,
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
	let privilege = $state<WorkbenchPrivilegeView | null>(null);
	let ttlMinutes = $state(15);
	let loading = $state(false);
	let busy = $state('');
	let error = $state('');
	let loadGeneration = 0;
	let privilegeGeneration = 0;
	let attemptedStaleRefresh = false;

	const selected = $derived(
		sessions.find((session) => session.session_id === selectedSessionId) ?? null
	);

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Privilege operation failed';
	}

	function belongsToWorkspace(session: WorkbenchSessionView): boolean {
		return session.workspace_id === workspaceId || session.active_workspace_id === workspaceId;
	}

	function time(value: number | null): string {
		if (!value) return 'Not active';
		return new Date(value).toLocaleString();
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
				await loadPrivilege(selectedSessionId);
			} else {
				privilege = null;
			}
			onfresh();
		} catch (cause) {
			if (generation !== loadGeneration) return;
			error = message(cause);
		} finally {
			if (generation === loadGeneration) loading = false;
		}
	}

	async function loadPrivilege(sessionId: string) {
		const generation = ++privilegeGeneration;
		if (!sessionId) {
			privilege = null;
			return;
		}
		try {
			const result = await getWorkbenchPrivilege(sessionId);
			if (generation !== privilegeGeneration || selectedSessionId !== sessionId) return;
			privilege = result;
		} catch (cause) {
			if (generation !== privilegeGeneration || selectedSessionId !== sessionId) return;
			error = message(cause);
		}
	}

	async function selectSession(sessionId: string) {
		selectedSessionId = sessionId;
		await loadPrivilege(sessionId);
	}

	async function grantAdmin() {
		if (!selected) return;
		const minutes = Math.max(1, Math.min(480, Math.round(Number(ttlMinutes) || 15)));
		const confirmed = window.confirm(
			'Grant ADMIN to this Workbench for ' +
				minutes +
				' minute' +
				(minutes === 1 ? '' : 's') +
				'? This is session-scoped and does not grant ROOT.'
		);
		if (!confirmed) return;
		busy = 'grant';
		try {
			await grantWorkbenchAdmin(selected.session_id, minutes * 60);
			await loadPrivilege(selected.session_id);
			toast.success('Workbench ADMIN granted');
			onfresh();
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	async function revokeAdmin() {
		if (!selected) return;
		const confirmed = window.confirm(
			'Revoke ADMIN from this Workbench now? Any separate ROOT grant remains governed by the local-root broker.'
		);
		if (!confirmed) return;
		busy = 'revoke';
		try {
			await revokeWorkbenchAdmin(selected.session_id);
			await loadPrivilege(selected.session_id);
			toast.success('Workbench ADMIN revoked');
			onfresh();
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
		privilegeGeneration += 1;
	});
</script>

<section class="space-y-4" aria-label="Workspace privilege broker">
	<div class="admin-heading">
		<div>
			<h3>Admin privilege</h3>
			<p>
				Temporary Workbench-scoped ADMIN authority. ROOT remains a separate prompt-scoped/local-root
				grant and cannot be created from this screen.
			</p>
		</div>
		<button class="secondary" disabled={loading || Boolean(busy)} onclick={() => void load(true)}>
			{loading ? 'Refreshing…' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="admin-error" role="alert">{error}</div>
	{/if}

	<div class="admin-grid">
		<div class="session-picker">
			<label for="admin-session">Workbench session</label>
			<select
				id="admin-session"
				value={selectedSessionId}
				onchange={(event) => void selectSession(event.currentTarget.value)}
			>
				{#if sessions.length === 0}
					<option value="">No active Workbench sessions</option>
				{:else}
					{#each sessions as session (session.session_id)}
						<option value={session.session_id}>
							{session.name} · {session.session_id.slice(0, 10)}
						</option>
					{/each}
				{/if}
			</select>
			<small>
				Only active sessions currently bound to this Workspace are shown. Archived sessions cannot
				retain reusable ADMIN authority.
			</small>
		</div>

		<div class="privilege-card">
			<span>Effective privilege</span>
			<strong data-level={privilege?.privilege ?? 'NORMAL'}
				>{privilege?.privilege ?? 'NORMAL'}</strong
			>
			<small>
				{privilege?.local_root_grant_active
					? 'A separate local ROOT grant is currently active.'
					: 'No local ROOT grant is active.'}
			</small>
		</div>
	</div>

	{#if selected}
		<div class="privilege-stats">
			<div>
				<span>ADMIN grant</span>
				<strong>{privilege?.admin_grant.active ? 'Active' : 'Inactive'}</strong>
			</div>
			<div>
				<span>Expires</span>
				<strong>{time(privilege?.admin_grant.expires_at ?? null)}</strong>
			</div>
			<div>
				<span>Remaining</span>
				<strong>{privilege?.admin_grant.remaining_seconds ?? 0}s</strong>
			</div>
			<div>
				<span>Session state</span>
				<strong>{selected.status}</strong>
			</div>
		</div>

		<div class="grant-panel">
			<div>
				<h4>Temporary ADMIN grant</h4>
				<p>
					ADMIN is bounded to this Workbench session. The broker still intersects ownership, session
					state, endpoint scopes and all existing guard policy.
				</p>
			</div>
			<div class="grant-controls">
				<label>
					<span>TTL minutes</span>
					<input type="number" min="1" max="480" step="1" bind:value={ttlMinutes} />
				</label>
				{#if privilege?.admin_grant.active}
					<button class="danger" disabled={Boolean(busy)} onclick={() => void revokeAdmin()}>
						{busy === 'revoke' ? 'Revoking…' : 'Revoke ADMIN'}
					</button>
				{:else}
					<button class="primary" disabled={Boolean(busy)} onclick={() => void grantAdmin()}>
						{busy === 'grant' ? 'Granting…' : 'Grant ADMIN'}
					</button>
				{/if}
			</div>
		</div>

		<div class="boundary">
			<strong>Security boundary</strong>
			<ul>
				<li>ADMIN automatically expires and is revoked when the owning Workbench is archived.</li>
				<li>
					ADMIN does not bypass workspace ownership, API scopes, guard controls or command policy.
				</li>
				<li>
					ROOT is stronger than ADMIN and requires the separate host-enabled local-root grant path.
				</li>
			</ul>
		</div>
	{:else}
		<div class="empty">
			Open a Workbench session for this Workspace before granting ADMIN authority.
		</div>
	{/if}
</section>

<style>
	.admin-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.8rem;
	}

	.admin-heading h3 {
		font-size: 1rem;
		font-weight: 650;
	}

	.admin-heading p,
	.session-picker small,
	.privilege-card small,
	.grant-panel p,
	.boundary li {
		margin-top: 0.2rem;
		font-size: 0.7rem;
		color: var(--app-muted-fg);
		line-height: 1.45;
	}

	.admin-grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(12rem, 0.35fr);
		gap: 0.8rem;
	}

	.session-picker,
	.privilege-card,
	.grant-panel,
	.boundary {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		padding: 0.8rem;
	}

	.session-picker {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}

	.session-picker label,
	.grant-controls label > span,
	.privilege-card > span,
	.privilege-stats span {
		font-size: 0.63rem;
		color: var(--app-muted-fg);
	}

	.session-picker select,
	.grant-controls input {
		width: 100%;
		min-height: 2.75rem;
		border: 1px solid var(--app-border);
		border-radius: 0.62rem;
		background: var(--app-bg);
		padding: 0.5rem 0.65rem;
		font-size: 0.74rem;
	}

	.privilege-card {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.privilege-card strong {
		font-size: 1.1rem;
	}

	.privilege-card strong[data-level='ADMIN'] {
		color: rgb(234 179 8);
	}

	.privilege-card strong[data-level='ROOT'] {
		color: rgb(239 68 68);
	}

	.privilege-stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
	}

	.privilege-stats > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.22rem;
		padding: 0.72rem;
	}

	.privilege-stats > div + div {
		border-left: 1px solid var(--app-border);
	}

	.privilege-stats strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.72rem;
		white-space: nowrap;
	}

	.grant-panel {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 1rem;
		align-items: end;
	}

	.grant-panel h4,
	.boundary strong {
		font-size: 0.76rem;
		font-weight: 650;
	}

	.grant-controls {
		display: flex;
		align-items: end;
		gap: 0.55rem;
	}

	.grant-controls label {
		display: flex;
		width: 7rem;
		flex-direction: column;
		gap: 0.3rem;
	}

	.boundary ul {
		margin-top: 0.45rem;
		padding-left: 1rem;
		list-style: disc;
	}

	.boundary li + li {
		margin-top: 0.22rem;
	}

	.primary,
	.secondary,
	.danger {
		min-height: 2.75rem;
		border-radius: 0.62rem;
		padding: 0.45rem 0.72rem;
		font-size: 0.7rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}

	.secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.danger {
		border: 1px solid rgb(239 68 68 / 0.35);
		color: rgb(239 68 68);
	}

	.primary:disabled,
	.secondary:disabled,
	.danger:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.admin-error {
		border: 1px solid rgb(239 68 68 / 0.28);
		border-radius: 0.75rem;
		padding: 0.75rem;
		font-size: 0.72rem;
		color: rgb(239 68 68);
	}

	.empty {
		border: 1px dashed var(--app-border);
		border-radius: 0.85rem;
		padding: 1rem;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 760px) {
		.admin-grid,
		.grant-panel {
			grid-template-columns: 1fr;
		}

		.privilege-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.privilege-stats > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.privilege-stats > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}

		.grant-controls {
			align-items: stretch;
			flex-direction: column;
		}

		.grant-controls label {
			width: 100%;
		}
	}

	@media (max-width: 540px) {
		.admin-heading {
			align-items: stretch;
			flex-direction: column;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.primary,
		.secondary,
		.danger {
			transition: none;
		}
	}
</style>
