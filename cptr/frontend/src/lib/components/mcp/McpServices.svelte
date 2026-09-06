<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getMcpActionTrace,
		getMcpServicesMaintainJob,
		getMcpServicesSnapshot,
		openMcpServicesStream,
		startMcpServicesMaintain,
		type McpActionTraceDetail,
		type McpActionTraceLayer,
		type McpActionTraceStatus,
		type McpActionTraceSummary,
		type McpMaintainJob,
		type McpMaintenanceSystemStatus,
		type McpServiceBand,
		type McpServiceStatus,
		type McpServicesSnapshot,
		type McpServicesTelemetry
	} from '$lib/apis/mcp';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';
	type WorkerWatchdogSnapshot = {
		aggregate?: McpServiceBand;
		worker_count?: number;
		workers?: Record<
			string,
			{
				status?: string;
				task_running?: boolean;
				restart_count_window?: number;
				restart_limit?: number;
				last_error?: string | null;
			}
		>;
	};

	let snapshot = $state<McpServicesSnapshot | null>(null);
	let telemetry = $state<McpServicesTelemetry | null>(null);
	let traces = $state<McpActionTraceSummary[]>([]);
	let traceSequence = $state(0);
	let expandedTraceId = $state<string | null>(null);
	let traceDetail = $state<McpActionTraceDetail | null>(null);
	let traceLoading = $state(false);
	let traceError = $state<string | null>(null);
	let streamStatus = $state<StreamStatus>('loading');
	let errorMessage = $state<string | null>(null);
	let closeStream: (() => void) | null = null;
	let connectionGeneration = 0;
	let maintainJob = $state<McpMaintainJob | null>(null);
	let maintainBusy = $state(false);
	let maintainError = $state<string | null>(null);
	let expandedServiceId = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const aggregate = $derived(snapshot?.aggregate ?? 'unhealthy');
	const services = $derived(snapshot?.services ?? []);
	const plugin = $derived(snapshot?.plugin ?? null);
	const jobRunning = $derived(
		maintainBusy || maintainJob?.status === 'queued' || maintainJob?.status === 'running'
	);
	const backendService = $derived(serviceById('backend'));
	const workerWatchdog = $derived.by(() => {
		const value = backendService?.stats?.worker_watchdog;
		return value && typeof value === 'object' ? (value as WorkerWatchdogSnapshot) : null;
	});
	const commandTelemetry = $derived(telemetry?.execution.commands ?? {});
	const hostTelemetry = $derived(telemetry?.host ?? null);

	function formatMs(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value)) return '—';
		if (value < 1) return `${value.toFixed(2)} ms`;
		if (value < 100) return `${value.toFixed(1)} ms`;
		return `${Math.round(value)} ms`;
	}

	function formatBytes(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value) || value < 0) return '—';
		if (value < 1024) return `${Math.round(value)} B`;
		const units = ['KB', 'MB', 'GB', 'TB'];
		let current = value / 1024;
		let index = 0;
		while (current >= 1024 && index < units.length - 1) {
			current /= 1024;
			index += 1;
		}
		return `${current >= 10 ? current.toFixed(0) : current.toFixed(1)} ${units[index]}`;
	}

	function formatPercent(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value)) return '—';
		return `${value.toFixed(value < 10 ? 1 : 0)}%`;
	}

	function usedPercent(
		total: number | null | undefined,
		available: number | null | undefined
	): number | null {
		if (!total || total <= 0 || available == null) return null;
		return Math.max(0, Math.min(100, ((total - available) / total) * 100));
	}

	function formatClock(value: string | number | null | undefined): string {
		if (value == null) return '—';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '—';
		return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
	}

	const traceLayerLabels: Record<McpActionTraceLayer, string> = {
		chatgpt: 'ChatGPT',
		mcp: 'MCP',
		backend: 'Backend',
		command: 'Command',
		workbench: 'Workbench',
		browser: 'Browser',
		cleanup: 'Cleanup'
	};

	function traceStatusClass(status: McpActionTraceStatus): string {
		if (status === 'ok') return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300';
		if (status === 'error') return 'border-rose-500/40 bg-rose-500/10 text-rose-300';
		if (status === 'cancelled') return 'border-slate-500/40 bg-slate-500/10 text-slate-300';
		return 'border-sky-500/40 bg-sky-500/10 text-sky-300';
	}

	function shortTraceId(traceId: string): string {
		return traceId.length <= 18 ? traceId : `${traceId.slice(0, 8)}…${traceId.slice(-6)}`;
	}

	function bandClass(band: McpServiceBand | string): string {
		if (band === 'healthy') return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300';
		if (band === 'moderate') return 'border-amber-500/40 bg-amber-500/10 text-amber-300';
		return 'border-rose-500/40 bg-rose-500/10 text-rose-300';
	}

	function systemStatusBand(status: McpMaintenanceSystemStatus | null | undefined): McpServiceBand {
		if (status === 'STABLE') return 'healthy';
		if (status === 'FAILED') return 'unhealthy';
		return 'moderate';
	}

	function workerStatusBand(status: string | undefined): McpServiceBand {
		if (status === 'healthy' || status === 'restarted') return 'healthy';
		if (status === 'stalled' || status === 'degraded') return 'moderate';
		return 'unhealthy';
	}

	function applySnapshot(next: McpServicesSnapshot, fromStream = true) {
		snapshot = next;
		errorMessage = null;
		if (fromStream) streamStatus = 'live';
		const activeId = next.maintain?.active_job_id;
		if (activeId && (!maintainJob || maintainJob.job_id !== activeId)) {
			void refreshJob(activeId);
		}
	}

	async function loadSnapshot() {
		try {
			const next = await getMcpServicesSnapshot();
			applySnapshot(next, false);
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Failed to load services snapshot';
			streamStatus = 'error';
		}
	}

	async function toggleTrace(traceId: string) {
		if (expandedTraceId === traceId) {
			expandedTraceId = null;
			traceDetail = null;
			traceError = null;
			return;
		}
		expandedTraceId = traceId;
		traceDetail = null;
		traceError = null;
		traceLoading = true;
		try {
			const detail = await getMcpActionTrace(traceId);
			if (expandedTraceId === traceId) traceDetail = detail;
		} catch (error) {
			if (expandedTraceId === traceId) {
				traceError = error instanceof Error ? error.message : 'Failed to load action trace';
			}
		} finally {
			if (expandedTraceId === traceId) traceLoading = false;
		}
	}

	function connectStream() {
		closeStream?.();
		const generation = ++connectionGeneration;
		streamStatus = snapshot ? 'reconnecting' : 'loading';
		closeStream = openMcpServicesStream({
			onSnapshot: (next) => {
				if (generation !== connectionGeneration) return;
				applySnapshot(next);
			},
			onTelemetry: (next) => {
				if (generation !== connectionGeneration) return;
				telemetry = next;
			},
			onTraces: (next) => {
				if (generation !== connectionGeneration || next.sequence < traceSequence) return;
				traceSequence = next.sequence;
				traces = next.traces;
			},
			onOpen: () => {
				if (generation !== connectionGeneration) return;
				streamStatus = 'live';
			},
			onError: () => {
				if (generation !== connectionGeneration) return;
				streamStatus = 'reconnecting';
				// Fall back to poll while stream is down
				void loadSnapshot();
			}
		});
	}

	async function refreshJob(jobId: string) {
		try {
			const job = await getMcpServicesMaintainJob(jobId);
			maintainJob = job;
			if (job.status === 'queued' || job.status === 'running') {
				maintainBusy = true;
			} else {
				maintainBusy = false;
				await loadSnapshot();
			}
		} catch (error) {
			maintainError = error instanceof Error ? error.message : 'Failed to load maintain job';
			maintainBusy = false;
		}
	}

	async function runMaintain(serviceId: string = 'all') {
		if (jobRunning) return;
		maintainError = null;
		maintainBusy = true;
		const idempotencyKey = crypto.randomUUID();
		try {
			const started = await startMcpServicesMaintain(serviceId, idempotencyKey);
			await refreshJob(started.job_id);
			// Poll job until terminal
			const deadline = Date.now() + 60_000;
			while (Date.now() < deadline) {
				const job = await getMcpServicesMaintainJob(started.job_id);
				maintainJob = job;
				if (job.status !== 'queued' && job.status !== 'running') break;
				await new Promise((r) => setTimeout(r, 400));
			}
			maintainBusy = false;
			await loadSnapshot();
		} catch (error) {
			maintainError = error instanceof Error ? error.message : 'Maintain failed';
			maintainBusy = false;
		}
	}

	onMount(() => {
		connectStream();
		pollTimer = setInterval(() => {
			if (streamStatus !== 'live') void loadSnapshot();
		}, 10_000);
	});

	onDestroy(() => {
		connectionGeneration += 1;
		closeStream?.();
		if (pollTimer) clearInterval(pollTimer);
	});

	function toggleService(id: string) {
		expandedServiceId = expandedServiceId === id ? null : id;
	}

	function serviceById(id: string): McpServiceStatus | undefined {
		return services.find((s) => s.id === id);
	}
