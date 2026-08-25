<script lang="ts">
	import { onMount } from 'svelte';
	import { ApiError } from '$lib/apis';
	import {
		cancelFlowDeckOrchestration,
		createFlowDeckOrchestration,
		getFlowDeckOrchestration,
		type FlowDeckOrchestration
	} from '$lib/apis/flowdeck';
	import Icon from '$lib/components/Icon.svelte';
import ManagedRuntimePanel from '$lib/components/ManagedRuntimePanel.svelte';
import ProjectDatabasePanel from '$lib/components/ProjectDatabasePanel.svelte';
	import { currentWorkspace, workspaceList } from '$lib/stores';
	import { chatModels, defaultModel } from '$lib/stores/chat';

	type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'error';
	type RunKind = 'success' | 'failure' | 'cancelled' | 'manual' | 'active' | 'unknown';

	let workspace = $state('');
	let objective = $state('');
	let run = $state<FlowDeckOrchestration | null>(null);
	let runId = $state('');
	let runWorkspace = $state('');
	let runObjective = $state('');
	let connectionState = $state<ConnectionState>('connecting');
	let lastCheckedAt = $state<Date | null>(null);
	let startError = $state('');
	let pollError = $state('');
	let orchestrationDisabled = $state(false);
	let isStarting = $state(false);
	let isCancelling = $state(false);
	let confirmCancel = $state(false);
	let hydrated = $state(false);
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let requestInFlight = false;

	const STORAGE_KEY = 'flowdeck:owned-run';
	const terminalStatuses = new Set([
		'succeeded',
		'success',
		'completed',
		'failed',
		'error',
		'cancelled',
		'canceled',
		'outcome_unknown',
		'manual_review',
		'manual_review_required',
		'approval_required',
		'review_required'
	]);

	let modelLabel = $derived(
		$defaultModel
			? ($chatModels.find((model) => model.id === $defaultModel)?.name ?? $defaultModel)
			: 'Not reported'
	);
	let selectedWorkspaceName = $derived(
		$workspaceList.find((item) => item.path === workspace)?.name ??
			($currentWorkspace?.path === workspace ? $currentWorkspace.name : workspace || 'Choose a workspace')
	);
	let runKind = $derived(classifyRun(run));
	let statusLabel = $derived(statusText(runKind, run));
	let isTerminal = $derived(runKind === 'success' || runKind === 'failure' || runKind === 'cancelled');
	let hasRun = $derived(Boolean(runId));
	let reconnectLabel = $derived(
		connectionState === 'connected'
			? 'Connected'
			: connectionState === 'reconnecting'
				? 'Reconnecting'
				: connectionState === 'offline'
					? 'Offline'
					: connectionState === 'error'
						? 'Unavailable'
						: 'Connecting'
	);

	function getRunId(value: FlowDeckOrchestration | null): string {
		if (!value) return '';
		return typeof value.run_id === 'string' ? value.run_id : typeof value.id === 'string' ? value.id : '';
	}

	function getStatus(value: FlowDeckOrchestration | null): string {
		const raw = value?.status ?? value?.state;
		return typeof raw === 'string' ? raw.toLowerCase().replaceAll('-', '_') : '';
	}

	function classifyRun(value: FlowDeckOrchestration | null): RunKind {
		if (!value) return 'unknown';
		const status = getStatus(value);
		if (['succeeded', 'success', 'completed'].includes(status)) return 'success';
		if (['failed', 'error'].includes(status)) return 'failure';
		if (['cancelled', 'canceled'].includes(status)) return 'cancelled';
		if (
			['manual_review', 'manual_review_required', 'approval_required', 'review_required'].includes(
				status
			)
		)
			return 'manual';
		if (status === 'outcome_unknown' || status === 'unknown') return 'unknown';
		if (!status) return 'unknown';
		return terminalStatuses.has(status) ? 'unknown' : 'active';
	}

	function statusText(kind: RunKind, value: FlowDeckOrchestration | null): string {
		if (kind === 'success') return 'Completed';
		if (kind === 'failure') return 'Failed';
		if (kind === 'cancelled') return 'Cancelled';
		if (kind === 'manual') return 'Manual review';
		if (kind === 'active') return value?.status ?? value?.state ?? 'In progress';
		return value ? 'State not reported' : 'Ready to coordinate';
	}

	function describeState(kind: RunKind): string {
		if (kind === 'success') return 'The controlled run reached a reported successful state.';
		if (kind === 'failure') return 'The backend reported that this run did not complete.';
		if (kind === 'cancelled') return 'The run accepted the cancellation request.';
		if (kind === 'manual') return 'The backend is asking for a deliberate human decision.';
		if (kind === 'active') return 'FlowDeck is coordinating this objective. This view refreshes automatically.';
		return 'Waiting for a truthful state from the orchestration service.';
	}

	function safeStringify(value: unknown): string {
		if (value === undefined || value === null || value === '') return 'Not reported';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2) ?? 'Not reported';
		} catch {
			return String(value);
		}
	}

	function field(key: string): unknown {
		if (!run || !Object.prototype.hasOwnProperty.call(run, key)) return undefined;
		return run[key];
	}

	function activityItems(value: FlowDeckOrchestration | null): unknown[] {
		if (!value) return [];
		const items = [
			...arrayFieldFrom(value, ['events', 'activity', 'event_log']),
			...arrayFieldFrom(value, ['evidence'])
		];
		const seen = new Set<string>();
		return items.filter((item) => {
			const key = safeStringify(item);
			if (seen.has(key)) return false;
			seen.add(key);
			return true;
		});
	}

	function arrayFieldFrom(value: FlowDeckOrchestration, keys: string[]): unknown[] {
		for (const key of keys) {
			const candidate = value[key];
			if (Array.isArray(candidate)) return candidate;
		}
		return [];
	}

	function formatTime(value: unknown): string {
		if (typeof value !== 'string' && typeof value !== 'number') return 'Not reported';
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
	}

	function eventTitle(item: unknown): string {
		if (!item || typeof item !== 'object') return String(item);
		const record = item as Record<string, unknown>;
		for (const key of ['title', 'name', 'type', 'event', 'message']) {
			if (typeof record[key] === 'string' && record[key]) return record[key] as string;
		}
		return 'Observed activity';
	}

	function eventDetail(item: unknown): string {
		if (!item || typeof item !== 'object') return '';
		const record = item as Record<string, unknown>;
		for (const key of ['message', 'detail', 'description', 'status']) {
			if (typeof record[key] === 'string' && record[key]) return record[key] as string;
		}
		return '';
	}

	function objectiveText(): string {
		if (runObjective) return runObjective;
		const reported = field('objective');
		return typeof reported === 'string' ? reported : 'Objective not reported';
	}

	function reportedNumber(keys: string[]): number | null {
		for (const key of keys) {
			const value = field(key);
			if (typeof value === 'number' && Number.isFinite(value)) return value;
		}
		return null;
	}

	function tokenSummary(): string {
		const total = reportedNumber(['total_tokens', 'tokens', 'token_count', 'cumulative_tokens']);
		return total === null ? 'Not reported' : total.toLocaleString();
	}

	function progressSummary(): string {
		const completed = reportedNumber(['completed_steps', 'steps_completed']);
		const planned = reportedNumber(['total_steps', 'planned_steps', 'steps_total']);
		if (completed !== null && planned !== null && planned > 0) {
			return `${Math.min(completed, planned)} / ${planned} steps`;
		}
		const percent = reportedNumber(['progress_percent', 'progress_percentage']);
		if (percent !== null && percent >= 0 && percent <= 100) return `${Math.round(percent)}%`;
		return 'Indeterminate';
	}

	function currentAgent(): string {
		for (const key of ['current_agent', 'current_specialist', 'active_specialist', 'agent']) {
			const value = field(key);
			if (typeof value === 'string' && value.trim()) return value;
		}
		return 'Not reported';
	}

	function persistOwnedRun() {
		if (typeof sessionStorage === 'undefined') return;
		if (!runId) {
			sessionStorage.removeItem(STORAGE_KEY);
			return;
		}
		sessionStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({ runId, workspace: runWorkspace, objective: runObjective })
		);
	}

	function updateRunUrl() {
		if (typeof window === 'undefined') return;
		const url = new URL(window.location.href);
		if (runId) {
			url.searchParams.set('run_id', runId);
			url.searchParams.set('workspace', runWorkspace);
		} else {
			url.searchParams.delete('run_id');
		}
		window.history.replaceState({}, '', url.toString());
	}

	function stopPolling() {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	}

	function startPolling() {
		stopPolling();
		if (!runId || isTerminal) return;
		pollTimer = setInterval(() => void refreshRun(), 3000);
	}

	async function refreshRun() {
		if (!runId || !runWorkspace || requestInFlight) return;
		requestInFlight = true;
		if (connectionState !== 'connected') connectionState = 'reconnecting';
		try {
			const next = await getFlowDeckOrchestration(runId, runWorkspace);
			if (getRunId(next) && getRunId(next) !== runId) return;
			run = next;
			pollError = '';
			connectionState = 'connected';
			lastCheckedAt = new Date();
			if (classifyRun(next) !== 'active') stopPolling();
			persistOwnedRun();
		} catch (error) {
			pollError = error instanceof Error ? error.message : 'Unable to read the current run state.';
			connectionState = navigator.onLine === false ? 'offline' : 'error';
		} finally {
			requestInFlight = false;
		}
	}

	function resetComposer() {
		run = null;
		runId = '';
		runWorkspace = '';
		runObjective = '';
		objective = '';
		confirmCancel = false;
		startError = '';
		pollError = '';
		orchestrationDisabled = false;
		stopPolling();
		persistOwnedRun();
		updateRunUrl();
	}

	async function startOrchestration(event: SubmitEvent) {
		event.preventDefault();
		if (!workspace || !objective.trim() || isStarting) return;
		isStarting = true;
		startError = '';
		orchestrationDisabled = false;
		try {
			const idempotencyKey =
				typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
					? crypto.randomUUID()
					: `flowdeck-${Date.now()}-${Math.random().toString(36).slice(2)}`;
			const created = await createFlowDeckOrchestration(
				{ workspace, objective: objective.trim() },
				idempotencyKey
			);
			const returnedId = getRunId(created);
			if (!returnedId) throw new Error('The orchestration service did not return a run_id.');
			run = created;
			runId = returnedId;
			runWorkspace = workspace;
			runObjective = objective.trim();
			connectionState = 'connected';
			lastCheckedAt = new Date();
			persistOwnedRun();
			updateRunUrl();
			startPolling();
		} catch (error) {
			if (error instanceof ApiError && [404, 405, 501].includes(error.status)) {
				orchestrationDisabled = true;
			}
			startError =
				error instanceof Error
					? error.message
					: 'Controlled orchestration could not be started. Nothing was launched.';
		} finally {
			isStarting = false;
		}
	}

	async function cancelRun() {
		if (!runId || !runWorkspace || isCancelling) return;
		isCancelling = true;
		pollError = '';
		try {
			const cancelled = await cancelFlowDeckOrchestration(runId, runWorkspace);
			run = { ...run, ...cancelled };
			confirmCancel = false;
			connectionState = 'connected';
			lastCheckedAt = new Date();
			if (classifyRun(run) === 'cancelled') stopPolling();
			persistOwnedRun();
		} catch (error) {
			pollError = error instanceof Error ? error.message : 'The cancellation request was not accepted.';
		} finally {
			isCancelling = false;
		}
	}

	function hydrateOwnedRun() {
		if (typeof window === 'undefined') return;
		const params = new URLSearchParams(window.location.search);
		const queryRunId = params.get('run_id');
		const queryWorkspace = params.get('workspace');
		let stored: { runId?: string; workspace?: string; objective?: string } = {};
		try {
			stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}');
		} catch {
			stored = {};
		}
		const ownedId = queryRunId || stored.runId;
		const ownedWorkspace = queryWorkspace || stored.workspace;
		if (ownedId && ownedWorkspace) {
			runId = ownedId;
			runWorkspace = ownedWorkspace;
			runObjective = stored.objective ?? '';
			workspace = ownedWorkspace;
			connectionState = 'reconnecting';
			updateRunUrl();
			void refreshRun();
		} else {
			connectionState = 'connected';
		}
		hydrated = true;
	}

	onMount(() => {
		hydrateOwnedRun();
		const onOnline = () => {
			if (runId) {
				connectionState = 'reconnecting';
				void refreshRun();
			} else connectionState = 'connected';
		};
		const onOffline = () => {
			connectionState = 'offline';
		};
		window.addEventListener('online', onOnline);
		window.addEventListener('offline', onOffline);
		return () => {
			stopPolling();
			window.removeEventListener('online', onOnline);
			window.removeEventListener('offline', onOffline);
		};
	});

	$effect(() => {
		if (!workspace && ($currentWorkspace?.path || $workspaceList[0]?.path)) {
			workspace = $currentWorkspace?.path ?? $workspaceList[0]?.path ?? '';
		}
	});

	$effect(() => {
		if (hydrated && runId && run) startPolling();
	});
