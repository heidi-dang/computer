<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getMcpFactorySnapshot,
		openMcpFactoryStream,
		type McpFactoryEvent,
		type McpFactoryProgress,
		type McpFactorySnapshot,
		type McpFactoryRunSummary
	} from '$lib/apis/mcp';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';
	type DetailView = 'activity' | 'evidence' | 'reasoning' | 'delivery';
	type RunFilter = 'all' | 'active' | 'finished';

	const lifecycleStates = [
		'MISSION',
		'RECOVERING',
		'BASELINING',
		'UNDERSTANDING',
		'AUDITING',
		'SELECTING_FINDING',
		'CAPABILITY_ANALYSIS',
		'SKILL_DISCOVERY',
		'TRUST_EVALUATION',
		'SKILL_SELECTION',
		'REPRODUCING',
		'ROOT_CAUSE_ANALYSIS',
		'PLANNING',
		'IMPLEMENTING',
		'TARGETED_VERIFYING',
		'FULL_VERIFYING',
		'ADVERSARIAL_REVIEW',
		'SECURITY_REVIEW',
		'LIVE_VERIFYING',
		'VICTORY_JUDGING',
		'COMMITTING',
		'PUSHING',
		'CI_VERIFYING',
		'CYCLE_COMPLETE',
		'COMPLETE'
	] as const;

	let snapshot = $state<McpFactorySnapshot | null>(null);
	let selectedRunId = $state<string | null>(null);
	let streamStatus = $state<StreamStatus>('loading');
	let detailView = $state<DetailView>('activity');
	let runFilter = $state<RunFilter>('all');
	let errorMessage = $state<string | null>(null);
	let closeStream: (() => void) | null = null;
	let connectionGeneration = 0;

	const selected = $derived(snapshot?.selected ?? null);
	const filteredRuns = $derived(
		(snapshot?.runs ?? []).filter((run) => {
			if (runFilter === 'active') return !run.terminal;
			if (runFilter === 'finished') return run.terminal;
			return true;
		})
	);
	const latestEvents = $derived(selected ? [...selected.events].reverse() : []);
	const capabilityLabels = $derived(valueLabels(selected?.cycle?.selected_capabilities));
	const capabilityRequirements = $derived(valueLabels(selected?.cycle?.capability_requirements));
	const failures = $derived(failureRows(selected?.cycle?.failure_signatures));
	const gatePercent = $derived(
		selected && selected.summary.required_gates > 0
			? Math.round((selected.summary.passed_required_gates / selected.summary.required_gates) * 100)
			: 0
	);
	const progressPercent = $derived(selected?.progress.percent ?? 0);

	function applySnapshot(next: McpFactorySnapshot) {
		snapshot = next;
		if (!selectedRunId && next.selected) selectedRunId = next.selected.run_id;
		streamStatus = 'live';
		errorMessage = null;
	}

	function applyActivity(event: McpFactoryEvent) {
		if (!snapshot?.selected) return;
		if (snapshot.selected.events.some((item) => item.event_id === event.event_id)) return;
		const events = [...snapshot.selected.events, event]
			.sort((a, b) => a.sequence - b.sequence)
			.slice(-160);
		snapshot = {
			...snapshot,
			selected: {
				...snapshot.selected,
				events,
				summary: {
					...snapshot.selected.summary,
					event_count: snapshot.selected.summary.event_count + 1,
					last_event_sequence: Math.max(
						snapshot.selected.summary.last_event_sequence,
						event.sequence
					)
				}
			}
		};
		streamStatus = 'live';
		errorMessage = null;
	}

	function applyProgress(progress: McpFactoryProgress) {
		if (!snapshot?.selected) return;
		snapshot = {
			...snapshot,
			selected: { ...snapshot.selected, progress }
		};
		streamStatus = 'live';
		errorMessage = null;
	}

	async function connect(runId: string | null) {
		const generation = ++connectionGeneration;
		closeStream?.();
		closeStream = null;
		streamStatus = snapshot ? 'reconnecting' : 'loading';
		errorMessage = null;
		try {
			const initial = await getMcpFactorySnapshot(runId);
			if (generation !== connectionGeneration) return;
			applySnapshot(initial);
			const effectiveRunId = runId ?? initial.selected?.run_id ?? null;
			if (effectiveRunId) selectedRunId = effectiveRunId;
			closeStream = openMcpFactoryStream(effectiveRunId, {
				onSnapshot: (next) => {
					if (generation === connectionGeneration) applySnapshot(next);
				},
				onActivity: (event) => {
					if (generation === connectionGeneration) applyActivity(event);
				},
				onProgress: (progress) => {
					if (generation === connectionGeneration) applyProgress(progress);
				},
				onOpen: () => {
					if (generation === connectionGeneration) streamStatus = 'live';
				},
				onError: () => {
					if (generation !== connectionGeneration) return;
					streamStatus = 'reconnecting';
					errorMessage = 'Realtime stream reconnecting';
				}
			});
		} catch (error) {
			if (generation !== connectionGeneration) return;
			streamStatus = 'error';
			errorMessage = error instanceof Error ? error.message : 'Unable to load Dark Factory state';
		}
	}

	function selectRun(runId: string) {
		if (selectedRunId === runId && snapshot?.selected?.run_id === runId) return;
		selectedRunId = runId;
		void connect(runId);
	}

	onMount(() => {
		void connect(null);
		return () => {
			connectionGeneration += 1;
			closeStream?.();
		};
	});

	function stateLabel(value: string | null | undefined): string {
		if (!value) return 'Unknown';
		return value
			.toLowerCase()
			.split('_')
			.map((word) => word.charAt(0).toUpperCase() + word.slice(1))
			.join(' ');
	}

	function statusTone(
		value: string | null | undefined
	): 'success' | 'danger' | 'warning' | 'info' | 'muted' {
		const normalized = String(value ?? '').toUpperCase();
		if (
			['PASS', 'SUCCESS', 'COMPLETE', 'COMPLETED', 'APPROVED', 'INTEGRATED', 'PUSHED'].includes(
				normalized
			)
		) {
			return 'success';
		}
		if (
			['FAIL', 'FAILED', 'ERROR', 'BLOCKED', 'CANCELLED', 'REJECTED', 'MISSING'].includes(
				normalized
			)
		) {
			return 'danger';
		}
		if (['PENDING', 'QUEUED', 'APPROVAL_REQUIRED', 'PAUSED', 'CANCELLING'].includes(normalized)) {
			return 'warning';
		}
		if (normalized) return 'info';
		return 'muted';
	}

	function pipelineTone(stage: string): 'done' | 'current' | 'pending' {
		const current = selected?.progress.effective_state;
		const currentIndex = lifecycleStates.indexOf(current as (typeof lifecycleStates)[number]);
		const stageIndex = lifecycleStates.indexOf(stage as (typeof lifecycleStates)[number]);
		if (currentIndex < 0) return 'pending';
		if (stageIndex < currentIndex) return 'done';
		if (stageIndex === currentIndex) return 'current';
		return 'pending';
	}

	function shortState(stage: string): string {
		const labels: Record<string, string> = {
			SELECTING_FINDING: 'Finding',
			CAPABILITY_ANALYSIS: 'Capability',
			SKILL_DISCOVERY: 'Skills',
			TRUST_EVALUATION: 'Trust',
			SKILL_SELECTION: 'Select skill',
			ROOT_CAUSE_ANALYSIS: 'Root cause',
			TARGETED_VERIFYING: 'Targeted',
			FULL_VERIFYING: 'Full verify',
			ADVERSARIAL_REVIEW: 'Adversarial',
			SECURITY_REVIEW: 'Security',
			LIVE_VERIFYING: 'Live verify',
			VICTORY_JUDGING: 'Victory',
			CI_VERIFYING: 'CI',
			CYCLE_COMPLETE: 'Cycle done'
		};
		return labels[stage] ?? stateLabel(stage);
	}

	function formatTime(value: number | null | undefined): string {
		if (!value) return '—';
		return new Intl.DateTimeFormat(undefined, {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		}).format(new Date(value));
	}

	function relativeTime(value: number | null | undefined): string {
		if (!value) return '—';
		const seconds = Math.max(0, Math.round((Date.now() - value) / 1000));
		if (seconds < 60) return `${seconds}s ago`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m ago`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `${hours}h ago`;
		return `${Math.floor(hours / 24)}d ago`;
	}

	function formatDuration(ms: number | null | undefined): string {
		if (!ms) return '0 ms';
		if (ms < 1000) return `${ms} ms`;
		if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
		return `${(ms / 60_000).toFixed(1)} min`;
	}

	function formatTokens(value: number): string {
		return new Intl.NumberFormat(undefined, {
			notation: value >= 10_000 ? 'compact' : 'standard'
		}).format(value);
	}

	function formatCost(microusd: number): string {
		const usd = microusd / 1_000_000;
		return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
	}

	function shortId(value: string | null | undefined, length = 10): string {
		if (!value) return '—';
		return value.length > length ? `${value.slice(0, length)}…` : value;
	}

	function jsonText(value: unknown): string {
		if (value === null || value === undefined) return '—';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	function valueLabels(value: unknown): string[] {
		if (!Array.isArray(value)) return [];
		return value
			.map((item) => {
				if (typeof item === 'string') return item;
				if (item && typeof item === 'object') {
					const record = item as Record<string, unknown>;
					for (const key of ['stable_id', 'name', 'id', 'capability', 'requirement']) {
						if (typeof record[key] === 'string') return String(record[key]);
					}
				}
				return jsonText(item).replace(/\s+/g, ' ').slice(0, 120);
			})
			.filter(Boolean);
	}

	function failureRows(
		value: unknown
	): Array<{ signature: string; count: number; code: string; summary: string }> {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
		return Object.entries(value as Record<string, unknown>)
			.map(([signature, item]) => {
				const record = item && typeof item === 'object' ? (item as Record<string, unknown>) : {};
				return {
					signature,
					count: Number(record.count ?? 0),
					code: String(record.code ?? 'unknown'),
					summary: String(record.last_summary ?? '')
				};
			})
			.sort((a, b) => b.count - a.count);
	}

	function runFilterCount(filter: RunFilter): number {
		const runs = snapshot?.runs ?? [];
		if (filter === 'active') return runs.filter((run) => !run.terminal).length;
		if (filter === 'finished') return runs.filter((run) => run.terminal).length;
		return runs.length;
	}

	function runStateTone(run: McpFactoryRunSummary) {
		return statusTone(run.state);
	}
</script>

<div class="factory-root app-theme h-full min-h-0 overflow-hidden">
	<div class="factory-layout h-full min-h-0">
		<aside class="run-rail app-surface min-h-0 border-r">
			<div class="run-rail-header border-b px-3 py-3">
				<div class="flex items-center justify-between gap-3">
					<div>
						<p class="text-[0.68rem] font-semibold tracking-[0.12em] app-muted uppercase">Runs</p>
						<p class="mt-0.5 text-sm font-semibold">Factory history</p>
					</div>
					<div
						class="stream-indicator"
						data-status={streamStatus}
						title={errorMessage ?? streamStatus}
					>
						<span class="stream-dot"></span>
						<span
							>{streamStatus === 'live'
								? 'Live'
								: streamStatus === 'loading'
									? 'Loading'
									: 'Syncing'}</span
						>
					</div>
				</div>
				<div class="mt-3 grid grid-cols-3 gap-1 rounded-xl app-subtle-surface p-1">
					{#each ['all', 'active', 'finished'] as filter}
						<button
							type="button"
							class="filter-button app-interactive {runFilter === filter
								? 'app-interactive-active'
								: 'app-muted'}"
							onclick={() => (runFilter = filter as RunFilter)}
						>
							<span>{filter === 'all' ? 'All' : filter === 'active' ? 'Active' : 'Done'}</span>
							<span class="tabular-nums opacity-70">{runFilterCount(filter as RunFilter)}</span>
						</button>
					{/each}
				</div>
			</div>

			<div class="run-list min-h-0 overflow-y-auto p-2">
				{#if streamStatus === 'loading' && !snapshot}
					{#each Array(5) as _}
						<div class="run-skeleton mb-2 h-[5.4rem] rounded-xl"></div>
					{/each}
				{:else if filteredRuns.length === 0}
					<div class="px-3 py-10 text-center">
						<p class="text-sm font-medium">No {runFilter === 'all' ? '' : runFilter} runs</p>
						<p class="mt-1 text-xs leading-relaxed app-muted">
							Durable Dark Factory runs will appear here automatically.
						</p>
					</div>
				{:else}
					{#each filteredRuns as run (run.run_id)}
						<button
							type="button"
							class="run-item app-interactive w-full text-left {selectedRunId === run.run_id
								? 'selected'
								: ''}"
							onclick={() => selectRun(run.run_id)}
							aria-current={selectedRunId === run.run_id ? 'true' : undefined}
						>
							<div class="flex items-center gap-2">
								<span class="status-dot" data-tone={runStateTone(run)}></span>
								<span
									class="min-w-0 flex-1 truncate text-[0.68rem] font-semibold tracking-[0.05em] uppercase"
									>{stateLabel(run.state)}</span
								>
								<span class="shrink-0 text-[0.62rem] tabular-nums app-muted"
									>{relativeTime(run.updated_at)}</span
								>
							</div>
							<p class="mt-2 line-clamp-2 text-[0.78rem] font-medium leading-[1.35]">
								{run.mission}
							</p>
							<div class="mt-2 flex items-center justify-between gap-2 text-[0.62rem] app-muted">
								<span class="truncate">{run.workspace_name ?? shortId(run.workspace_id, 16)}</span>
								<span class="font-mono">{shortId(run.run_id, 12)}</span>
							</div>
						</button>
					{/each}
				{/if}
			</div>
		</aside>

		<main class="factory-main min-h-0 overflow-y-auto">
			{#if streamStatus === 'error' && !selected}
				<div class="flex min-h-full items-center justify-center p-6">
					<div class="max-w-md text-center">
						<div
							class="mx-auto flex size-12 items-center justify-center rounded-2xl app-subtle-surface border"
						>
							<svg
								viewBox="0 0 24 24"
								class="size-5 app-muted"
								fill="none"
								stroke="currentColor"
								stroke-width="1.7"
								aria-hidden="true"
							>
								<path
									d="M12 9v4m0 4h.01M10.3 4.6 2.9 17.3A2 2 0 0 0 4.6 20h14.8a2 2 0 0 0 1.7-2.7L13.7 4.6a2 2 0 0 0-3.4 0Z"
									stroke-linecap="round"
									stroke-linejoin="round"
								/>
							</svg>
						</div>
						<h2 class="mt-4 text-base font-semibold">Factory telemetry unavailable</h2>
						<p class="mt-1 text-sm app-muted">
							{errorMessage ?? 'The realtime observability API could not be reached.'}
						</p>
						<button
							type="button"
							class="app-interactive app-accent-surface mt-4 rounded-xl border px-4 py-2 text-xs font-semibold"
							onclick={() => void connect(selectedRunId)}>Retry</button
						>
					</div>
				</div>
			{:else if !selected}
				<div class="empty-factory flex min-h-full items-center justify-center p-6">
					<div class="max-w-lg text-center">
						<div
							class="factory-mark mx-auto flex size-16 items-center justify-center rounded-2xl border"
						>
							<svg
								viewBox="0 0 32 32"
								class="size-8"
								fill="none"
								stroke="currentColor"
								stroke-width="1.5"
								aria-hidden="true"
							>
								<path
									d="M7 8.5h18v15H7zM11 5v3.5M21 5v3.5M12 14h8M12 18h5"
									stroke-linecap="round"
									stroke-linejoin="round"
								/>
							</svg>
						</div>
						<h2 class="mt-5 text-lg font-semibold">Dark Factory is ready</h2>
						<p class="mt-2 text-sm leading-relaxed app-muted">
							When a factory mission starts, this view becomes the live operations record: state
							transitions, machine gates, evidence, reasoning roles, workers, capability choices,
							Git and CI.
						</p>
					</div>
				</div>
			{:else}
				<div
					class="factory-content mx-auto w-full max-w-[112rem] px-3 py-3 sm:px-5 sm:py-4 xl:px-6"
				>
					<section class="run-hero">
						<div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
							<div class="min-w-0 max-w-5xl">
								<div class="flex flex-wrap items-center gap-x-3 gap-y-2 text-[0.68rem]">
									<span class="status-badge" data-tone={statusTone(selected.state)}>
										<span class="status-dot" data-tone={statusTone(selected.state)}></span>
										{stateLabel(selected.state)}
									</span>
									<span class="app-muted"
										>{selected.workspace_name ?? shortId(selected.workspace_id, 20)}</span
									>
									<span class="app-muted"
										>Cycle {selected.summary.current_cycle_ordinal || '—'}</span
									>
									<span class="app-muted">Updated {relativeTime(selected.updated_at)}</span>
								</div>
								<h2 class="mt-3 text-xl font-semibold leading-tight tracking-[-0.02em] sm:text-2xl">
									{selected.mission}
								</h2>
								{#if selected.next_action}
									<div class="next-action mt-3 flex items-start gap-2.5">
										<span class="mt-1.5 size-1.5 shrink-0 rounded-full bg-[var(--app-accent)]"
										></span>
										<div class="min-w-0">
											<p class="text-[0.62rem] font-semibold tracking-[0.11em] app-muted uppercase">
												Next action
											</p>
											<p class="mt-0.5 text-sm leading-relaxed">{selected.next_action}</p>
										</div>
									</div>
								{/if}
							</div>
							<div
								class="hero-meta grid shrink-0 grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4 xl:grid-cols-2"
							>
								<div>
									<p class="meta-label">Model</p>
									<p class="meta-value truncate" title={selected.model_id ?? 'not recorded'}>
										{selected.model_id ?? '—'}
									</p>
								</div>
								<div>
									<p class="meta-label">Run</p>
									<p class="meta-value font-mono" title={selected.run_id}>
										{shortId(selected.run_id, 16)}
									</p>
								</div>
								<div>
									<p class="meta-label">Target</p>
									<p class="meta-value font-mono" title={selected.cycle?.target_revision ?? ''}>
										{shortId(selected.cycle?.target_revision, 12)}
									</p>
								</div>
								<div>
									<p class="meta-label">Stream</p>
									<p class="meta-value flex items-center gap-1.5">
										<span class="stream-dot" data-live={streamStatus === 'live'}
										></span>{streamStatus === 'live' ? 'Realtime' : 'Reconnecting'}
									</p>
								</div>
							</div>
						</div>
					</section>

					<section
						class="overall-progress mt-4"
						role="progressbar"
						aria-label="Dark Factory progress"
						aria-valuemin="0"
						aria-valuemax="100"
						aria-valuenow={progressPercent}
					>
						<div class="progress-copy">
							<div class="min-w-0">
								<p class="meta-label">Factory progress</p>
								<div class="mt-1 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
									<strong class="progress-phase"
										>{stateLabel(selected.progress.effective_state)}</strong
									>
									<span class="progress-position">
										Phase {selected.progress.phase_index}/{selected.progress.phase_count}
									</span>
								</div>
							</div>
							<div class="progress-percent">{progressPercent}%</div>
						</div>
						<div class="overall-progress-track" aria-hidden="true">
							<span style={`width: ${progressPercent}%`}></span>
						</div>
						<div class="progress-footer">
							<span class="capitalize">{selected.progress.outcome.replace('_', ' ')}</span>
							<span>Server-authoritative · seq {selected.summary.last_event_sequence}</span>
						</div>
					</section>

					<section class="pipeline-wrap mt-5 border-y py-3">
						<div class="pipeline overflow-x-auto pb-1">
							{#each lifecycleStates as stage, index}
								<div
									class="pipeline-stage"
									data-tone={pipelineTone(stage)}
									title={stateLabel(stage)}
								>
									<div class="pipeline-node">
										<span class="pipeline-dot"></span>
										{#if index < lifecycleStates.length - 1}<span class="pipeline-line"></span>{/if}
									</div>
									<span class="pipeline-label">{shortState(stage)}</span>
								</div>
							{/each}
						</div>
					</section>

					<section class="metric-strip mt-4 grid grid-cols-2 border sm:grid-cols-3 xl:grid-cols-6">
						<div class="metric-cell">
							<p class="meta-label">Verification</p>
							<p class="metric-value">
								{selected.summary.passed_required_gates}<span class="metric-denominator"
									>/{selected.summary.required_gates}</span
								>
							</p>
							<div class="metric-progress"><span style={`width: ${gatePercent}%`}></span></div>
						</div>
						<div class="metric-cell">
							<p class="meta-label">Evidence</p>
							<p class="metric-value">{selected.summary.evidence_count}</p>
							<p class="metric-caption">machine records</p>
						</div>
						<div class="metric-cell">
							<p class="meta-label">Events</p>
							<p class="metric-value">{selected.summary.event_count}</p>
							<p class="metric-caption">seq {selected.summary.last_event_sequence}</p>
						</div>
						<div class="metric-cell">
							<p class="meta-label">Workers</p>
							<p class="metric-value">{selected.summary.active_workers}</p>
							<p class="metric-caption">active lanes</p>
						</div>
						<div class="metric-cell">
							<p class="meta-label">Reasoning</p>
							<p class="metric-value">
								{formatTokens(selected.summary.input_tokens + selected.summary.output_tokens)}
							</p>
							<p class="metric-caption">{selected.summary.reasoning_calls} role calls</p>
						</div>
						<div class="metric-cell border-r-0">
							<p class="meta-label">Reasoning cost</p>
							<p class="metric-value">{formatCost(selected.summary.reasoning_cost_microusd)}</p>
							<p class="metric-caption">{formatDuration(selected.summary.reasoning_runtime_ms)}</p>
						</div>
					</section>

					<div
						class="overview-grid mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.12fr)_minmax(23rem,0.88fr)]"
					>
						<div class="space-y-4">
							<section class="factory-panel border">
								<div class="panel-heading">
									<div>
										<p class="panel-kicker">Current cycle</p>
										<h3>Decision context</h3>
									</div>
									{#if selected.cycle}<span class="cycle-number">#{selected.cycle.ordinal}</span
										>{/if}
								</div>
								<div class="panel-body grid gap-4 lg:grid-cols-2">
									<div>
										<p class="detail-label">Selected finding</p>
										<div class="detail-copy mt-1">
											{selected.cycle?.selected_finding
												? jsonText(selected.cycle.selected_finding)
												: 'No finding selected yet.'}
										</div>
									</div>
									<div>
										<p class="detail-label">Acceptance criteria</p>
										<ul class="mt-1.5 space-y-1.5">
											{#each selected.acceptance_criteria as criterion}
												<li class="flex gap-2 text-xs leading-relaxed">
													<span
														class="mt-[0.45rem] size-1 shrink-0 rounded-full bg-[var(--app-fg-subtle)]"
													></span><span>{criterion}</span>
												</li>
											{/each}
										</ul>
									</div>
									<div>
										<p class="detail-label">Capability requirements</p>
										<div class="chip-list mt-2">
											{#if capabilityRequirements.length}
												{#each capabilityRequirements as item}<span class="info-chip">{item}</span
													>{/each}
											{:else}<span class="text-xs app-muted">None recorded</span>{/if}
										</div>
									</div>
									<div>
										<p class="detail-label">Selected capabilities</p>
										<div class="chip-list mt-2">
											{#if capabilityLabels.length}
												{#each capabilityLabels as item}<span class="info-chip accent">{item}</span
													>{/each}
											{:else}<span class="text-xs app-muted">No capability selected yet</span>{/if}
										</div>
									</div>
								</div>
								{#if selected.cycle}
									<div class="revision-row border-t">
										<div>
											<span>Base</span><code>{shortId(selected.cycle.base_revision, 16)}</code>
										</div>
										<div>
											<span>Target</span><code>{shortId(selected.cycle.target_revision, 16)}</code>
										</div>
										<div><span>Attempts</span><strong>{selected.cycle.attempt_count}</strong></div>
										<div><span>Cycles</span><strong>{selected.summary.cycle_count}</strong></div>
									</div>
								{/if}
							</section>

							<section class="factory-panel border">
								<div class="panel-heading">
									<div>
										<p class="panel-kicker">Verification</p>
										<h3>Machine gate matrix</h3>
									</div>
									<span class="text-xs font-semibold tabular-nums app-muted">{gatePercent}%</span>
								</div>
								<div class="gate-list">
									{#if selected.gates.length === 0}
										<div class="panel-empty">
											No gates have been evaluated for the current cycle.
										</div>
									{:else}
										{#each selected.gates as gate (gate.gate_result_id)}
											<div class="gate-row">
												<span class="gate-status" data-tone={statusTone(gate.status)}
													>{gate.status}</span
												>
												<div class="min-w-0 flex-1">
													<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
														<span class="truncate text-xs font-semibold">{gate.gate_id}</span><span
															class="text-[0.62rem] app-muted"
															>{gate.category} · attempt {gate.attempt}</span
														>
													</div>
													{#if gate.reason}<p class="mt-1 text-[0.68rem] leading-relaxed app-muted">
															{gate.reason}
														</p>{/if}
												</div>
												<span class="shrink-0 text-[0.62rem] app-muted"
													>{gate.evidence_ids.length} proof</span
												>
											</div>
										{/each}
									{/if}
								</div>
							</section>
						</div>

						<div class="space-y-4">
							<section class="factory-panel border">
								<div class="panel-heading">
									<div>
										<p class="panel-kicker">Execution</p>
										<h3>Worker ownership</h3>
									</div>
									<span class="text-xs tabular-nums app-muted">{selected.workers.length}</span>
								</div>
								<div class="worker-list">
									{#if selected.workers.length === 0}
										<div class="panel-empty">No worker lane has been assigned.</div>
									{:else}
										{#each selected.workers as worker (worker.assignment_id)}
											<div class="worker-row">
												<div class="flex min-w-0 items-center gap-2">
													<span class="status-dot" data-tone={statusTone(worker.status)}
													></span><span class="truncate text-xs font-semibold"
														>{worker.mode === 'MUTATION' ? 'Mutation lane' : 'Read-only lane'}</span
													>
												</div>
												<span
													class="text-[0.62rem] font-semibold"
													data-tone={statusTone(worker.status)}>{stateLabel(worker.status)}</span
												>
												<div
													class="col-span-2 mt-1 grid grid-cols-2 gap-2 text-[0.65rem] app-muted"
												>
													<div class="truncate" title={worker.branch ?? ''}>
														Branch <code>{worker.branch ?? '—'}</code>
													</div>
													<div class="truncate">
														Scope {worker.scope.length ? worker.scope.join(', ') : '—'}
													</div>
												</div>
											</div>
										{/each}
									{/if}
								</div>
							</section>

							<section class="factory-panel border">
								<div class="panel-heading">
									<div>
										<p class="panel-kicker">Recovery</p>
										<h3>Failures & approvals</h3>
									</div>
									<span class="text-xs tabular-nums app-muted"
										>{failures.length + selected.approvals.length}</span
									>
								</div>
								<div class="panel-body space-y-4">
									<div>
										<p class="detail-label">Failure signatures</p>
										{#if failures.length === 0}<p class="mt-1.5 text-xs app-muted">
												No persisted failure signature in this cycle.
											</p>{:else}
											<div class="mt-2 space-y-2">
												{#each failures as failure}
													<div class="failure-row">
														<div class="flex items-center justify-between gap-2">
															<code class="truncate">{failure.code}</code><span
																class="failure-count">×{failure.count}</span
															>
														</div>
														{#if failure.summary}<p>{failure.summary}</p>{/if}
													</div>
												{/each}
											</div>
										{/if}
									</div>
									<div class="border-t pt-3">
										<p class="detail-label">Approval ledger</p>
										{#if selected.approvals.length === 0}<p class="mt-1.5 text-xs app-muted">
												No approval boundary recorded.
											</p>{:else}
											<div class="mt-2 space-y-2">
												{#each selected.approvals as approval}
													<div class="approval-row">
														<span class="status-dot" data-tone={statusTone(approval.status)}></span>
														<div class="min-w-0 flex-1">
															<p class="truncate text-xs font-semibold">
																{stateLabel(approval.kind)}
															</p>
															<p class="mt-0.5 truncate font-mono text-[0.62rem] app-muted">
																{shortId(approval.revision, 14)} → {approval.remote}/{approval.branch}
															</p>
														</div>
														<span
															class="text-[0.62rem] font-semibold"
															data-tone={statusTone(approval.status)}>{approval.status}</span
														>
													</div>
												{/each}
											</div>
										{/if}
									</div>
								</div>
							</section>
						</div>
					</div>

					<section class="factory-detail mt-4 border">
						<div class="detail-tabs border-b" role="tablist" aria-label="Factory detail stream">
							{#each [['activity', 'Activity', selected.summary.event_count], ['evidence', 'Evidence', selected.summary.evidence_count], ['reasoning', 'Reasoning', selected.summary.reasoning_calls], ['delivery', 'Delivery', selected.commit_intents.length + selected.ci_runs.length]] as tab}
								<button
									type="button"
									role="tab"
									aria-selected={detailView === tab[0]}
									class="detail-tab app-interactive {detailView === tab[0] ? 'active' : ''}"
									onclick={() => (detailView = tab[0] as DetailView)}
									><span>{tab[1]}</span><span class="tab-count">{tab[2]}</span></button
								>
							{/each}
						</div>

						{#if detailView === 'activity'}
							<div class="detail-scroll">
								{#if latestEvents.length === 0}<div class="panel-empty">
										No durable factory events yet.
									</div>{:else}
									{#each latestEvents as event (event.event_id)}
										<div class="activity-row">
											<div class="activity-seq">{event.sequence}</div>
											<div class="activity-rail">
												<span
													class="activity-dot"
													data-tone={event.event_type === 'victory.authorized'
														? 'success'
														: event.event_type.includes('failure')
															? 'danger'
															: 'info'}
												></span>
											</div>
											<div class="min-w-0 flex-1">
												<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
													<span class="text-xs font-semibold">{event.event_type}</span><span
														class="text-[0.62rem] app-muted">{event.actor}</span
													>{#if event.from_state || event.to_state}<span
															class="text-[0.62rem] app-muted"
															>{stateLabel(event.from_state)} → {stateLabel(event.to_state)}</span
														>{/if}
												</div>
												{#if jsonText(event.payload) !== '{}'}<pre class="event-payload">{jsonText(
															event.payload
														)}</pre>{/if}
											</div>
											<time
												class="activity-time"
												datetime={new Date(event.created_at).toISOString()}
												>{formatTime(event.created_at)}</time
											>
										</div>
									{/each}
								{/if}
							</div>
						{:else if detailView === 'evidence'}
							<div class="evidence-grid detail-scroll">
								{#if selected.evidence.length === 0}<div class="panel-empty">
										No evidence has been persisted.
									</div>{:else}
									{#each selected.evidence as evidence (evidence.evidence_id)}
										<article class="evidence-row">
											<div class="flex flex-wrap items-center gap-2">
												<span
													class="status-badge compact"
													data-tone={evidence.authority === 'MACHINE' ? 'success' : 'info'}
													>{evidence.authority}</span
												><span class="text-xs font-semibold">{evidence.kind}</span><span
													class="text-[0.62rem] app-muted">{evidence.source}</span
												>
											</div>
											<div
												class="mt-2 grid gap-x-4 gap-y-1 text-[0.65rem] app-muted sm:grid-cols-3"
											>
												<span>Gate <strong>{evidence.gate_id ?? '—'}</strong></span><span
													>Revision <code>{shortId(evidence.revision, 12)}</code></span
												><span>{relativeTime(evidence.created_at)}</span>
											</div>
											<pre class="event-payload mt-2">{jsonText(evidence.payload)}</pre>
										</article>
									{/each}
								{/if}
							</div>
						{:else if detailView === 'reasoning'}
							<div class="detail-scroll">
								{#if selected.reasoning.length === 0}<div class="panel-empty">
										No structured reasoning call has been persisted.
									</div>{:else}
									{#each selected.reasoning as call (call.reasoning_id)}
										<div class="reasoning-row">
											<div class="reasoning-role">
												<span>{stateLabel(call.role)}</span><small>#{call.role_ordinal}</small>
											</div>
											<div class="min-w-0 flex-1">
												<div class="flex flex-wrap gap-x-3 gap-y-1 text-[0.65rem] app-muted">
													<span>{call.provider} · {call.model}</span><span
														>{formatTokens(call.total_tokens)} tokens</span
													><span>{formatDuration(call.runtime_ms)}</span><span
														>{formatCost(call.cost_microusd)}</span
													>
												</div>
												<pre class="event-payload mt-2">{jsonText(call.data)}</pre>
											</div>
											<time class="activity-time">{formatTime(call.created_at)}</time>
										</div>
									{/each}
								{/if}
							</div>
						{:else}
							<div class="delivery-grid detail-scroll grid gap-0 lg:grid-cols-2">
								<div class="delivery-column lg:border-r">
									<div class="delivery-heading">
										<span>Git lifecycle</span><span>{selected.commit_intents.length}</span>
									</div>
									{#if selected.commit_intents.length === 0}<div class="panel-empty">
											No verified commit intent recorded.
										</div>{:else}
										{#each selected.commit_intents as commit (commit.commit_intent_id)}
											<div class="delivery-row">
												<div class="flex items-center justify-between gap-3">
													<span class="text-xs font-semibold">{commit.commit_message}</span><span
														class="gate-status"
														data-tone={statusTone(commit.push_status ?? commit.status)}
														>{commit.push_status ?? commit.status}</span
													>
												</div>
												<div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.65rem] app-muted">
													<span>SHA <code>{shortId(commit.commit_sha, 12)}</code></span><span
														>{commit.changed_paths.length} paths</span
													><span>{commit.push_remote ?? '—'}/{commit.push_branch ?? '—'}</span>
												</div>
											</div>
										{/each}
									{/if}
								</div>
								<div class="delivery-column">
									<div class="delivery-heading">
										<span>CI verification</span><span>{selected.ci_runs.length}</span>
									</div>
									{#if selected.ci_runs.length === 0}<div class="panel-empty">
											No CI execution recorded.
										</div>{:else}
										{#each selected.ci_runs as ci (ci.ci_run_id)}
											<div class="delivery-row">
												<div class="flex items-center justify-between gap-3">
													<span class="truncate text-xs font-semibold"
														>{ci.check_id || ci.repository}</span
													><span
														class="gate-status"
														data-tone={statusTone(ci.conclusion ?? ci.status)}
														>{ci.conclusion ?? ci.status}</span
													>
												</div>
												<div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.65rem] app-muted">
													<span>{ci.provider}</span><span
														>Rev <code>{shortId(ci.revision, 12)}</code></span
													><span>{relativeTime(ci.updated_at)}</span>
												</div>
												{#if ci.failure_summary}<p
														class="mt-2 text-[0.68rem] leading-relaxed"
														data-tone="danger"
													>
														{ci.failure_summary}
													</p>{/if}
											</div>
										{/each}
									{/if}
								</div>
								<div class="delivery-column border-t lg:col-span-2">
									<div class="delivery-heading">
										<span>Verified capability outcomes</span><span
											>{selected.capability_outcomes.length}</span
										>
									</div>
									{#if selected.capability_outcomes.length === 0}<div class="panel-empty">
											No proof-bound capability outcome recorded yet.
										</div>{:else}
										<div class="capability-table">
											{#each selected.capability_outcomes as outcome (outcome.outcome_id)}
												<div class="capability-row">
													<span
														class="status-dot"
														data-tone={outcome.verified_success ? 'success' : 'danger'}
													></span>
													<div class="min-w-0">
														<p class="truncate text-xs font-semibold">
															{outcome.stable_id}
															<span class="font-normal app-muted">v{outcome.version}</span>
														</p>
														<p class="mt-0.5 text-[0.62rem] app-muted">
															{outcome.repository_family} · {outcome.task_family} · {outcome.trust_status}
														</p>
													</div>
													<div class="text-right text-[0.62rem] app-muted">
														<p>{formatDuration(outcome.runtime_ms)}</p>
														<p>{formatCost(outcome.cost_microusd)}</p>
													</div>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							</div>
						{/if}
					</section>
				</div>
			{/if}
		</main>
	</div>
</div>

<style>
	.factory-root {
		min-width: 0;
		--factory-success: #16a36a;
		--factory-danger: #df4d58;
		--factory-warning: #d28a16;
		--factory-info: var(--app-accent);
	}
	:global(.dark) .factory-root {
		--factory-success: #4fd6a0;
		--factory-danger: #ff7b86;
		--factory-warning: #f4b860;
	}
	.factory-layout {
		display: grid;
		grid-template-columns: 18rem minmax(0, 1fr);
	}
	.run-rail {
		display: grid;
		grid-template-rows: auto minmax(0, 1fr);
		background: color-mix(in oklab, var(--app-surface) 90%, var(--app-bg));
	}
	.run-rail-header {
		border-color: var(--app-divider);
	}
	.stream-indicator {
		display: inline-flex;
		min-height: 1.75rem;
		align-items: center;
		gap: 0.4rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0 0.55rem;
		font-size: 0.64rem;
		font-weight: 650;
		color: var(--app-fg-muted);
	}
	.stream-dot {
		width: 0.42rem;
		height: 0.42rem;
		flex: 0 0 auto;
		border-radius: 999px;
		background: var(--app-fg-subtle);
	}
	.stream-indicator[data-status='live'] .stream-dot,
	.stream-dot[data-live='true'] {
		background: var(--factory-success);
		box-shadow: 0 0 0 0.18rem color-mix(in oklab, var(--factory-success) 13%, transparent);
	}
	.stream-indicator[data-status='reconnecting'] .stream-dot {
		background: var(--factory-warning);
		animation: factory-pulse 1.1s ease-in-out infinite;
	}
	.stream-indicator[data-status='error'] .stream-dot {
		background: var(--factory-danger);
	}
	.filter-button {
		display: flex;
		min-height: 2rem;
		align-items: center;
		justify-content: center;
		gap: 0.3rem;
		border-radius: 0.6rem;
		padding: 0 0.35rem;
		font-size: 0.66rem;
		font-weight: 650;
	}
	.run-item {
		margin-bottom: 0.35rem;
		border: 1px solid transparent;
		border-radius: 0.8rem;
		padding: 0.72rem 0.75rem;
	}
	.run-item.selected {
		background: var(--app-active);
		border-color: color-mix(in oklab, var(--app-accent) 22%, transparent);
		box-shadow: inset 2px 0 0 var(--app-accent);
	}
	.run-skeleton {
		background: linear-gradient(
			90deg,
			var(--app-surface-subtle),
			var(--app-surface-raised),
			var(--app-surface-subtle)
		);
		background-size: 200% 100%;
		animation: factory-shimmer 1.5s linear infinite;
	}
	.factory-main {
		min-width: 0;
		overscroll-behavior: contain;
		background:
			radial-gradient(
				circle at 50% -10rem,
				color-mix(in oklab, var(--app-accent) 4%, transparent),
				transparent 38rem
			),
			var(--app-bg);
	}
	.factory-mark {
		color: var(--app-accent);
		background: var(--app-accent-soft);
		border-color: color-mix(in oklab, var(--app-accent) 24%, transparent);
	}
	.run-hero {
		padding: 0.25rem 0.15rem;
	}
	.status-badge {
		display: inline-flex;
		align-items: center;
		gap: 0.42rem;
		min-height: 1.7rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0 0.58rem;
		font-size: 0.64rem;
		font-weight: 700;
		letter-spacing: 0.035em;
		text-transform: uppercase;
	}
	.status-badge.compact {
		min-height: 1.45rem;
		padding-inline: 0.45rem;
		font-size: 0.58rem;
	}
	.status-dot {
		width: 0.45rem;
		height: 0.45rem;
		flex: 0 0 auto;
		border-radius: 999px;
		background: var(--app-fg-subtle);
	}
	[data-tone='success'] {
		color: var(--factory-success);
	}
	[data-tone='danger'] {
		color: var(--factory-danger);
	}
	[data-tone='warning'] {
		color: var(--factory-warning);
	}
	[data-tone='info'] {
		color: var(--factory-info);
	}
	.status-dot[data-tone='success'],
	.activity-dot[data-tone='success'] {
		background: var(--factory-success);
	}
	.status-dot[data-tone='danger'],
	.activity-dot[data-tone='danger'] {
		background: var(--factory-danger);
	}
	.status-dot[data-tone='warning'] {
		background: var(--factory-warning);
	}
	.status-dot[data-tone='info'],
	.activity-dot[data-tone='info'] {
		background: var(--factory-info);
	}
	.next-action {
		max-width: 58rem;
		border-left: 2px solid color-mix(in oklab, var(--app-accent) 55%, transparent);
		padding-left: 0.75rem;
	}
	.hero-meta {
		min-width: min(100%, 24rem);
		border-left: 1px solid var(--app-divider);
		padding-left: 1rem;
	}
	.meta-label,
	.detail-label,
	.panel-kicker {
		font-size: 0.61rem;
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.meta-value {
		margin-top: 0.18rem;
		max-width: 12rem;
		font-size: 0.72rem;
		font-weight: 650;
	}
	.overall-progress {
		border: 1px solid color-mix(in oklab, var(--app-accent) 24%, var(--app-border));
		border-radius: 0.9rem;
		padding: 0.9rem 1rem;
		background: color-mix(in oklab, var(--app-accent-soft) 42%, var(--app-surface));
	}
	.progress-copy,
	.progress-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}
	.progress-phase {
		font-size: 0.9rem;
		font-weight: 720;
		line-height: 1.25;
		overflow-wrap: anywhere;
	}
	.progress-position,
	.progress-footer {
		font-size: 0.62rem;
		color: var(--app-fg-muted);
	}
	.progress-percent {
		flex: 0 0 auto;
		font-size: clamp(1.45rem, 3vw, 2.1rem);
		font-weight: 760;
		line-height: 1;
		letter-spacing: -0.04em;
		font-variant-numeric: tabular-nums;
	}
	.overall-progress-track {
		height: 0.48rem;
		margin-top: 0.8rem;
		overflow: hidden;
		border-radius: 999px;
		background: color-mix(in oklab, var(--app-border) 86%, transparent);
	}
	.overall-progress-track span {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: var(--app-accent);
		transition: width 220ms ease;
	}
	.progress-footer {
		margin-top: 0.55rem;
	}
	.pipeline-wrap {
		border-color: var(--app-divider);
	}
	.pipeline {
		display: flex;
		min-width: max-content;
	}
	.pipeline-stage {
		width: 5.85rem;
		flex: 0 0 auto;
		color: var(--app-fg-subtle);
	}
	.pipeline-node {
		position: relative;
		height: 1rem;
	}
	.pipeline-dot {
		position: relative;
		z-index: 2;
		display: block;
		width: 0.55rem;
		height: 0.55rem;
		border: 2px solid var(--app-bg);
		border-radius: 999px;
		background: var(--app-border);
		box-shadow: 0 0 0 1px var(--app-border);
	}
	.pipeline-line {
		position: absolute;
		z-index: 1;
		top: 0.26rem;
		left: 0.55rem;
		width: calc(100% - 0.55rem);
		height: 1px;
		background: var(--app-border);
	}
	.pipeline-stage[data-tone='done'] .pipeline-dot,
	.pipeline-stage[data-tone='done'] .pipeline-line {
		background: color-mix(in oklab, var(--app-accent) 62%, var(--app-border));
	}
	.pipeline-stage[data-tone='done'] {
		color: var(--app-fg-muted);
	}
	.pipeline-stage[data-tone='current'] {
		color: var(--app-fg);
	}
	.pipeline-stage[data-tone='current'] .pipeline-dot {
		background: var(--app-accent);
		box-shadow: 0 0 0 0.2rem var(--app-accent-soft);
	}
	.pipeline-label {
		display: block;
		width: 5.1rem;
		margin-top: 0.25rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.59rem;
		font-weight: 650;
	}
	.metric-strip {
		overflow: hidden;
		border-color: var(--app-border);
		border-radius: 0.8rem;
		background: color-mix(in oklab, var(--app-surface) 72%, transparent);
	}
	.metric-cell {
		min-height: 5.75rem;
		border-right: 1px solid var(--app-divider);
		padding: 0.8rem 0.9rem;
	}
	.metric-value {
		margin-top: 0.3rem;
		font-size: 1.2rem;
		font-weight: 700;
		line-height: 1;
		letter-spacing: -0.025em;
		font-variant-numeric: tabular-nums;
	}
	.metric-denominator {
		font-size: 0.78rem;
		font-weight: 550;
		color: var(--app-fg-muted);
	}
	.metric-caption {
		margin-top: 0.35rem;
		font-size: 0.62rem;
		color: var(--app-fg-muted);
	}
	.metric-progress {
		width: 100%;
		height: 0.16rem;
		margin-top: 0.55rem;
		overflow: hidden;
		border-radius: 999px;
		background: var(--app-border);
	}
	.metric-progress span {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: var(--app-accent);
		transition: width 240ms ease;
	}
	.factory-panel,
	.factory-detail {
		overflow: hidden;
		border-color: var(--app-border);
		border-radius: 0.85rem;
		background: color-mix(in oklab, var(--app-surface) 82%, transparent);
	}
	.panel-heading {
		display: flex;
		min-height: 3.45rem;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.65rem 0.85rem;
	}
	.panel-heading h3 {
		margin-top: 0.1rem;
		font-size: 0.8rem;
		font-weight: 700;
	}
	.cycle-number {
		font-family: var(--font-mono);
		font-size: 0.72rem;
		color: var(--app-fg-muted);
	}
	.panel-body {
		padding: 0.85rem;
	}
	.detail-copy {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: 0.75rem;
		line-height: 1.55;
		color: var(--app-fg-muted);
	}
	.chip-list {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.info-chip {
		display: inline-flex;
		min-height: 1.55rem;
		align-items: center;
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		border: 1px solid var(--app-border);
		border-radius: 0.45rem;
		padding: 0 0.45rem;
		font-family: var(--font-mono);
		font-size: 0.6rem;
		color: var(--app-fg-muted);
	}
	.info-chip.accent {
		border-color: color-mix(in oklab, var(--app-accent) 20%, transparent);
		background: var(--app-accent-soft);
		color: var(--app-accent-strong);
	}
	.revision-row {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		border-color: var(--app-divider);
	}
	.revision-row > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.2rem;
		padding: 0.65rem 0.8rem;
	}
	.revision-row span {
		font-size: 0.58rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--app-fg-subtle);
	}
	.revision-row code,
	.revision-row strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.66rem;
		white-space: nowrap;
	}
	.gate-row {
		display: grid;
		grid-template-columns: 4.1rem minmax(0, 1fr) auto;
		align-items: start;
		gap: 0.7rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.7rem 0.85rem;
	}
	.gate-row:last-child {
		border-bottom: 0;
	}
	.gate-status {
		display: inline-flex;
		min-height: 1.35rem;
		align-items: center;
		justify-content: center;
		border: 1px solid currentColor;
		border-radius: 0.38rem;
		padding: 0 0.35rem;
		font-size: 0.56rem;
		font-weight: 800;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		opacity: 0.9;
	}
	.panel-empty {
		padding: 1.2rem 0.85rem;
		text-align: center;
		font-size: 0.72rem;
		color: var(--app-fg-muted);
	}
	.worker-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.1rem 0.75rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.7rem 0.85rem;
	}
	.worker-row:last-child {
		border-bottom: 0;
	}
	.worker-row code {
		font-family: var(--font-mono);
		font-size: 0.62rem;
	}
	.failure-row {
		border: 1px solid color-mix(in oklab, var(--factory-danger) 22%, var(--app-border));
		border-radius: 0.55rem;
		padding: 0.55rem 0.6rem;
		background: color-mix(in oklab, var(--factory-danger) 4%, transparent);
	}
	.failure-row code {
		color: var(--factory-danger);
		font-size: 0.64rem;
	}
	.failure-row p {
		margin-top: 0.3rem;
		font-size: 0.65rem;
		line-height: 1.45;
		color: var(--app-fg-muted);
	}
	.failure-count {
		font-size: 0.62rem;
		font-weight: 750;
		color: var(--factory-danger);
	}
	.approval-row {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		border: 1px solid var(--app-border);
		border-radius: 0.55rem;
		padding: 0.55rem 0.6rem;
	}
	.detail-tabs {
		display: flex;
		overflow-x: auto;
		border-color: var(--app-divider);
		background: color-mix(in oklab, var(--app-surface-subtle) 80%, transparent);
	}
	.detail-tab {
		display: inline-flex;
		min-height: 2.8rem;
		align-items: center;
		gap: 0.45rem;
		border-bottom: 2px solid transparent;
		padding: 0 1rem;
		font-size: 0.68rem;
		font-weight: 650;
		color: var(--app-fg-muted);
	}
	.detail-tab.active {
		border-bottom-color: var(--app-accent);
		color: var(--app-fg);
		background: var(--app-active);
	}
	.tab-count {
		display: inline-flex;
		min-width: 1.2rem;
		height: 1.2rem;
		align-items: center;
		justify-content: center;
		border-radius: 0.35rem;
		padding: 0 0.25rem;
		background: var(--app-hover);
		font-size: 0.58rem;
		font-variant-numeric: tabular-nums;
	}
	.detail-scroll {
		max-height: 32rem;
		overflow-y: auto;
	}
	.activity-row {
		display: grid;
		grid-template-columns: 2.3rem 1rem minmax(0, 1fr) auto;
		gap: 0.35rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.7rem 0.85rem 0.7rem 0.45rem;
	}
	.activity-row:last-child {
		border-bottom: 0;
	}
	.activity-seq {
		padding-top: 0.05rem;
		text-align: right;
		font-family: var(--font-mono);
		font-size: 0.58rem;
		color: var(--app-fg-subtle);
	}
	.activity-rail {
		position: relative;
		display: flex;
		justify-content: center;
		padding-top: 0.2rem;
	}
	.activity-rail::after {
		position: absolute;
		top: 0.75rem;
		bottom: -0.75rem;
		width: 1px;
		content: '';
		background: var(--app-divider);
	}
	.activity-row:last-child .activity-rail::after {
		display: none;
	}
	.activity-dot {
		position: relative;
		z-index: 1;
		width: 0.42rem;
		height: 0.42rem;
		border-radius: 999px;
		background: var(--app-fg-subtle);
		box-shadow: 0 0 0 3px var(--app-surface);
	}
	.activity-time {
		padding-top: 0.05rem;
		font-family: var(--font-mono);
		font-size: 0.58rem;
		color: var(--app-fg-subtle);
		white-space: nowrap;
	}
	.event-payload {
		max-height: 10rem;
		margin-top: 0.45rem;
		overflow: auto;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		border-left: 1px solid var(--app-divider);
		padding-left: 0.55rem;
		font-family: var(--font-mono);
		font-size: 0.6rem;
		line-height: 1.45;
		color: var(--app-fg-muted);
	}
	.evidence-row,
	.reasoning-row,
	.delivery-row {
		border-bottom: 1px solid var(--app-divider);
		padding: 0.8rem 0.9rem;
	}
	.evidence-row:last-child,
	.reasoning-row:last-child,
	.delivery-row:last-child {
		border-bottom: 0;
	}
	.reasoning-row {
		display: grid;
		grid-template-columns: 8rem minmax(0, 1fr) auto;
		gap: 0.8rem;
	}
	.reasoning-role {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.2rem;
	}
	.reasoning-role span {
		font-size: 0.66rem;
		font-weight: 750;
		letter-spacing: 0.04em;
		color: var(--app-accent);
	}
	.reasoning-role small {
		font-size: 0.58rem;
		color: var(--app-fg-subtle);
	}
	.delivery-column {
		min-width: 0;
		border-color: var(--app-divider);
	}
	.delivery-heading {
		display: flex;
		min-height: 2.6rem;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0 0.9rem;
		font-size: 0.64rem;
		font-weight: 700;
		color: var(--app-fg-muted);
	}
	.capability-table {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	.capability-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 0.6rem;
		border-right: 1px solid var(--app-divider);
		border-bottom: 1px solid var(--app-divider);
		padding: 0.65rem 0.85rem;
	}
	.capability-row:nth-child(2n) {
		border-right: 0;
	}
	@keyframes factory-pulse {
		50% {
			opacity: 0.38;
			transform: scale(0.8);
		}
	}
	@keyframes factory-shimmer {
		to {
			background-position: -200% 0;
		}
	}
	@media (max-width: 1023px) {
		.factory-layout {
			grid-template-columns: 1fr;
			grid-template-rows: auto minmax(0, 1fr);
		}
		.run-rail {
			grid-template-columns: 13rem minmax(0, 1fr);
			grid-template-rows: auto;
			border-right: 0;
			border-bottom: 1px solid var(--app-border);
		}
		.run-list {
			display: flex;
			gap: 0.4rem;
			overflow-x: auto;
			overflow-y: hidden;
			padding: 0.45rem;
		}
		.run-item {
			width: 15.5rem;
			min-width: 15.5rem;
			margin-bottom: 0;
		}
		.run-skeleton {
			min-width: 14rem;
		}
		.run-rail-header {
			border-right: 1px solid var(--app-divider);
			border-bottom: 0;
		}
		.hero-meta {
			border-left: 0;
			padding-left: 0;
		}
	}
	@media (max-width: 639px) {
		.factory-content {
			padding: 0.7rem;
		}
		.run-rail {
			display: block;
			max-width: 100vw;
		}
		.run-rail-header {
			padding: 0.6rem 0.7rem;
		}
		.filter-button {
			min-height: 2.75rem;
		}
		.stream-indicator {
			min-height: 2rem;
		}
		.run-hero h2,
		.next-action p,
		.detail-copy {
			overflow-wrap: anywhere;
		}
		.run-rail-header {
			border-right: 0;
			border-bottom: 1px solid var(--app-divider);
		}
		.run-list {
			max-height: none;
			padding: 0.4rem 0.5rem;
			scroll-snap-type: x proximity;
		}
		.run-item {
			width: min(78vw, 15rem);
			min-width: min(78vw, 15rem);
			min-height: 4.75rem;
			padding: 0.62rem 0.7rem;
			scroll-snap-align: start;
		}
		.run-item p.mt-2 {
			-webkit-line-clamp: 1;
			line-clamp: 1;
		}
		.hero-meta {
			width: 100%;
			min-width: 0;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.7rem 1rem;
		}
		.meta-value {
			max-width: 100%;
		}
		.overall-progress {
			padding: 0.8rem;
		}
		.progress-copy {
			align-items: flex-start;
		}
		.progress-footer {
			align-items: flex-start;
			flex-direction: column;
			gap: 0.2rem;
		}
		.pipeline-wrap {
			margin-inline: -0.7rem;
			padding-inline: 0.7rem;
		}
		.metric-cell {
			min-height: 5rem;
		}
		.revision-row {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.gate-row {
			grid-template-columns: 3.8rem minmax(0, 1fr);
		}
		.gate-row > :last-child {
			grid-column: 2;
		}
		.reasoning-row {
			grid-template-columns: 1fr;
		}
		.reasoning-role {
			flex-direction: row;
			align-items: center;
		}
		.activity-row {
			grid-template-columns: 1.8rem 0.8rem minmax(0, 1fr);
		}
		.activity-time {
			grid-column: 3;
		}
		.capability-table {
			grid-template-columns: 1fr;
		}
		.capability-row {
			border-right: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.stream-indicator[data-status='reconnecting'] .stream-dot,
		.run-skeleton {
			animation: none;
		}
		.metric-progress span,
		.overall-progress-track span {
			transition: none;
		}
	}
</style>