</script>

<div class="services-shell flex h-full min-h-0 flex-col overflow-hidden">
	<header
		class="services-header mobile-edge-padding app-surface shrink-0 border-b px-3 py-3 sm:px-4"
	>
		<div class="flex flex-wrap items-center gap-2">
			<span
				class="rounded-full border px-2.5 py-1 text-xs font-semibold uppercase tracking-wide {bandClass(
					aggregate
				)}"
			>
				{aggregate}
			</span>
			<p class="text-sm font-medium">Services health</p>
			<span class="text-[0.7rem] app-muted">
				{streamStatus === 'live'
					? 'live'
					: streamStatus === 'reconnecting'
						? 'reconnecting…'
						: streamStatus}
			</span>
			{#if snapshot?.generated_at}
				<span class="text-[0.7rem] tabular-nums app-muted"
					>{formatClock(snapshot.generated_at)}</span
				>
			{/if}
			<div class="services-actions ml-auto flex flex-wrap gap-2">
				<button
					class="touch-target app-interactive rounded-lg border px-3 py-1.5 text-xs font-medium disabled:opacity-50"
					disabled={jobRunning}
					onclick={() => runMaintain('all')}
				>
					{jobRunning ? 'Stabilizing…' : 'Stabilize All'}
				</button>
				<button
					class="touch-target app-interactive rounded-lg border px-3 py-1.5 text-xs font-medium"
					onclick={() => loadSnapshot()}
				>
					Refresh
				</button>
			</div>
		</div>
		{#if errorMessage}
			<p class="mt-2 text-xs text-rose-300">{errorMessage}</p>
		{/if}
		{#if maintainError}
			<p class="mt-2 text-xs text-rose-300">{maintainError}</p>
		{/if}
	</header>

	<div
		class="services-scroll mobile-edge-padding mobile-safe-bottom min-h-0 flex-1 overflow-y-auto p-3 sm:p-4"
	>
		{#if !snapshot}
			<p class="text-sm app-muted">Loading services snapshot…</p>
		{:else}
			{#if telemetry}
				<section class="observability-section app-subtle-surface mb-4 rounded-xl border p-3">
					<div class="mb-3 flex items-center justify-between gap-3">
						<div>
							<h2 class="text-sm font-semibold">Runtime overview</h2>
							<p class="mt-0.5 text-[0.68rem] app-muted">
								Bounded telemetry · no payloads or browser frames
							</p>
						</div>
						<span class="shrink-0 text-[0.68rem] tabular-nums app-muted">
							{formatClock(telemetry.generated_at_ms)}
						</span>
					</div>

					<div class="observability-grid grid grid-cols-2 gap-2 lg:grid-cols-3 xl:grid-cols-6">
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Active MCP</p>
							<p class="metric-value">{telemetry.mcp.active_requests}</p>
							<p class="metric-meta app-muted">
								{telemetry.mcp.session_count} sessions · {telemetry.mcp.client_count} clients
							</p>
						</article>
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Request p95</p>
							<p class="metric-value">{formatMs(telemetry.runtime.requests.p95_ms)}</p>
							<p class="metric-meta app-muted">
								{telemetry.runtime.requests.server_error_count} backend errors
							</p>
						</article>
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Commands</p>
							<p class="metric-value">
								{commandTelemetry.capacity_used ?? 0}/{commandTelemetry.capacity_limit ?? '—'}
							</p>
							<p class="metric-meta app-muted">
								{commandTelemetry.active ?? 0} active · {commandTelemetry.launching ?? 0} launching
							</p>
						</article>
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Workers</p>
							<p class="metric-value">{telemetry.workers.healthy}/{telemetry.workers.total}</p>
							<p class="metric-meta app-muted">{telemetry.workers.restarts} restarts in window</p>
						</article>
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Host CPU</p>
							<p class="metric-value">{formatPercent(hostTelemetry?.cpu_usage_percent)}</p>
							<p class="metric-meta app-muted">
								CPTR {formatPercent(hostTelemetry?.cptr_process?.cpu_percent)}
							</p>
						</article>
						<article class="metric-card rounded-lg border border-white/5 p-2.5">
							<p class="metric-label app-muted">Event loop</p>
							<p class="metric-value">{formatMs(telemetry.runtime.event_loop.last_lag_ms)}</p>
							<p class="metric-meta app-muted">
								{telemetry.runtime.process.open_fds ?? '—'} open FDs
							</p>
						</article>
					</div>

					<div class="mt-2 grid gap-2 lg:grid-cols-3">
						<details class="observability-detail rounded-lg border border-white/5">
							<summary
								class="touch-target flex cursor-pointer items-center px-3 text-xs font-medium"
								>Runtime details</summary
							>
							<dl class="detail-grid border-t border-white/5 px-3 py-2 text-[0.7rem]">
								<div>
									<dt>HTTP requests</dt>
									<dd>{telemetry.runtime.requests.count}</dd>
								</div>
								<div>
									<dt>HTTP errors</dt>
									<dd>{telemetry.runtime.requests.server_error_count}</dd>
								</div>
								<div>
									<dt>DB p95</dt>
									<dd>{formatMs(telemetry.runtime.database.p95_ms)}</dd>
								</div>
								<div>
									<dt>DB busy</dt>
									<dd>{telemetry.runtime.database.busy_count}</dd>
								</div>
								<div>
									<dt>DB errors</dt>
									<dd>{telemetry.runtime.database.error_count}</dd>
								</div>
								<div>
									<dt>CPTR RSS</dt>
									<dd>{formatBytes(telemetry.runtime.process.rss_bytes)}</dd>
								</div>
							</dl>
						</details>
						<details class="observability-detail rounded-lg border border-white/5">
							<summary
								class="touch-target flex cursor-pointer items-center px-3 text-xs font-medium"
								>Execution & pressure</summary
							>
							<dl class="detail-grid border-t border-white/5 px-3 py-2 text-[0.7rem]">
								<div>
									<dt>MCP total</dt>
									<dd>{telemetry.mcp.total_requests}</dd>
								</div>
								<div>
									<dt>MCP errors</dt>
									<dd>{telemetry.mcp.errors}</dd>
								</div>
								<div>
									<dt>Backend RTT p95</dt>
									<dd>{formatMs(telemetry.mcp.backend_rtt_p95_ms)}</dd>
								</div>
								<div>
									<dt>Retained commands</dt>
									<dd>{commandTelemetry.completed_retained ?? 0}</dd>
								</div>
								<div>
									<dt>Unreconciled exits</dt>
									<dd>{commandTelemetry.exited_unreconciled ?? 0}</dd>
								</div>
								<div>
									<dt>Live-event queue</dt>
									<dd>{telemetry.pressure.live_event_queue_percent.toFixed(2)}%</dd>
								</div>
							</dl>
						</details>
						<details class="observability-detail rounded-lg border border-white/5">
							<summary
								class="touch-target flex cursor-pointer items-center px-3 text-xs font-medium"
								>Host resources</summary
							>
							{#if hostTelemetry}
								<dl class="detail-grid border-t border-white/5 px-3 py-2 text-[0.7rem]">
									<div>
										<dt>Memory used</dt>
										<dd>
											{formatPercent(
												usedPercent(
													hostTelemetry.memory_total_bytes,
													hostTelemetry.memory_available_bytes
												)
											)}
										</dd>
									</div>
									<div>
										<dt>Memory free</dt>
										<dd>{formatBytes(hostTelemetry.memory_available_bytes)}</dd>
									</div>
									<div>
										<dt>Disk free</dt>
										<dd>{formatBytes(hostTelemetry.disk_free_bytes)}</dd>
									</div>
									<div>
										<dt>Network RX</dt>
										<dd>{formatBytes(hostTelemetry.network_rx_bytes_per_s)}/s</dd>
									</div>
									<div>
										<dt>Network TX</dt>
										<dd>{formatBytes(hostTelemetry.network_tx_bytes_per_s)}/s</dd>
									</div>
									<div>
										<dt>GPU</dt>
										<dd>{hostTelemetry.gpu_status}</dd>
									</div>
								</dl>
							{:else}
								<p class="border-t border-white/5 px-3 py-2 text-[0.7rem] app-muted">
									Host sampler has not produced a sample yet.
								</p>
							{/if}
						</details>
					</div>
				</section>
			{/if}

			<section class="trace-explorer deferred-panel app-subtle-surface mb-4 rounded-xl border p-3">
				<div class="mb-2 flex items-start justify-between gap-3">
					<div>
						<h2 class="text-sm font-semibold">Action traces</h2>
						<p class="mt-0.5 text-[0.68rem] app-muted">
							One correlation ID across ChatGPT → MCP → Backend → execution → Cleanup
						</p>
					</div>
					<span class="shrink-0 text-[0.68rem] tabular-nums app-muted">{traces.length} recent</span>
				</div>

				{#if traces.length === 0}
					<p class="rounded-lg border border-white/5 px-3 py-3 text-xs app-muted">
						No correlated action traces have arrived yet.
					</p>
				{:else}
					<ul class="trace-list space-y-2">
						{#each traces as trace (trace.trace_id)}
							<li class="trace-row overflow-hidden rounded-lg border border-white/5">
								<button
									class="touch-target trace-toggle app-interactive flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left"
									type="button"
									aria-expanded={expandedTraceId === trace.trace_id}
									aria-controls={`trace-detail-${trace.trace_id}`}
									onclick={() => toggleTrace(trace.trace_id)}
								>
									<span
										class="shrink-0 rounded-full border px-2 py-0.5 text-[0.62rem] font-semibold uppercase {traceStatusClass(
											trace.status
										)}"
									>
										{trace.status}
									</span>
									<span class="min-w-0 flex-1">
										<span class="block truncate text-xs font-medium">
											{trace.tool_name ?? 'MCP action'}
										</span>
										<span class="mt-0.5 block truncate text-[0.64rem] tabular-nums app-muted">
											{shortTraceId(trace.trace_id)} · {formatMs(trace.duration_ms)} · {trace.stage_count}
											stages
										</span>
									</span>
									<span class="shrink-0 text-[0.7rem] app-muted" aria-hidden="true">
										{expandedTraceId === trace.trace_id ? '−' : '+'}
									</span>
								</button>

								<div
									class="trace-layers flex min-w-0 gap-1 overflow-x-auto border-t border-white/5 px-3 py-2"
								>
									{#each trace.layers as layer, index (layer)}
										<span
											class="trace-layer-chip shrink-0 rounded border border-white/5 px-1.5 py-0.5 text-[0.6rem] app-muted"
										>
											{traceLayerLabels[layer]}
										</span>
										{#if index < trace.layers.length - 1}
											<span class="self-center text-[0.6rem] app-muted" aria-hidden="true">→</span>
										{/if}
									{/each}
								</div>

								{#if expandedTraceId === trace.trace_id}
									<div
										id={`trace-detail-${trace.trace_id}`}
										class="trace-detail border-t border-white/5 px-3 py-2"
									>
										{#if traceLoading}
											<p class="text-[0.7rem] app-muted" role="status">
												Loading bounded trace detail…
											</p>
										{:else if traceError}
											<p class="text-[0.7rem] text-rose-300">{traceError}</p>
										{:else if traceDetail?.trace_id === trace.trace_id}
											<ol class="trace-timeline space-y-1.5">
												{#each traceDetail.stages as stage (stage.sequence)}
													<li
														class="trace-stage grid min-w-0 grid-cols-[4.4rem_minmax(0,1fr)] gap-2 rounded-md border border-white/5 px-2 py-1.5 text-[0.68rem]"
													>
														<time
															class="tabular-nums app-muted"
															datetime={new Date(stage.timestamp_ms).toISOString()}
														>
															{formatClock(stage.timestamp_ms)}
														</time>
														<div class="min-w-0">
															<div class="flex min-w-0 flex-wrap items-center gap-1.5">
																<span class="font-medium">{traceLayerLabels[stage.layer]}</span>
																<span class="min-w-0 truncate">{stage.name}</span>
																<span
																	class="rounded border px-1 py-0.5 text-[0.58rem] uppercase {traceStatusClass(
																		stage.status
																	)}"
																>
																	{stage.status}
																</span>
															</div>
															{#if stage.entity_id || stage.error_code || stage.duration_ms != null}
																<p class="mt-0.5 break-all text-[0.62rem] app-muted">
																	{#if stage.entity_id}{stage.entity_type}: {stage.entity_id}{/if}
																	{#if stage.duration_ms != null}
																		· {formatMs(stage.duration_ms)}{/if}
																	{#if stage.error_code}
																		· {stage.error_code}{/if}
																</p>
															{/if}
														</div>
													</li>
												{/each}
											</ol>
										{/if}
									</div>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<!-- Plugin identity -->
			<section class="app-subtle-surface mb-4 rounded-xl border p-3">
				<div class="mb-2 flex flex-wrap items-center gap-2">
					<h2 class="text-sm font-semibold">Plugin</h2>
					<span
						class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(
							plugin?.band ?? 'unhealthy'
						)}">{plugin?.band ?? 'unknown'}</span
					>
				</div>
				<div class="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
					<div>
						<p class="app-muted">Version</p>
						<p class="font-medium">{plugin?.version ?? '—'}</p>
					</div>
					<div>
						<p class="app-muted">Contract</p>
						<p class="font-medium">{plugin?.contract_version ?? '—'}</p>
					</div>
					<div>
						<p class="app-muted">Tool count</p>
						<p class="font-medium">{plugin?.tool_count ?? '—'}</p>
					</div>
					<div>
						<p class="app-muted">Refresh required</p>
						<p class="font-medium">
							{plugin?.refresh_required === true
								? 'yes'
								: plugin?.refresh_required === false
									? 'no'
									: '—'}
						</p>
					</div>
				</div>
				{#if plugin?.refresh_required}
					<p class="mt-2 text-xs text-amber-300">
						ChatGPT host must refresh the frozen tool snapshot (Settings → Apps → CPTR Computer →
						Refresh).
					</p>
				{/if}
			</section>

			<!-- Service grid -->
			<section class="deferred-panel grid gap-3 md:grid-cols-2">
				{#each services as service (service.id)}
					<article class="app-subtle-surface rounded-xl border p-3">
						<div class="mb-2 flex flex-wrap items-center gap-2">
							<h3 class="text-sm font-semibold">{service.name}</h3>
							<span
								class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(
									service.band
								)}">{service.band}</span
							>
							<span class="text-[0.65rem] app-muted">score {service.score.toFixed(2)}</span>
							<button
								class="touch-target ml-auto app-interactive rounded-md border px-2 py-1 text-[0.65rem] disabled:opacity-50"
								disabled={jobRunning}
								onclick={() => runMaintain(service.id)}
							>
								Maintain
							</button>
						</div>
						{#if service.last_error}
							<p class="mb-1 text-[0.7rem] text-rose-300">{service.last_error}</p>
						{/if}
						<button
							class="touch-target inline-flex items-center text-[0.7rem] app-muted underline-offset-2 hover:underline"
							onclick={() => toggleService(service.id)}
						>
							{expandedServiceId === service.id ? 'Hide probes' : 'Why this band'}
						</button>
						{#if service.id === 'backend' && workerWatchdog?.workers}
							<div class="mt-2 border-t border-white/5 pt-2">
								<div class="flex items-center justify-between gap-2 text-[0.7rem]">
									<span class="font-medium">Background workers</span>
									<span class="app-muted">
										{workerWatchdog.worker_count ?? Object.keys(workerWatchdog.workers).length} supervised
									</span>
								</div>
								<ul class="mt-1.5 grid gap-1 sm:grid-cols-2">
									{#each Object.entries(workerWatchdog.workers) as [workerName, worker] (workerName)}
										<li class="flex min-w-0 items-center gap-2 text-[0.65rem]">
											<span class="min-w-0 flex-1 truncate">{workerName.replaceAll('_', ' ')}</span>
											<span
												class="rounded border px-1.5 py-0.5 uppercase {bandClass(
													workerStatusBand(worker.status)
												)}"
											>
												{worker.status ?? 'unknown'}
											</span>
										</li>
									{/each}
								</ul>
							</div>
						{/if}
						{#if expandedServiceId === service.id}
							<ul class="mt-2 space-y-1.5 text-[0.7rem]">
								{#each service.probes as probe}
									<li class="rounded-lg border border-white/5 px-2 py-1.5">
										<div class="flex items-center gap-2">
											<span class="font-medium">{probe.id}</span>
											<span
												class="rounded border px-1.5 py-0.5 text-[0.6rem] uppercase {bandClass(
													probe.band_hint
												)}">{probe.band_hint}</span
											>
											{#if probe.critical}
												<span class="text-[0.6rem] app-muted">critical</span>
											{/if}
										</div>
										<p class="app-muted">{probe.detail}</p>
									</li>
								{/each}
							</ul>
						{/if}
					</article>
				{/each}
			</section>

			<!-- Job drawer -->
			{#if maintainJob}
				<section class="deferred-panel app-subtle-surface mt-4 rounded-xl border p-3">
					<div
						class="mb-3 rounded-lg border border-white/5 px-2.5 py-2"
						role="status"
						aria-live="polite"
						aria-atomic="true"
					>
						<div class="flex flex-wrap items-center gap-2 text-xs">
							<span class="font-medium">
								{jobRunning
									? 'Running deterministic stabilization…'
									: maintainJob.system_status
										? `System ${maintainJob.system_status}`
										: 'Maintenance finished'}
							</span>
							{#if maintainJob.system_status}
								<span
									class="rounded-full border px-2 py-0.5 text-[0.65rem] font-semibold {bandClass(
										systemStatusBand(maintainJob.system_status)
									)}"
								>
									{maintainJob.system_status}
								</span>
							{/if}
							<span class="ml-auto app-muted">pass {maintainJob.pass_count || 1} / 2 max</span>
						</div>
						{#if jobRunning}
							<progress class="mt-2 h-1.5 w-full" max="100" aria-label="Stabilization in progress"
							></progress>
						{:else}
							<progress
								class="mt-2 h-1.5 w-full"
								max="100"
								value="100"
								aria-label="Stabilization complete"
							></progress>
						{/if}
					</div>
					<div class="mb-2 flex flex-wrap items-center gap-2">
						<h2 class="text-sm font-semibold">
							{maintainJob.service_id === 'all' ? 'Stabilization run' : 'Maintenance run'}
						</h2>
						<span class="text-[0.7rem] app-muted">{maintainJob.job_id}</span>
						<span class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase"
							>{maintainJob.status}</span
						>
						{#if maintainJob.post_band}
							<span
								class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(
									maintainJob.post_band
								)}">post {maintainJob.post_band}</span
							>
						{/if}
					</div>
					<ol class="space-y-1.5 text-[0.7rem]">
						{#each maintainJob.steps as step}
							<li class="rounded-lg border border-white/5 px-2 py-1.5">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-medium">{step.step_id}</span>
									<span class="app-muted">{step.result ?? '…'}</span>
								</div>
								{#if step.evidence && Object.keys(step.evidence).length}
									<pre
										class="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-all app-muted">{JSON.stringify(
											step.evidence,
											null,
											0
										)}</pre>
								{/if}
							</li>
						{/each}
					</ol>
					{#if maintainJob.error}
						<p class="mt-2 text-xs text-rose-300">{maintainJob.error}</p>
					{/if}
				</section>
			{/if}
		{/if}
	</div>
</div>

<style>
	.metric-card,
	.detail-grid dd,
	.services-header {
		font-variant-numeric: tabular-nums;
	}

	.metric-card {
		min-width: 0;
		background: color-mix(in oklab, var(--app-surface-subtle) 88%, transparent);
	}

	.metric-label {
		font-size: 0.68rem;
	}

	.metric-value {
		margin-top: 0.15rem;
		font-size: 1.05rem;
		font-weight: 650;
		line-height: 1.25;
		letter-spacing: -0.015em;
	}

	.metric-meta {
		margin-top: 0.25rem;
		font-size: 0.64rem;
		line-height: 1.25;
	}

	.observability-detail > summary {
		min-height: 2.75rem;
		list-style-position: inside;
	}

	.detail-grid {
		display: grid;
		gap: 0.4rem;
	}

	.detail-grid > div {
		display: flex;
		min-width: 0;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.75rem;
	}

	.detail-grid dt {
		color: var(--app-muted);
	}

	.detail-grid dd {
		min-width: 0;
		text-align: right;
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.trace-row {
		background: color-mix(in oklab, var(--app-surface-subtle) 84%, transparent);
	}

	.trace-toggle {
		min-height: 3rem;
	}

	.trace-layers {
		scrollbar-width: none;
		overscroll-behavior-x: contain;
	}

	.trace-layers::-webkit-scrollbar {
		display: none;
	}

	.trace-stage {
		content-visibility: auto;
		contain-intrinsic-size: auto 2.75rem;
	}

	.deferred-panel {
		content-visibility: auto;
		contain-intrinsic-size: auto 320px;
	}

	@media (max-width: 767px) {
		.services-header,
		.services-scroll {
			padding-left: max(0.75rem, env(safe-area-inset-left, 0px));
			padding-right: max(0.75rem, env(safe-area-inset-right, 0px));
		}

		.services-scroll {
			overscroll-behavior: contain;
			-webkit-overflow-scrolling: touch;
		}

		.services-actions {
			margin-left: 0;
			width: 100%;
			display: grid;
			grid-template-columns: minmax(0, 1fr) auto;
		}

		.services-actions > button {
			min-height: 2.75rem;
		}

		.metric-card {
			min-height: 5.25rem;
		}

		.observability-section {
			padding: 0.65rem;
		}
	}
</style>
