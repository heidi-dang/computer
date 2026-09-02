<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import type { EChartsCoreOption } from 'echarts/core';
	import {
		requestOutcomeTotals,
		requestTimeline,
		type McpRequestTimelineBucket,
		type McpTrafficState
	} from '$lib/stores/mcp-traffic';
	import McpTimeSeriesChart from './McpTimeSeriesChart.svelte';

	type Props = {
		state: McpTrafficState | null;
	};

	const telemetryGroup = 'mcp-live-telemetry';
	const successColor = '#34d399';
	const failureColor = '#fb7185';
	const axisColor = 'rgba(148, 163, 184, 0.58)';
	const gridColor = 'rgba(148, 163, 184, 0.13)';

	let { state: trafficState }: Props = $props();
	let nowMs = $state(Date.now());
	let clock: ReturnType<typeof setInterval> | null = null;

	const totals = $derived(
		trafficState
			? requestOutcomeTotals(trafficState)
			: {
					total: 0,
					success: 0,
					failed: 0,
					active: 0
				}
	);
	const buckets = $derived(
		trafficState ? requestTimeline(trafficState, nowMs) : emptyTimeline(nowMs)
	);
	const recentTotal = $derived(buckets.reduce((sum, bucket) => sum + bucket.total, 0));
	const recentSuccess = $derived(buckets.reduce((sum, bucket) => sum + bucket.success, 0));
	const recentFailed = $derived(buckets.reduce((sum, bucket) => sum + bucket.failed, 0));
	const successRate = $derived(
		totals.total > 0 ? Math.round((totals.success / totals.total) * 1000) / 10 : 100
	);
	const chartOption = $derived.by((): EChartsCoreOption => {
		const start = buckets[0]?.startMs ?? nowMs - 60_000;
		const end = buckets.at(-1)?.endMs ?? nowMs;
		return {
			animationThreshold: 200,
			grid: { left: 10, right: 14, top: 18, bottom: 28, containLabel: true },
			tooltip: {
				trigger: 'axis',
				confine: true,
				axisPointer: {
					type: 'line',
					snap: true,
					lineStyle: { color: 'rgba(125, 211, 252, 0.62)', width: 1 },
					label: { show: false }
				},
				backgroundColor: 'rgba(10, 15, 24, 0.94)',
				borderColor: 'rgba(148, 163, 184, 0.28)',
				borderWidth: 1,
				textStyle: { color: '#e5edf7', fontSize: 11 }
			},
			xAxis: {
				type: 'time',
				min: start,
				max: end,
				boundaryGap: false,
				axisLine: { lineStyle: { color: gridColor } },
				axisTick: { show: false },
				axisLabel: {
					color: axisColor,
					fontSize: 10,
					hideOverlap: true,
					formatter: (value: number) => formatTime(value)
				},
				splitLine: { show: false }
			},
			yAxis: {
				type: 'value',
				min: 0,
				minInterval: 1,
				splitNumber: 3,
				axisLine: { show: false },
				axisTick: { show: false },
				axisLabel: { color: axisColor, fontSize: 10 },
				splitLine: { lineStyle: { color: gridColor, type: 'dashed' } }
			},
			dataZoom: [
				{
					type: 'inside',
					xAxisIndex: 0,
					filterMode: 'none',
					zoomOnMouseWheel: 'shift',
					moveOnMouseWheel: true,
					moveOnMouseMove: true
				}
			],
			series: [
				{
					name: 'Successful',
					type: 'line',
					data: buckets.map((bucket) => [bucket.endMs, bucket.success]),
					smooth: 0.28,
					showSymbol: false,
					symbol: 'circle',
					symbolSize: 7,
					lineStyle: { color: successColor, width: 2.4, shadowBlur: 8, shadowColor: '#34d39955' },
					itemStyle: { color: successColor },
					areaStyle: {
						color: {
							type: 'linear',
							x: 0,
							y: 0,
							x2: 0,
							y2: 1,
							colorStops: [
								{ offset: 0, color: '#34d39938' },
								{ offset: 1, color: '#34d39903' }
							]
						}
					},
					emphasis: { focus: 'series' }
				},
				{
					name: 'Failed',
					type: 'line',
					data: buckets.map((bucket) => [bucket.endMs, bucket.failed]),
					smooth: 0.2,
					showSymbol: false,
					symbol: 'diamond',
					symbolSize: 8,
					lineStyle: { color: failureColor, width: 2.2, shadowBlur: 9, shadowColor: '#fb718555' },
					itemStyle: { color: failureColor },
					areaStyle: {
						color: {
							type: 'linear',
							x: 0,
							y: 0,
							x2: 0,
							y2: 1,
							colorStops: [
								{ offset: 0, color: '#fb718532' },
								{ offset: 1, color: '#fb718502' }
							]
						}
					},
					emphasis: { focus: 'series' }
				}
			],
			aria: {
				enabled: true,
				label: {
					description: `MCP requests in the last 60 seconds: ${recentSuccess} successful and ${recentFailed} failed.`
				}
			},
			media: [
				{
					query: { maxWidth: 520 },
					option: {
						grid: { left: 4, right: 8, top: 14, bottom: 24, containLabel: true },
						xAxis: { axisLabel: { fontSize: 9, hideOverlap: true } },
						yAxis: { axisLabel: { show: false }, splitNumber: 2 }
					}
				}
			]
		};
	});

	function emptyTimeline(now: number): McpRequestTimelineBucket[] {
		return Array.from({ length: 12 }, (_, index) => ({
			startMs: now - 60_000 + index * 5_000,
			endMs: now - 55_000 + index * 5_000,
			success: 0,
			failed: 0,
			total: 0
		}));
	}

	function formatTime(value: number): string {
		return new Intl.DateTimeFormat(undefined, {
			minute: '2-digit',
			second: '2-digit'
		}).format(new Date(value));
	}

	onMount(() => {
		clock = setInterval(() => {
			nowMs = Date.now();
		}, 1000);
	});

	onDestroy(() => {
		if (clock) clearInterval(clock);
		clock = null;
	});
