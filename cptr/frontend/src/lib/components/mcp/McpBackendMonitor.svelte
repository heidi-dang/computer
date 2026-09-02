<script lang="ts">
	import type { EChartsCoreOption } from 'echarts/core';
	import type { McpBackendMetricsState } from '$lib/stores/mcp-diagnostics';
	import McpTimeSeriesChart from './McpTimeSeriesChart.svelte';

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
	const cpuSeries = $derived(
		metricSeries(history.map((sample) => [sample.timestampMs, sample.cpuUsagePercent]))
	);
	const ramSeries = $derived(
		metricSeries(
			history.map((sample) => [
				sample.timestampMs,
				sample.memoryTotalBytes && sample.memoryAvailableBytes != null
					? ((sample.memoryTotalBytes - sample.memoryAvailableBytes) / sample.memoryTotalBytes) *
						100
					: null
			])
		)
	);
	const diskSeries = $derived(
		metricSeries(
			history.map((sample) => [
				sample.timestampMs,
				(sample.diskReadBytesPerS ?? 0) + (sample.diskWriteBytesPerS ?? 0)
			])
		)
	);
	const networkSeries = $derived(
		metricSeries(
			history.map((sample) => [
				sample.timestampMs,
				(sample.networkRxBytesPerS ?? 0) + (sample.networkTxBytesPerS ?? 0)
			])
		)
	);
	const cpuStats = $derived(stats(cpuSeries.map((point) => point[1])));
	const ramStats = $derived(stats(ramSeries.map((point) => point[1])));
	const diskStats = $derived(stats(diskSeries.map((point) => point[1])));
	const networkStats = $derived(stats(networkSeries.map((point) => point[1])));
	const cpuChart = $derived(metricChartOption('CPU utilization', cpuSeries, '#38bdf8', true));
	const ramChart = $derived(metricChartOption('RAM utilization', ramSeries, '#a78bfa', true));
	const diskChart = $derived(metricChartOption('Disk throughput', diskSeries, '#34d399', false));
	const networkChart = $derived(
		metricChartOption('Network throughput', networkSeries, '#f59e0b', false)
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

	type MetricPoint = [number, number];

	function metricSeries(points: Array<[number, number | null]>): MetricPoint[] {
		return points
			.filter((point): point is MetricPoint => point[1] != null && Number.isFinite(point[1]))
			.map(([timestamp, value]) => [timestamp, Math.max(0, value)]);
	}

	function stats(values: number[]) {
		if (values.length === 0) return { minimum: null, average: null, maximum: null };
		return {
			minimum: Math.min(...values),
			average: values.reduce((sum, value) => sum + value, 0) / values.length,
			maximum: Math.max(...values)
		};
	}

	function metricChartOption(
		name: string,
		data: MetricPoint[],
		color: string,
		isPercent: boolean
	): EChartsCoreOption {
		return {
			grid: { left: 4, right: 6, top: 10, bottom: 10, containLabel: false },
			tooltip: {
				trigger: 'axis',
				confine: true,
				axisPointer: { type: 'line', lineStyle: { color: 'rgba(148, 163, 184, 0.5)' } },
				backgroundColor: 'rgba(10, 15, 24, 0.94)',
				borderColor: 'rgba(148, 163, 184, 0.28)',
				textStyle: { color: '#e5edf7', fontSize: 10 }
			},
			xAxis: {
				type: 'time',
				boundaryGap: false,
				axisLine: { show: false },
				axisTick: { show: false },
				axisLabel: { show: false },
				splitLine: { show: false }
			},
			yAxis: {
				type: 'value',
				min: 0,
				max: isPercent ? 100 : undefined,
				axisLine: { show: false },
				axisTick: { show: false },
				axisLabel: { show: false },
				splitLine: { show: false }
			},
			series: [
				{
					name,
					type: 'line',
					data,
					smooth: 0.3,
					showSymbol: false,
					lineStyle: { color, width: 2, shadowBlur: 7, shadowColor: `${color}45` },
					itemStyle: { color },
					areaStyle: {
						color: {
							type: 'linear',
							x: 0,
							y: 0,
							x2: 0,
							y2: 1,
							colorStops: [
								{ offset: 0, color: `${color}35` },
								{ offset: 1, color: `${color}02` }
							]
						}
					},
					tooltip: {
						valueFormatter: (value: unknown) =>
							isPercent ? `${Number(value).toFixed(1)}%` : rate(Number(value))
					},
					emphasis: { focus: 'series' }
				}
			],
			aria: {
				enabled: true,
				label: { description: `${name} over recent CPTR backend telemetry samples.` }
			}
		};
	}

	function statValue(value: number | null, isPercent: boolean): string {
		if (value == null) return '—';
		return isPercent ? `${value.toFixed(1)}%` : rate(value);
	}
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
			<div class="mt-2 overflow-hidden rounded-lg border border-transparent">
				<McpTimeSeriesChart option={cpuChart} height="compact" ariaLabel="Recent CPU utilization" />
			</div>
			<div class="mt-2 grid grid-cols-3 gap-1 text-[0.54rem] app-muted">
				<span>Minimum <b class="font-medium">{statValue(cpuStats.minimum, true)}</b></span>
				<span>Average <b class="font-medium">{statValue(cpuStats.average, true)}</b></span>
				<span>Maximum <b class="font-medium">{statValue(cpuStats.maximum, true)}</b></span>
			</div>
		</div>
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">RAM</p>
			<p class="mt-1 text-sm font-semibold tabular-nums">{percent(ramPercent)}</p>
			<p class="mt-0.5 text-[0.62rem] app-muted">
				{latest?.memoryTotalBytes == null ? 'Unavailable' : bytes(latest.memoryTotalBytes)} total
			</p>
			<div class="mt-2 overflow-hidden rounded-lg border border-transparent">
				<McpTimeSeriesChart option={ramChart} height="compact" ariaLabel="Recent RAM utilization" />
			</div>
			<div class="mt-2 grid grid-cols-3 gap-1 text-[0.54rem] app-muted">
				<span>Minimum <b class="font-medium">{statValue(ramStats.minimum, true)}</b></span>
				<span>Average <b class="font-medium">{statValue(ramStats.average, true)}</b></span>
				<span>Maximum <b class="font-medium">{statValue(ramStats.maximum, true)}</b></span>
			</div>
		</div>
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Disk</p>
			<p class="mt-1 text-sm font-semibold tabular-nums">{percent(diskPercent)}</p>
			<p class="mt-0.5 text-[0.62rem] app-muted">
				{latest?.diskFreeBytes == null ? 'Unavailable' : `${bytes(latest.diskFreeBytes)} free`}
			</p>
			<div class="mt-2 overflow-hidden rounded-lg border border-transparent">
				<McpTimeSeriesChart
					option={diskChart}
					height="compact"
					ariaLabel="Recent disk throughput"
				/>
			</div>
			<div class="mt-2 grid grid-cols-3 gap-1 text-[0.54rem] app-muted">
				<span>Minimum <b class="font-medium">{statValue(diskStats.minimum, false)}</b></span>
				<span>Average <b class="font-medium">{statValue(diskStats.average, false)}</b></span>
				<span>Maximum <b class="font-medium">{statValue(diskStats.maximum, false)}</b></span>
			</div>
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
			<div class="mt-2 overflow-hidden rounded-lg border border-transparent">
				<McpTimeSeriesChart
					option={networkChart}
					height="compact"
					ariaLabel="Recent network throughput"
				/>
			</div>
			<div class="mt-2 grid grid-cols-3 gap-1 text-[0.54rem] app-muted">
				<span>Minimum <b class="font-medium">{statValue(networkStats.minimum, false)}</b></span>
				<span>Average <b class="font-medium">{statValue(networkStats.average, false)}</b></span>
				<span>Maximum <b class="font-medium">{statValue(networkStats.maximum, false)}</b></span>
			</div>
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