</script>

<svelte:head>
	<title>FlowDeck / Computer</title>
	<meta
		name="description"
		content="A careful orchestration cockpit for controlled work in Computer."
	/>
</svelte:head>

<div class="flowdeck-shell">
	<header class="flowdeck-header">
		<div class="header-brand">
			<div class="brand-mark" aria-hidden="true"><Icon name="gateway" size={17} strokeWidth={1.8} /></div>
			<div>
				<div class="brand-name">FlowDeck</div>
				<div class="brand-kicker">controlled orchestration</div>
			</div>
		</div>
		<div class="header-status" aria-live="polite">
			<span class:status-pulse={connectionState === 'connected' && !isTerminal} class="status-dot"></span>
			<span>{reconnectLabel}</span>
			<span class="status-divider"></span>
			<span class="header-context">{selectedWorkspaceName}</span>
		</div>
	</header>

	<main class="flowdeck-main">
		{#if !hasRun}
			<section class="welcome-grid">
				<div class="welcome-copy">
					<div class="eyebrow"><span class="eyebrow-line"></span> live computer coordination</div>
					<h1>Give the work<br /><em>a steady hand.</em></h1>
					<p class="welcome-lede">
						FlowDeck turns a clear objective into a controlled run across your workspace. You stay
						in the loop; the backend stays the source of truth.
					</p>
					<div class="trust-notes">
						<div><Icon name="shield" size={15} /><span>Scoped to one workspace</span></div>
						<div><Icon name="refresh" size={15} /><span>State rehydrates after reconnect</span></div>
					</div>
				</div>

				<form class="composer-card" action="javascript:void(0)" onsubmit={startOrchestration}>
					<div class="composer-topline">
						<div>
							<div class="section-label">New controlled run</div>
							<h2>What should Computer coordinate?</h2>
						</div>
						<div class="composer-orbit" aria-hidden="true"><span></span><span></span><span></span></div>
					</div>
					<label for="flowdeck-workspace">Workspace</label>
					<div class="select-wrap">
						<Icon name="folder" size={16} />
						<select id="flowdeck-workspace" bind:value={workspace} required>
							<option value="" disabled>Select an existing workspace</option>
							{#each $workspaceList as item}
								<option value={item.path}>{item.name} · {item.path}</option>
							{/each}
							{#if workspace && !$workspaceList.some((item) => item.path === workspace)}
								<option value={workspace}>{selectedWorkspaceName} · {workspace}</option>
							{/if}
						</select>
						<Icon name="chevron-down" size={15} />
					</div>
					{#if !$workspaceList.length && !$currentWorkspace}
						<div class="workspace-empty" role="status">
							<Icon name="info" size={14} /> No existing workspaces are available in this session.
						</div>
					{/if}
					<label for="flowdeck-objective">Objective</label>
					<textarea
						id="flowdeck-objective"
						bind:value={objective}
						placeholder="Describe the outcome, constraints, and what a safe finish looks like."
						rows="5"
						required
					></textarea>
					<div class="composer-foot">
						<div class="context-note">
							<Icon name="cube" size={14} />
							<span>Model context: {modelLabel}</span>
						</div>
						<button class="start-button" type="submit" disabled={isStarting || !workspace || !objective.trim()}>
							{#if isStarting}
								<span class="button-sheen">Starting</span>
							{:else}
								<span>Start run</span><Icon name="play" size={15} />
							{/if}
						</button>
					</div>
					{#if startError}
						<div class="inline-error" role="alert">
							<Icon name="info" size={15} />
							<span>{startError}</span>
						</div>
					{/if}
					{#if orchestrationDisabled}
						<div class="disabled-note" role="status">
							Controlled orchestration is not enabled on this Computer instance. No run was created.
						</div>
					{/if}
				</form>
			</section>

			<section class="empty-lower">
				<div class="empty-rule"></div>
				<div class="empty-lower-copy">
					<span class="mono">FLOWDECK / READY</span>
					<span>One objective. One owned run. No hidden progress.</span>
				</div>
			</section>
		{:else}
			<section class="run-heading">
				<div>
					<div class="eyebrow"><span class="eyebrow-line"></span> owned orchestration</div>
					<h1>{statusLabel}</h1>
					<p class="run-objective">{objectiveText()}</p>
				</div>
				<div class="run-actions">
					<div class="run-id-block">
						<span>RUN ID</span>
						<code title={runId}>{runId}</code>
					</div>
					<button class="quiet-button" onclick={() => void refreshRun()} disabled={connectionState === 'reconnecting'}>
						<Icon name="refresh" size={15} /> Refresh
					</button>
					{#if !isTerminal && runKind !== 'manual'}
						{#if confirmCancel}
							<div class="cancel-confirm" role="alertdialog" aria-label="Confirm cancellation">
								<span>Stop this run?</span>
								<button class="confirm-stop" onclick={() => void cancelRun()} disabled={isCancelling}>
									{isCancelling ? 'Stopping…' : 'Confirm'}
								</button>
								<button class="cancel-dismiss" onclick={() => (confirmCancel = false)}>Keep running</button>
							</div>
						{:else}
							<button class="cancel-button" onclick={() => (confirmCancel = true)}>
								<Icon name="xmark" size={15} /> Cancel
							</button>
						{/if}
					{/if}
				</div>
			</section>

			{#if pollError}
				<div class="reconnect-banner" role="alert">
					<div><Icon name="refresh" size={16} /><span><strong>State refresh interrupted.</strong> {pollError}</span></div>
					<button onclick={() => void refreshRun()}>Try again</button>
				</div>
			{/if}

			<section class="run-grid">
				<div class="run-primary">
					<div class="state-card state-{runKind}">
						<div class="state-card-top">
							<div class="state-icon">
								{#if runKind === 'success'}<Icon name="check" size={20} />
								{:else if runKind === 'failure'}<Icon name="xmark" size={20} />
								{:else if runKind === 'cancelled'}<Icon name="xmark" size={20} />
								{:else if runKind === 'manual'}<Icon name="eye" size={20} />
								{:else}<Icon name="gateway" size={20} />{/if}
							</div>
							<div>
								<div class="section-label">Current state</div>
								<h2>{statusLabel}</h2>
								<p>{describeState(runKind)}</p>
							</div>
							<div class="state-connection">
								<span class:status-pulse={connectionState === 'connected' && !isTerminal} class="status-dot"></span>
								<span>{reconnectLabel}</span>
							</div>
						</div>
						<div class="state-meta">
							<div><span>Workspace</span><strong>{runWorkspace || 'Not reported'}</strong></div>
							<div><span>Last checked</span><strong>{lastCheckedAt ? formatTime(lastCheckedAt) : 'Awaiting first read'}</strong></div>
							<div><span>Backend status</span><strong>{typeof run?.status === 'string' ? run.status : typeof run?.state === 'string' ? run.state : 'Not reported'}</strong></div>
						</div>
					</div>

				<div class="telemetry-grid" aria-label="Live telemetry">
					<div class="telemetry-card">
						<span class="telemetry-label">Progress</span>
						<strong>{progressSummary()}</strong>
						<small>Derived only from reported backend state</small>
					</div>
					<div class="telemetry-card">
						<span class="telemetry-label">Tokens</span>
						<strong>{tokenSummary()}</strong>
						<small>Provider usage; never estimated</small>
					</div>
					<div class="telemetry-card">
						<span class="telemetry-label">Current agent</span>
						<strong>{currentAgent()}</strong>
						<small>Specialist telemetry when available</small>
					</div>
				</div>

					<div class="inspector-grid">
						<article class="inspector-card">
							<div class="inspector-heading"><Icon name="list-ordered" size={16} /><h3>Plan</h3></div>
							{#if field('plan') !== undefined}
								<pre>{safeStringify(field('plan'))}</pre>
							{:else}
								<div class="not-reported"><span></span>Awaiting backend state</div>
							{/if}
						</article>
						<article class="inspector-card">
							<div class="inspector-heading"><Icon name="shield" size={16} /><h3>Scopes</h3></div>
							{#if field('scopes') !== undefined}
								<pre>{safeStringify(field('scopes'))}</pre>
							{:else}
								<div class="not-reported"><span></span>Not reported</div>
							{/if}
						</article>
					</div>

					<article class="activity-card">
						<div class="activity-heading">
							<div class="inspector-heading"><Icon name="clock" size={16} /><h3>Activity</h3></div>
							<span class="telemetry-note">deduplicated observations</span>
						</div>
						{#if activityItems(run).length}
							<div class="activity-list">
								{#each activityItems(run) as item}
									<div class="activity-item">
										<div class="activity-marker"></div>
										<div class="activity-item-copy"><strong>{eventTitle(item)}</strong>{#if eventDetail(item)}<span>{eventDetail(item)}</span>{/if}</div>
										{#if item && typeof item === 'object' && typeof (item as Record<string, unknown>).created_at === 'string'}
											<time>{formatTime((item as Record<string, unknown>).created_at)}</time>
										{/if}
									</div>
								{/each}
							</div>
						{:else}
							<div class="empty-telemetry"><Icon name="clock" size={18} /><span>No activity has been reported yet.</span></div>
						{/if}
					</article>
				</div>

				<aside class="run-side">
					<article class="evidence-card">
						<div class="inspector-heading"><Icon name="eye" size={16} /><h3>Evidence</h3></div>
						{#if field('evidence') !== undefined}
							<pre>{safeStringify(field('evidence'))}</pre>
						{:else}
							<div class="not-reported"><span></span>Not reported</div>
							<p class="side-note">Evidence will appear here when the orchestration backend returns it.</p>
						{/if}
					</article>
					<div class="reconnect-card">
						<div class="reconnect-card-icon"><Icon name="refresh" size={16} /></div>
						<div>
							<strong>Reconnect-safe</strong>
							<p>This run is owned by this browser session. Reloading reads {runId} again; it never starts a second run.</p>
						</div>
					</div>
					<div class="run-footnote">
						<span class="mono">FLOWDECK / {runKind.toUpperCase()}</span>
						<span>Telemetry is shown only when returned by the service.</span>
					</div>
				</aside>
			</section>
		{/if}
<ManagedRuntimePanel workspace={workspace || $currentWorkspace?.path || ''} />
<ProjectDatabasePanel workspace={workspace || $currentWorkspace?.path || ''} />
	</main>
</div>

<style>
	:global(:root) {
		--fd-ink: #25332f;
		--fd-muted: #708078;
		--fd-faint: #a4afa8;
		--fd-line: color-mix(in oklab, var(--app-fg) 12%, transparent);
		--fd-panel: color-mix(in oklab, var(--app-bg) 92%, #dce8d9);
		--fd-panel-strong: color-mix(in oklab, var(--app-bg) 85%, #f0dcc0);
		--fd-teal: #247a6d;
		--fd-teal-deep: #195d55;
		--fd-amber: #b87437;
		--fd-coral: #b75b51;
	}

	:global(.dark) {
		--fd-ink: #e0e9e1;
		--fd-muted: #9aa9a0;
		--fd-faint: #6d7c74;
		--fd-line: color-mix(in oklab, var(--app-fg) 15%, transparent);
		--fd-panel: color-mix(in oklab, var(--app-bg) 88%, #234b43);
		--fd-panel-strong: color-mix(in oklab, var(--app-bg) 84%, #5e4933);
		--fd-teal: #72c1ae;
		--fd-teal-deep: #98d4c3;
		--fd-amber: #dda871;
		--fd-coral: #e28a7d;
	}

	.flowdeck-shell {
		height: 100%;
		overflow: auto;
		background:
			radial-gradient(circle at 86% 10%, color-mix(in oklab, var(--fd-teal) 9%, transparent), transparent 26rem),
			var(--app-bg);
		color: var(--fd-ink);
		font-family: var(--app-ui-font);
	}

	.flowdeck-header {
		position: sticky;
		top: 0;
		z-index: 4;
		display: flex;
		align-items: center;
		justify-content: space-between;
		min-height: 64px;
		padding: 0.8rem clamp(1rem, 3vw, 2.8rem);
		border-bottom: 1px solid var(--fd-line);
		background: color-mix(in oklab, var(--app-bg) 87%, transparent);
		backdrop-filter: blur(18px);
	}

	.header-brand,
	.header-status,
	.state-connection,
	.run-actions,
	.quiet-button,
	.cancel-button,
	.context-note,
	.inspector-heading,
	.trust-notes div,
	.empty-lower-copy,
	.reconnect-card,
	.reconnect-banner > div {
		display: flex;
		align-items: center;
	}

	.header-brand { gap: 0.65rem; }
	.brand-mark {
		display: grid;
		width: 32px;
		height: 32px;
		place-items: center;
		border: 1px solid color-mix(in oklab, var(--fd-teal) 48%, transparent);
		border-radius: 10px;
		color: var(--fd-teal);
		background: color-mix(in oklab, var(--fd-teal) 10%, transparent);
	}
	.brand-name { font-size: 0.84rem; font-weight: 700; letter-spacing: -0.02em; }
	.brand-kicker, .section-label, .eyebrow, .mono, .run-id-block span, .telemetry-note {
		font-family: var(--font-mono);
		text-transform: uppercase;
		letter-spacing: 0.11em;
	}
	.brand-kicker { margin-top: 0.12rem; color: var(--fd-muted); font-size: 0.58rem; }
	.header-status { gap: 0.48rem; color: var(--fd-muted); font-size: 0.7rem; }
	.header-context { max-width: 20rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.status-divider { width: 1px; height: 13px; margin: 0 0.2rem; background: var(--fd-line); }
	.status-dot {
		display: inline-block;
		width: 7px;
		height: 7px;
		border-radius: 99px;
		background: var(--fd-faint);
	}
	.status-pulse { background: var(--fd-teal); box-shadow: 0 0 0 4px color-mix(in oklab, var(--fd-teal) 15%, transparent); }

	.flowdeck-main { width: min(1280px, 100%); margin: 0 auto; padding: clamp(2rem, 6vw, 5rem) clamp(1rem, 4vw, 3.5rem) 5rem; }
	.welcome-grid { display: grid; grid-template-columns: minmax(0, 0.82fr) minmax(320px, 1.18fr); align-items: center; gap: clamp(2rem, 8vw, 8rem); min-height: calc(100dvh - 180px); }
	.welcome-copy { padding: 1rem 0; }
	.eyebrow { display: flex; align-items: center; gap: 0.6rem; color: var(--fd-teal); font-size: 0.62rem; font-weight: 700; }
	.eyebrow-line { display: inline-block; width: 24px; height: 1px; background: var(--fd-teal); }
	h1, h2, h3, p { margin: 0; }
	.welcome-copy h1, .run-heading h1 { max-width: 650px; margin-top: 1.1rem; font-size: clamp(2.8rem, 6vw, 5.9rem); line-height: 0.94; letter-spacing: -0.075em; font-weight: 650; }
	h1 em { color: var(--fd-teal); font-style: normal; }
	.welcome-lede { max-width: 450px; margin-top: 1.65rem; color: var(--fd-muted); font-size: 0.95rem; line-height: 1.7; }
	.trust-notes { display: grid; gap: 0.7rem; margin-top: 2.2rem; color: var(--fd-muted); font-size: 0.74rem; }
	.trust-notes div { gap: 0.55rem; }
	.trust-notes :global(svg) { color: var(--fd-teal); }
	.composer-card { position: relative; padding: clamp(1.2rem, 3vw, 2rem); border: 1px solid var(--fd-line); border-radius: 19px; background: linear-gradient(145deg, var(--fd-panel-strong), var(--fd-panel)); box-shadow: 0 22px 70px color-mix(in oklab, var(--fd-teal) 8%, transparent); }
	.composer-topline { display: flex; justify-content: space-between; min-height: 70px; }
	.composer-card h2 { margin-top: 0.45rem; font-size: 1.28rem; letter-spacing: -0.04em; }
	.section-label { color: var(--fd-muted); font-size: 0.59rem; font-weight: 700; }
	.composer-orbit { position: relative; width: 62px; height: 62px; margin-top: -0.25rem; border: 1px solid color-mix(in oklab, var(--fd-teal) 34%, transparent); border-radius: 50%; opacity: 0.8; }
	.composer-orbit::before, .composer-orbit::after { position: absolute; inset: 8px; border: 1px solid color-mix(in oklab, var(--fd-teal) 30%, transparent); border-radius: 50%; content: ''; }
	.composer-orbit::after { inset: 18px; background: var(--fd-teal); box-shadow: 0 0 0 5px color-mix(in oklab, var(--fd-teal) 13%, transparent); }
	.composer-orbit span { position: absolute; top: 3px; left: 30px; width: 5px; height: 5px; border-radius: 50%; background: var(--fd-amber); }
	.composer-orbit span:nth-child(2) { top: 41px; left: 4px; background: var(--fd-coral); }
	.composer-orbit span:nth-child(3) { top: 16px; left: 50px; }
	label { display: block; margin: 1.2rem 0 0.45rem; color: var(--fd-muted); font-size: 0.7rem; font-weight: 650; }
	.select-wrap { display: flex; align-items: center; gap: 0.65rem; padding: 0 0.85rem; border: 1px solid var(--fd-line); border-radius: 10px; background: color-mix(in oklab, var(--app-bg) 60%, transparent); color: var(--fd-teal); }
	select { min-width: 0; flex: 1; height: 44px; border: 0; outline: 0; background: transparent; color: var(--fd-ink); font: inherit; font-size: 0.8rem; }
	textarea { display: block; width: 100%; min-height: 128px; resize: vertical; padding: 0.8rem 0.9rem; border: 1px solid var(--fd-line); border-radius: 10px; outline: 0; background: color-mix(in oklab, var(--app-bg) 60%, transparent); color: var(--fd-ink); font: inherit; font-size: 0.82rem; line-height: 1.55; }
	select:focus, textarea:focus { border-color: color-mix(in oklab, var(--fd-teal) 68%, transparent); box-shadow: 0 0 0 3px color-mix(in oklab, var(--fd-teal) 13%, transparent); }
	textarea::placeholder { color: var(--fd-faint); }
	.composer-foot { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-top: 1rem; }
	.context-note { min-width: 0; gap: 0.45rem; color: var(--fd-muted); font-size: 0.67rem; }
	.context-note span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.start-button { display: inline-flex; align-items: center; gap: 0.55rem; min-height: 44px; padding: 0 1rem; border: 0; border-radius: 10px; background: var(--fd-teal-deep); color: var(--app-bg); font: inherit; font-size: 0.76rem; font-weight: 750; transition: transform 160ms ease, opacity 160ms ease; }
	.start-button:hover:not(:disabled) { transform: translateY(-2px); }
	button:focus-visible { outline: 2px solid var(--fd-teal); outline-offset: 3px; }
	button:disabled { cursor: not-allowed; opacity: 0.55; }
	.button-sheen { animation: breathe 1.1s ease-in-out infinite; }
	.inline-error, .disabled-note { display: flex; gap: 0.5rem; margin-top: 0.9rem; padding: 0.7rem 0.8rem; border-radius: 9px; font-size: 0.7rem; line-height: 1.45; }
	.inline-error { color: var(--fd-coral); background: color-mix(in oklab, var(--fd-coral) 10%, transparent); }
	.disabled-note { color: var(--fd-amber); background: color-mix(in oklab, var(--fd-amber) 11%, transparent); }
	.workspace-empty { display: flex; align-items: center; gap: 0.45rem; margin-top: 0.55rem; color: var(--fd-amber); font-size: 0.66rem; }
	.empty-lower { margin-top: 0.5rem; }
	.empty-rule { height: 1px; background: var(--fd-line); }
	.empty-lower-copy { justify-content: space-between; gap: 1rem; padding-top: 0.8rem; color: var(--fd-faint); font-size: 0.68rem; }
	.mono { font-size: 0.57rem; }

	.run-heading { display: flex; align-items: end; justify-content: space-between; gap: 2rem; margin-bottom: 2.2rem; }
	.run-heading h1 { margin-top: 0.8rem; font-size: clamp(2.5rem, 5vw, 4.4rem); }
	.run-objective { max-width: 680px; margin-top: 1rem; color: var(--fd-muted); font-size: 0.85rem; line-height: 1.55; white-space: pre-wrap; }
	.run-actions { flex-wrap: wrap; justify-content: flex-end; gap: 0.55rem; }
	.run-id-block { min-width: 0; margin-right: 0.35rem; }
	.run-id-block span { display: block; margin-bottom: 0.3rem; color: var(--fd-faint); font-size: 0.55rem; }
	code { display: block; max-width: 150px; overflow: hidden; color: var(--fd-muted); font-family: var(--font-mono); font-size: 0.62rem; text-overflow: ellipsis; white-space: nowrap; }
	.quiet-button, .cancel-button { gap: 0.4rem; min-height: 36px; padding: 0 0.75rem; border: 1px solid var(--fd-line); border-radius: 8px; background: transparent; color: var(--fd-muted); font: inherit; font-size: 0.68rem; }
	.quiet-button:hover:not(:disabled) { color: var(--fd-ink); background: var(--fd-panel); }
	.cancel-button { border-color: color-mix(in oklab, var(--fd-coral) 38%, transparent); color: var(--fd-coral); }
	.cancel-button:hover { background: color-mix(in oklab, var(--fd-coral) 8%, transparent); }
	.cancel-confirm { display: flex; align-items: center; gap: 0.4rem; padding: 0.3rem; border: 1px solid color-mix(in oklab, var(--fd-coral) 35%, transparent); border-radius: 9px; background: color-mix(in oklab, var(--fd-coral) 7%, var(--app-bg)); font-size: 0.68rem; }
	.cancel-confirm span { padding-left: 0.45rem; color: var(--fd-coral); }
	.confirm-stop, .cancel-dismiss { min-height: 29px; padding: 0 0.55rem; border: 0; border-radius: 6px; font: inherit; font-size: 0.64rem; }
	.confirm-stop { background: var(--fd-coral); color: #fff5ef; }
	.cancel-dismiss { background: transparent; color: var(--fd-muted); }
	.reconnect-banner { display: flex; justify-content: space-between; gap: 1rem; margin-bottom: 1.2rem; padding: 0.8rem 1rem; border: 1px solid color-mix(in oklab, var(--fd-amber) 36%, transparent); border-radius: 10px; background: color-mix(in oklab, var(--fd-amber) 9%, transparent); color: var(--fd-amber); font-size: 0.71rem; }
	.reconnect-banner > div { gap: 0.55rem; }
	.reconnect-banner button { border: 0; background: transparent; color: inherit; font: inherit; font-weight: 700; text-decoration: underline; }
	.run-grid { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(260px, 0.55fr); align-items: start; gap: 1rem; }
	.run-primary { display: grid; gap: 1rem; min-width: 0; }
	.state-card, .inspector-card, .activity-card, .evidence-card, .reconnect-card { border: 1px solid var(--fd-line); border-radius: 14px; background: color-mix(in oklab, var(--fd-panel) 75%, transparent); }
	.state-card { overflow: hidden; }
	.state-card-top { display: grid; grid-template-columns: auto 1fr auto; align-items: start; gap: 0.9rem; padding: 1.3rem; }
	.state-icon { display: grid; width: 40px; height: 40px; place-items: center; border-radius: 11px; background: color-mix(in oklab, var(--fd-teal) 14%, transparent); color: var(--fd-teal); }
	.state-failure .state-icon { background: color-mix(in oklab, var(--fd-coral) 13%, transparent); color: var(--fd-coral); }
	.state-cancelled .state-icon { background: color-mix(in oklab, var(--fd-amber) 14%, transparent); color: var(--fd-amber); }
	.state-manual .state-icon { background: color-mix(in oklab, var(--fd-amber) 14%, transparent); color: var(--fd-amber); }
	.state-card h2 { margin-top: 0.35rem; font-size: 1.12rem; letter-spacing: -0.035em; }
	.state-card p { max-width: 500px; margin-top: 0.35rem; color: var(--fd-muted); font-size: 0.73rem; line-height: 1.5; }
	.state-connection { gap: 0.4rem; color: var(--fd-muted); font-size: 0.63rem; white-space: nowrap; }
	.state-meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.8rem; padding: 0.9rem 1.3rem 1.1rem; border-top: 1px solid var(--fd-line); background: color-mix(in oklab, var(--app-bg) 25%, transparent); }
	.state-meta span { display: block; margin-bottom: 0.35rem; color: var(--fd-faint); font-size: 0.58rem; }
	.state-meta strong { display: block; overflow: hidden; color: var(--fd-ink); font-size: 0.7rem; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
	.telemetry-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.7rem; }
	.telemetry-card { min-width: 0; padding: 0.95rem 1rem; border: 1px solid var(--fd-line); border-radius: 12px; background: color-mix(in oklab, var(--fd-panel) 70%, transparent); }
	.telemetry-label { display: block; color: var(--fd-faint); font-family: var(--font-mono); font-size: 0.55rem; letter-spacing: 0.11em; text-transform: uppercase; }
	.telemetry-card strong { display: block; overflow: hidden; margin-top: 0.42rem; color: var(--fd-ink); font-size: 0.84rem; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
	.telemetry-card small { display: block; margin-top: 0.36rem; color: var(--fd-muted); font-size: 0.61rem; line-height: 1.35; }
	.inspector-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
	.inspector-card, .evidence-card { min-height: 160px; padding: 1.1rem; }
	.inspector-heading { gap: 0.55rem; color: var(--fd-teal); }
	.inspector-heading h3 { color: var(--fd-ink); font-size: 0.8rem; font-weight: 700; }
	pre { max-height: 180px; margin: 1rem 0 0; overflow: auto; color: var(--fd-muted); font-family: var(--font-mono); font-size: 0.64rem; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
	.not-reported { display: flex; align-items: center; gap: 0.5rem; margin-top: 2rem; color: var(--fd-faint); font-size: 0.7rem; }
	.not-reported span { width: 6px; height: 6px; border-radius: 50%; background: var(--fd-faint); }
	.activity-card { padding: 1.1rem; }
	.activity-heading { display: flex; align-items: center; justify-content: space-between; }
	.telemetry-note { color: var(--fd-faint); font-size: 0.52rem; }
	.activity-list { margin-top: 1rem; }
	.activity-item { position: relative; display: grid; grid-template-columns: 13px 1fr auto; gap: 0.7rem; padding: 0.7rem 0; border-top: 1px solid var(--fd-line); }
	.activity-marker { width: 7px; height: 7px; margin-top: 0.28rem; border: 2px solid var(--fd-teal); border-radius: 50%; }
	.activity-item-copy { display: grid; gap: 0.2rem; min-width: 0; }
	.activity-item-copy strong { font-size: 0.72rem; font-weight: 650; }
	.activity-item-copy span, .activity-item time { color: var(--fd-muted); font-size: 0.65rem; }
	.activity-item time { white-space: nowrap; }
	.empty-telemetry { display: flex; align-items: center; gap: 0.55rem; margin-top: 1rem; padding: 1.35rem 0; color: var(--fd-faint); font-size: 0.7rem; }
	.run-side { display: grid; gap: 1rem; min-width: 0; }
	.evidence-card { min-height: 220px; }
	.side-note { margin-top: 1.1rem; color: var(--fd-muted); font-size: 0.68rem; line-height: 1.55; }
	.reconnect-card { align-items: flex-start; gap: 0.75rem; padding: 1rem; }
	.reconnect-card-icon { display: grid; width: 29px; height: 29px; flex: 0 0 auto; place-items: center; border-radius: 8px; background: color-mix(in oklab, var(--fd-teal) 12%, transparent); color: var(--fd-teal); }
	.reconnect-card strong { font-size: 0.72rem; }
	.reconnect-card p { margin-top: 0.35rem; color: var(--fd-muted); font-size: 0.66rem; line-height: 1.5; }
	.run-footnote { display: grid; gap: 0.45rem; padding: 0.4rem 0.2rem; color: var(--fd-faint); font-size: 0.62rem; }
	@keyframes breathe { 0%, 100% { opacity: 0.55; } 50% { opacity: 1; } }
	@media (max-width: 820px) {
		.welcome-grid, .run-grid { grid-template-columns: 1fr; }
		.welcome-grid { min-height: auto; gap: 2.5rem; }
		.run-heading { align-items: start; flex-direction: column; }
		.run-actions { justify-content: flex-start; }
		.run-side { grid-row: auto; }
	}
	@media (max-width: 560px) {
		.flowdeck-header { min-height: 58px; padding: 0.65rem 0.9rem; }
		.header-context, .status-divider { display: none; }
		.flowdeck-main { padding: 2.2rem 0.9rem 3rem; }
		.welcome-copy h1, .run-heading h1 { font-size: clamp(2.5rem, 14vw, 4rem); }
		.composer-card { border-radius: 14px; }
		.composer-foot { align-items: stretch; flex-direction: column; }
		.start-button { justify-content: center; }
		.empty-lower-copy { align-items: flex-start; flex-direction: column; gap: 0.45rem; }
		.state-card-top { grid-template-columns: auto 1fr; }
		.state-connection { grid-column: 2; }
		.state-meta { grid-template-columns: 1fr; gap: 0.7rem; }
		.telemetry-grid { grid-template-columns: 1fr; }
		.inspector-grid { grid-template-columns: 1fr; }
		.reconnect-banner { align-items: flex-start; flex-direction: column; }
		.activity-item { grid-template-columns: 13px 1fr; }
		.activity-item time { grid-column: 2; }
	}
	@media (prefers-reduced-motion: reduce) {
		.start-button, .button-sheen { animation: none; transition: none; }
		.flowdeck-header { backdrop-filter: none; }
	}
</style>