<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getMcpServicesMaintainJob,
		getMcpServicesSnapshot,
		openMcpServicesStream,
		startMcpServicesMaintain,
		type McpMaintainJob,
		type McpServiceBand,
		type McpServiceStatus,
		type McpServicesSnapshot
	} from '$lib/apis/mcp';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';

	let snapshot = $state<McpServicesSnapshot | null>(null);
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

	function bandClass(band: McpServiceBand | string): string {
		if (band === 'healthy') return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300';
		if (band === 'moderate') return 'border-amber-500/40 bg-amber-500/10 text-amber-300';
		return 'border-rose-500/40 bg-rose-500/10 text-rose-300';
	}

	function applySnapshot(next: McpServicesSnapshot) {
		snapshot = next;
		errorMessage = null;
		streamStatus = 'live';
		const activeId = next.maintain?.active_job_id;
		if (activeId && (!maintainJob || maintainJob.job_id !== activeId)) {
			void refreshJob(activeId);
		}
	}

	async function loadSnapshot() {
		try {
			const next = await getMcpServicesSnapshot();
			applySnapshot(next);
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Failed to load services snapshot';
			streamStatus = 'error';
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
		void loadSnapshot().then(() => connectStream());
		pollTimer = setInterval(() => {
			if (streamStatus !== 'live') void loadSnapshot();
		}, 8000);
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

<div class="flex h-full min-h-0 flex-col overflow-hidden">
	<header class="app-surface shrink-0 border-b px-3 py-3 sm:px-4">
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
				<span class="text-[0.7rem] app-muted">{snapshot.generated_at}</span>
			{/if}
			<div class="ml-auto flex flex-wrap gap-2">
				<button
					class="app-interactive rounded-lg border px-3 py-1.5 text-xs font-medium disabled:opacity-50"
					disabled={jobRunning}
					onclick={() => runMaintain('all')}
				>
					{jobRunning ? 'Maintaining…' : 'Maintain all'}
				</button>
				<button
					class="app-interactive rounded-lg border px-3 py-1.5 text-xs font-medium"
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

	<div class="min-h-0 flex-1 overflow-y-auto p-3 sm:p-4">
		{#if !snapshot}
			<p class="text-sm app-muted">Loading services snapshot…</p>
		{:else}
			<!-- Plugin identity -->
			<section class="app-subtle-surface mb-4 rounded-xl border p-3">
				<div class="mb-2 flex flex-wrap items-center gap-2">
					<h2 class="text-sm font-semibold">Plugin</h2>
					<span class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(plugin?.band ?? 'unhealthy')}"
						>{plugin?.band ?? 'unknown'}</span
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
						<p class="font-medium">{plugin?.refresh_required === true ? 'yes' : plugin?.refresh_required === false ? 'no' : '—'}</p>
					</div>
				</div>
				{#if plugin?.refresh_required}
					<p class="mt-2 text-xs text-amber-300">
						ChatGPT host must refresh the frozen tool snapshot (Settings → Apps → CPTR Computer → Refresh).
					</p>
				{/if}
			</section>

			<!-- Service grid -->
			<section class="grid gap-3 md:grid-cols-2">
				{#each services as service (service.id)}
					<article class="app-subtle-surface rounded-xl border p-3">
						<div class="mb-2 flex flex-wrap items-center gap-2">
							<h3 class="text-sm font-semibold">{service.name}</h3>
							<span class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(service.band)}"
								>{service.band}</span
							>
							<span class="text-[0.65rem] app-muted">score {service.score.toFixed(2)}</span>
							<button
								class="ml-auto app-interactive rounded-md border px-2 py-1 text-[0.65rem] disabled:opacity-50"
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
							class="text-[0.7rem] app-muted underline-offset-2 hover:underline"
							onclick={() => toggleService(service.id)}
						>
							{expandedServiceId === service.id ? 'Hide probes' : 'Why this band'}
						</button>
						{#if expandedServiceId === service.id}
							<ul class="mt-2 space-y-1.5 text-[0.7rem]">
								{#each service.probes as probe}
									<li class="rounded-lg border border-white/5 px-2 py-1.5">
										<div class="flex items-center gap-2">
											<span class="font-medium">{probe.id}</span>
											<span class="rounded border px-1.5 py-0.5 text-[0.6rem] uppercase {bandClass(probe.band_hint)}"
												>{probe.band_hint}</span
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
				<section class="app-subtle-surface mt-4 rounded-xl border p-3">
					<div class="mb-2 flex flex-wrap items-center gap-2">
						<h2 class="text-sm font-semibold">Maintain job</h2>
						<span class="text-[0.7rem] app-muted">{maintainJob.job_id}</span>
						<span class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase">{maintainJob.status}</span>
						{#if maintainJob.post_band}
							<span class="rounded-full border px-2 py-0.5 text-[0.65rem] uppercase {bandClass(maintainJob.post_band)}"
								>post {maintainJob.post_band}</span
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
									<pre class="mt-1 max-h-24 overflow-auto whitespace-pre-wrap app-muted">{JSON.stringify(step.evidence, null, 0)}</pre>
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
