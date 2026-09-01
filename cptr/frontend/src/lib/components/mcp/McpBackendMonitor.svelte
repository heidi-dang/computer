<script lang="ts">
	import type { McpBackendMetricsState } from '$lib/stores/mcp-diagnostics';

	type StreamHealth = {
		subscriberCount: number;
		slowSubscriberDrops: number;
	};

	type Props = {
		history?: McpBackendMetricsState[];
		streamHealth?: StreamHealth | null;
	};

	let { history = [], streamHealth = null }: Props = $props();

	const latest = $derived(history.at(-1) ?? null);
	const ramPercent = $derived(
		latest?.memoryTotalBytes && latest.memoryAvailableBytes != null
			? ((latest.memoryTotalBytes - latest.memoryAvailableBytes) / latest.memoryTotalBytes) * 100
			: null
	);
	const diskPercent = $derived(
		latest?.diskTotalBytes && latest.diskUsedBytes != null
			? (latest.diskUsedBytes / latest.diskTotalBytes) * 100
			: null
	);
	const telemetryHealth = $derived(
		!latest ? 'Unavailable' : (streamHealth?.slowSubscriberDrops ?? 0) > 0 ? 'Degraded' : 'Live'
	);

	function percent(value: number | null): string {
		return value == null ? 'Unavailable' : `${Math.max(0, Math.min(100, value)).toFixed(1)}%`;
	}

	function bytes(value: number | null): string {
		if (value == null) return 'Unavailable';
		if (value < 1024) return `${value.toFixed(0)} B`;
		if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
		if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
		return `${(value / 1024 ** 3).toFixed(1)} GB`;
	}

	function rate(value: number | null): string {
		return value == null ? 'Unavailable' : `${bytes(value)}/s`;
	}

	function iops(read: number | null, write: number | null): string {
		if (read == null && write == null) return 'Unavailable';
		return `${(read ?? 0).toFixed(1)} / ${(write ?? 0).toFixed(1)} ops/s`;
	}

	function uptime(seconds: number | null): string {
		if (seconds == null) return 'Unavailable';
		const days = Math.floor(seconds / 86_400);
		const hours = Math.floor((seconds % 86_400) / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		return `${days ? `${days}d ` : ''}${hours}h ${minutes}m`;
	}

	function points(values: Array<number | null>): string {
		const normalized = values.map((value) =>
			value == null || !Number.isFinite(value) ? 0 : value
		);
		if (normalized.length === 0) return '';
		const max = Math.max(1, ...normalized);
		return normalized
			.map((value, index) => {
				const x = normalized.length === 1 ? 100 : (index / (normalized.length - 1)) * 100;
				const y = 28 - (value / max) * 24;
				return `${x.toFixed(2)},${y.toFixed(2)}`;
			})
			.join(' ');
	}

	const cpuSpark = $derived(points(history.map((sample) => sample.cpuUsagePercent)));
	const ramSpark = $derived(
		points(
			history.map((sample) =>
				sample.memoryTotalBytes && sample.memoryAvailableBytes != null
					? ((sample.memoryTotalBytes - sample.memoryAvailableBytes) / sample.memoryTotalBytes) *
						100
					: null
			)
		)
	);
	const diskSpark = $derived(
		points(
			history.map((sample) => (sample.diskReadBytesPerS ?? 0) + (sample.diskWriteBytesPerS ?? 0))
		)
	);
	const networkSpark = $derived(
		points(
			history.map((sample) => (sample.networkRxBytesPerS ?? 0) + (sample.networkTxBytesPerS ?? 0))
		)
	);
</script>

<section class="space-y-3" aria-label="CPTR Backend system monitor">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<div>
			<h3 class="text-xs font-semibold">Live system monitor</h3>
			<p class="mt-0.5 text-[0.65rem] app-muted">Bounded host telemetry from CPTR Backend</p>
		</div>
		<span class="app-subtle-surface rounded-full border px-2 py-1 text-[0.65rem] app-muted">
			Telemetry health · {telemetryHealth}
		</span>
	</div>

	<div class="grid grid-cols-2 gap-2 md:grid-cols-4">
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">CPU</p>
			<p class="mt-1 text-sm font-semibold tabular-nums">
				{percent(latest?.cpuUsagePercent ?? null)}
			</p>
			<p class="mt-0.5 text-[0.62rem] app-muted">{latest?.cpuCount ?? 0} logical cores</p>
			<svg viewBox="0 0 100 30" class="mt-2 h-8 w-full" aria-hidden="true">
				<polyline points={cpuSpark} class="sparkline" />
			</svg>
		</div>
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">RAM</p>
			<p class="mt-1 text-sm font-semibold tabular-nums">{percent(ramPercent)}</p>
			<p class="mt-0.5 text-[0.62rem] app-muted">
				{latest?.memoryTotalBytes == null ? 'Unavailable' : bytes(latest.memoryTotalBytes)} total
			</p>
			<svg viewBox="0 0 100 30" class="mt-2 h-8 w-full" aria-hidden="true">
				<polyline points={ramSpark} class="sparkline" />
			</svg>
		</div>
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Disk</p>
			<p class="mt-1 text-sm font-semibold tabular-nums">{percent(diskPercent)}</p>
			<p class="mt-0.5 text-[0.62rem] app-muted">
				{latest?.diskFreeBytes == null ? 'Unavailable' : `${bytes(latest.diskFreeBytes)} free`}
			</p>
			<svg viewBox="0 0 100 30" class="mt-2 h-8 w-full" aria-hidden="true">
				<polyline points={diskSpark} class="sparkline" />
			</svg>
		</div>
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Network</p>
			<p class="mt-1 text-[0.68rem]">
				<span class="app-muted">Network RX</span>
				{rate(latest?.networkRxBytesPerS ?? null)}
			</p>
			<p class="mt-1 text-[0.68rem]">
				<span class="app-muted">Network TX</span>
				{rate(latest?.networkTxBytesPerS ?? null)}
			</p>
			<svg viewBox="0 0 100 30" class="mt-2 h-8 w-full" aria-hidden="true">
				<polyline points={networkSpark} class="sparkline" />
			</svg>
		</div>
	</div>

	<div
		class="app-subtle-surface grid grid-cols-2 gap-3 rounded-xl border p-3 text-[0.7rem] md:grid-cols-4"
	>
		<div>
			<p class="app-muted">Disk read</p>
			<p class="mt-1 font-medium tabular-nums">{rate(latest?.diskReadBytesPerS ?? null)}</p>
		</div>
		<div>
			<p class="app-muted">Disk write</p>
			<p class="mt-1 font-medium tabular-nums">{rate(latest?.diskWriteBytesPerS ?? null)}</p>
		</div>
		<div>
			<p class="app-muted">Disk IOPS</p>
			<p class="mt-1 font-medium tabular-nums">
				{iops(latest?.diskReadOpsPerS ?? null, latest?.diskWriteOpsPerS ?? null)}
			</p>
		</div>
		<div>
			<p class="app-muted">Uptime</p>
			<p class="mt-1 font-medium tabular-nums">{uptime(latest?.uptimeSeconds ?? null)}</p>
		</div>
	</div>

	<div class="app-surface rounded-xl border p-3">
		<div class="flex items-center justify-between gap-2">
			<h4 class="text-xs font-semibold">GPU</h4>
			<span class="text-[0.65rem] app-muted">{latest?.gpuStatus ?? 'unavailable'}</span>
		</div>
		{#if latest?.gpuStatus === 'available' && latest.gpus.length > 0}
			<div class="mt-3 grid gap-2 md:grid-cols-2">
				{#each latest.gpus as gpu (gpu.index)}
					<div class="app-subtle-surface rounded-lg border p-3 text-[0.7rem]">
						<p class="font-medium">GPU {gpu.index} · {gpu.name}</p>
						<div class="mt-2 grid grid-cols-3 gap-2">
							<div>
								<p class="app-muted">Utilization</p>
								<p>{percent(gpu.utilizationPercent)}</p>
							</div>
							<div>
								<p class="app-muted">GPU memory</p>
								<p>{bytes(gpu.memoryUsedBytes)} / {bytes(gpu.memoryTotalBytes)}</p>
							</div>
							<div>
								<p class="app-muted">GPU temperature</p>
								<p>
									{gpu.temperatureC == null ? 'Unavailable' : `${gpu.temperatureC.toFixed(0)} °C`}
								</p>
							</div>
						</div>
					</div>
				{/each}
			</div>
		{:else}
			<p class="mt-2 text-[0.7rem] app-muted">Unavailable</p>
		{/if}
	</div>

	<div class="app-surface rounded-xl border p-3">
		<div class="flex items-center justify-between gap-2">
			<h4 class="text-xs font-semibold">Processes</h4>
			{#if latest?.cptrProcess}<span class="text-[0.65rem] app-muted"
					>CPTR PID {latest.cptrProcess.pid}</span
				>{/if}
		</div>
		{#if latest?.processes?.length}
			<div class="mt-2 space-y-1.5">
				{#each latest.processes.slice(0, 10) as process (process.pid)}
					<div
						class="app-subtle-surface grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 rounded-lg px-2.5 py-2 text-[0.68rem]"
					>
						<span class="truncate" title={process.name}>{process.name}</span>
						<span class="tabular-nums app-muted"
							>CPU {process.cpuPercent == null ? '—' : `${process.cpuPercent.toFixed(1)}%`}</span
						>
						<span class="tabular-nums app-muted"
							>RAM {process.memoryPercent == null
								? '—'
								: `${process.memoryPercent.toFixed(1)}%`}</span
						>
					</div>
				{/each}
			</div>
		{:else}
			<p class="mt-2 text-[0.7rem] app-muted">Unavailable</p>
		{/if}
	</div>
</section>

<style>
	.sparkline {
		fill: none;
		stroke: var(--app-accent);
		stroke-width: 2;
		vector-effect: non-scaling-stroke;
	}
</style>