</script>

<section class="app-raised-surface overflow-hidden rounded-2xl border shadow-sm">
	<div
		class="app-surface flex flex-wrap items-start justify-between gap-3 border-b px-3 py-2.5 sm:px-4 sm:py-3"
	>
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h2 class="text-sm font-semibold">Live request statistics</h2>
				<span
					class="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[0.6rem] font-medium text-emerald-400"
				>
					<span class="size-1.5 animate-pulse rounded-full bg-emerald-400"></span>Live
				</span>
			</div>
			<p class="mt-0.5 text-[0.6875rem] app-muted">
				Last 60 seconds · 5-second buckets · Shift + wheel to inspect
			</p>
		</div>
		<div class="text-right text-[0.65rem] tabular-nums app-muted">
			<p>{recentTotal} recent requests</p>
			<p>{successRate}% cumulative success</p>
		</div>
	</div>

	<div class="grid grid-cols-3 border-b sm:grid-cols-4">
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Successful</p>
			<p class="mt-1 text-lg font-semibold tabular-nums text-emerald-500">{totals.success}</p>
			<p class="mt-0.5 text-[0.58rem] app-muted">{recentSuccess} in window</p>
		</div>
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Failed</p>
			<p class="mt-1 text-lg font-semibold tabular-nums text-rose-400">{totals.failed}</p>
			<p class="mt-0.5 text-[0.58rem] app-muted">{recentFailed} in window</p>
		</div>
		<div class="px-3 py-2.5 sm:border-r sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Active</p>
			<p class="mt-1 text-lg font-semibold tabular-nums" style="color: var(--app-accent);">
				{totals.active}
			</p>
			<p class="mt-0.5 text-[0.58rem] app-muted">in flight now</p>
		</div>
		<div class="hidden px-4 py-2.5 sm:block">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Completed</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">{totals.total}</p>
			<p class="mt-0.5 text-[0.58rem] app-muted">since backend start</p>
		</div>
	</div>

	<div class="p-3 sm:p-4">
		<div class="app-subtle-surface overflow-hidden rounded-xl border p-2 sm:p-3">
			<div
				class="mb-1 flex flex-wrap items-center justify-between gap-2 px-1 text-[0.62rem] app-muted"
			>
				<div class="flex items-center gap-3">
					<span class="inline-flex items-center gap-1.5"
						><span class="h-0.5 w-4 rounded-full bg-emerald-400"></span>Successful</span
					>
					<span class="inline-flex items-center gap-1.5"
						><span class="h-0.5 w-4 rounded-full bg-rose-400"></span>Failed</span
					>
				</div>
				<span>Hover or touch for exact bucket values</span>
			</div>
			<McpTimeSeriesChart
				option={chartOption}
				group={telemetryGroup}
				height="tall"
				ariaLabel={`MCP requests in the last 60 seconds: ${recentSuccess} successful and ${recentFailed} failed`}
			/>
		</div>
	</div>
</section>
