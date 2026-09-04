<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import type { EChartsCoreOption } from 'echarts/core';
	import {
		currentUsageModel,
		usagePeriodTotals,
		usageTimeline,
		type McpDiagnosticsState,
		type McpUsageTimelineBucket
	} from '$lib/stores/mcp-diagnostics';
	import McpTimeSeriesChart from './McpTimeSeriesChart.svelte';

	type Props = {
		state: McpDiagnosticsState | null;
	};

	const telemetryGroup = 'mcp-live-telemetry';
	const inputColor = '#38bdf8';
	const outputColor = '#a78bfa';
	const costColor = '#22d3ee';
	const axisColor = 'rgba(148, 163, 184, 0.58)';
	const gridColor = 'rgba(148, 163, 184, 0.13)';

	let { state: diagnosticsState }: Props = $props();
	let nowMs = $state(Date.now());
	let clock: ReturnType<typeof setInterval> | null = null;

	const emptyPeriod = {
		requests: 0,
		inputTokensEstimated: 0,
		outputTokensEstimated: 0,
		totalTokensEstimated: 0,
		simulatedCostUsd: 0,
		pricedEvents: 0,
		staleEvents: 0,
		unpricedEvents: 0
	};
	const weekTotals = $derived(
		diagnosticsState ? usagePeriodTotals(diagnosticsState, 'week') : emptyPeriod
	);
	const monthTotals = $derived(
		diagnosticsState ? usagePeriodTotals(diagnosticsState, 'month') : emptyPeriod
	);
	const currentModel = $derived(currentUsageModel(diagnosticsState));
	const buckets = $derived(
		diagnosticsState ? usageTimeline(diagnosticsState, nowMs) : emptyTimeline(nowMs)
	);
	const recentInput = $derived(buckets.reduce((sum, bucket) => sum + bucket.inputTokens, 0));
	const recentOutput = $derived(buckets.reduce((sum, bucket) => sum + bucket.outputTokens, 0));
	const recentCost = $derived(buckets.reduce((sum, bucket) => sum + bucket.simulatedCostUsd, 0));
	const pricedRequestCount = $derived(monthTotals.pricedEvents + monthTotals.staleEvents);
	const avgCost = $derived(
		pricedRequestCount > 0 ? monthTotals.simulatedCostUsd / pricedRequestCount : null
	);
	const tokenChartOption = $derived.by((): EChartsCoreOption => {
		const start = buckets[0]?.startMs ?? nowMs - 60_000;
		const end = buckets.at(-1)?.endMs ?? nowMs;
		return {
			legend: {
				data: ['Input tokens', 'Output tokens'],
				selectedMode: true,
				top: 0,
				left: 'center',
				icon: 'roundRect',
				itemWidth: 14,
				itemHeight: 3,
				textStyle: { color: axisColor, fontSize: 10 }
			},
			grid: { left: 10, right: 12, top: 34, bottom: 26, containLabel: true },
			tooltip: {
				trigger: 'axis',
				confine: true,
				axisPointer: {
					type: 'line',
					snap: true,
					lineStyle: { color: 'rgba(125, 211, 252, 0.62)' }
				},
				backgroundColor: 'rgba(10, 15, 24, 0.94)',
				borderColor: 'rgba(148, 163, 184, 0.28)',
				textStyle: { color: '#e5edf7', fontSize: 11 }
			},
			xAxis: timeAxis(start, end),
			yAxis: valueAxis((value) => compactNumber(value)),
			dataZoom: insideDataZoom(),
			series: [
				lineSeries(
					'Input tokens',
					inputColor,
					buckets.map((bucket) => [bucket.endMs, bucket.inputTokens])
				),
				lineSeries(
					'Output tokens',
					outputColor,
					buckets.map((bucket) => [bucket.endMs, bucket.outputTokens])
				)
			],
			aria: {
				enabled: true,
				label: {
					description: `Estimated MCP-visible tokens in the last 60 seconds: ${recentInput} input and ${recentOutput} output.`
				}
			},
			media: [
				{
					query: { maxWidth: 520 },
					option: {
						grid: { left: 4, right: 8, top: 34, bottom: 22, containLabel: true },
						xAxis: { axisLabel: { fontSize: 9, hideOverlap: true } },
						yAxis: { axisLabel: { show: false }, splitNumber: 2 }
					}
				}
			]
		};
	});
	const costChartOption = $derived.by((): EChartsCoreOption => {
		const start = buckets[0]?.startMs ?? nowMs - 60_000;
		const end = buckets.at(-1)?.endMs ?? nowMs;
		return {
			grid: { left: 10, right: 12, top: 16, bottom: 26, containLabel: true },
			tooltip: {
				trigger: 'axis',
				confine: true,
				axisPointer: { type: 'line', snap: true, lineStyle: { color: 'rgba(34, 211, 238, 0.68)' } },
				backgroundColor: 'rgba(10, 15, 24, 0.94)',
				borderColor: 'rgba(148, 163, 184, 0.28)',
				textStyle: { color: '#e5edf7', fontSize: 11 }
			},
			xAxis: timeAxis(start, end),
			yAxis: valueAxis((value) => formatUsdAxis(value)),
			dataZoom: insideDataZoom(),
			series: [
				lineSeries(
					'Simulated cost',
					costColor,
					buckets.map((bucket) => [bucket.endMs, bucket.simulatedCostUsd]),
					true
				)
			],
			aria: {
				enabled: true,
				label: {
					description: `API-equivalent simulated MCP cost in the last 60 seconds: ${formatUsd(recentCost)}.`
				}
			},
			media: [
				{
					query: { maxWidth: 520 },
					option: {
						grid: { left: 4, right: 8, top: 14, bottom: 22, containLabel: true },
						xAxis: { axisLabel: { fontSize: 9, hideOverlap: true } },
						yAxis: { axisLabel: { show: false }, splitNumber: 2 }
					}
				}
			]
		};
	});

	function emptyTimeline(now: number): McpUsageTimelineBucket[] {
		return Array.from({ length: 12 }, (_, index) => ({
			startMs: now - 60_000 + index * 5_000,
			endMs: now - 55_000 + index * 5_000,
			inputTokens: 0,
			outputTokens: 0,
			totalTokens: 0,
			simulatedCostUsd: 0,
			requests: 0
		}));
	}

	function timeAxis(start: number, end: number) {
		return {
			type: 'time' as const,
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
		};
	}

	function valueAxis(formatter: (value: number) => string) {
		return {
			type: 'value' as const,
			min: 0,
			splitNumber: 3,
			axisLine: { show: false },
			axisTick: { show: false },
			axisLabel: { color: axisColor, fontSize: 10, formatter },
			splitLine: { lineStyle: { color: gridColor, type: 'dashed' as const } }
		};
	}

	function insideDataZoom() {
		return [
			{
				type: 'inside' as const,
				xAxisIndex: 0,
				filterMode: 'none' as const,
				zoomOnMouseWheel: 'shift' as const,
				moveOnMouseWheel: true,
				moveOnMouseMove: true
			}
		];
	}

	function lineSeries(name: string, color: string, data: number[][], cost = false) {
		return {
			name,
			type: 'line' as const,
			data,
			smooth: 0.28,
			showSymbol: false,
			symbol: 'circle',
			symbolSize: 7,
			lineStyle: { color, width: 2.25, shadowBlur: 8, shadowColor: `${color}55` },
			itemStyle: { color },
			areaStyle: {
				color: {
					type: 'linear',
					x: 0,
					y: 0,
					x2: 0,
					y2: 1,
					colorStops: [
						{ offset: 0, color: `${color}32` },
						{ offset: 1, color: `${color}03` }
					]
				}
			},
			tooltip: {
				valueFormatter: (value: unknown) =>
					cost ? formatUsd(Number(value)) : `${formatTokens(Number(value))} tokens`
			},
			emphasis: { focus: 'series' as const }
		};
	}

	function formatTime(value: number): string {
		return new Intl.DateTimeFormat(undefined, { minute: '2-digit', second: '2-digit' }).format(
			new Date(value)
		);
	}

	function compactNumber(value: number): string {
		return new Intl.NumberFormat(undefined, {
			notation: 'compact',
			maximumFractionDigits: 1
		}).format(value);
	}

	function formatUsdAxis(value: number): string {
		if (value === 0) return '$0';
		if (Math.abs(value) < 0.001) return `$${value.toExponential(1)}`;
		if (Math.abs(value) < 1) return `$${value.toFixed(3)}`;
		return `$${value.toFixed(2)}`;
	}

	function formatTokens(value: number): string {
		return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
	}

	function formatUsd(value: number | null): string {
		if (value == null || !Number.isFinite(value)) return 'Unavailable';
		if (value === 0) return '$0';
		if (Math.abs(value) < 0.01) return `$${value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')}`;
		return `$${value.toFixed(value < 1 ? 4 : 2)}`;
	}

	function formatRate(value: string | null): string {
		if (!value) return 'Unavailable';
		const parsed = Number(value);
		return Number.isFinite(parsed) ? `$${parsed.toLocaleString()} / 1M` : 'Unavailable';
	}

	function pricingStatusLabel(): string {
		if (!currentModel || currentModel.pricingStatus === 'model_not_reported')
			return 'Model not reported';
		if (currentModel.pricingStatus === 'unknown_model') return 'Unpriced';
		if (currentModel.pricingStatus === 'stale') return 'Stale pricing';
		return 'Current pricing';
	}

	function pricingStatusClass(): string {
		if (currentModel?.pricingStatus === 'current') return 'text-emerald-500';
		if (currentModel?.pricingStatus === 'stale') return 'text-amber-500';
		return 'app-muted';
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
				<h2 class="text-sm font-semibold">Model usage & simulated cost</h2>
				<span class="app-subtle-surface rounded-full border px-2 py-0.5 text-[0.62rem] app-muted">
					Estimated · MCP-visible tokens
				</span>
			</div>
			<p class="mt-0.5 text-[0.6875rem] app-muted">Last 60 seconds · 5-second buckets</p>
		</div>
		<div class="text-right text-[0.65rem] tabular-nums app-muted">
			<p>{formatTokens(recentInput + recentOutput)} recent tokens</p>
			<p>{formatUsd(recentCost)} recent simulated cost</p>
		</div>
	</div>

	<div class="grid grid-cols-2 border-b sm:grid-cols-3 lg:grid-cols-6">
		<div class="border-b border-r px-3 py-2.5 sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Current model</p>
			<p
				class="mt-1 truncate text-sm font-semibold"
				title={currentModel?.modelReported ?? 'Model not reported'}
			>
				{currentModel?.modelReported ?? 'Model not reported'}
			</p>
			{#if currentModel?.modelSource === 'self_reported'}
				<span
					class="mt-1 inline-flex rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[0.58rem] text-emerald-500"
					>Self-reported</span
				>
			{/if}
		</div>
		<div class="border-b px-3 py-2.5 sm:border-r sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">This week · Weekly tokens</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">
				{formatTokens(weekTotals.totalTokensEstimated)}
			</p>
			<p class="text-[0.58rem] app-muted">
				{formatTokens(weekTotals.inputTokensEstimated)} in · {formatTokens(
					weekTotals.outputTokensEstimated
				)} out · {weekTotals.requests} requests
			</p>
		</div>
		<div class="border-b border-r px-3 py-2.5 sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">
				This week · Simulated cost (USD)
			</p>
			<p class="mt-1 text-lg font-semibold tabular-nums" style="color: var(--app-accent);">
				{formatUsd(weekTotals.simulatedCostUsd)}
			</p>
			<p class="text-[0.58rem] app-muted">database-backed · UTC week</p>
		</div>
		<div class="border-b px-3 py-2.5 sm:border-r sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">This month · Monthly tokens</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">
				{formatTokens(monthTotals.totalTokensEstimated)}
			</p>
			<p class="text-[0.58rem] app-muted">
				{formatTokens(monthTotals.inputTokensEstimated)} in · {formatTokens(
					monthTotals.outputTokensEstimated
				)} out · {monthTotals.requests} requests
			</p>
		</div>
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">
				This month · Simulated cost (USD)
			</p>
			<p class="mt-1 text-lg font-semibold tabular-nums" style="color: var(--app-accent);">
				{formatUsd(monthTotals.simulatedCostUsd)}
			</p>
			<p class="text-[0.58rem] app-muted">database-backed · calendar month</p>
		</div>
		<div class="px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Avg simulated cost/request</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">{formatUsd(avgCost)}</p>
			<p class="mt-0.5 text-[0.58rem] {pricingStatusClass()}">
				Pricing status · {pricingStatusLabel()}
			</p>
		</div>
	</div>

	<div class="grid grid-cols-1 gap-3 p-3 sm:p-4 lg:grid-cols-2">
		<div class="app-subtle-surface min-w-0 rounded-xl border p-3">
			<div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-[0.62rem] app-muted">
				<div class="flex items-center gap-3">
					<span class="inline-flex items-center gap-1.5"
						><span class="h-0.5 w-4 rounded-full bg-sky-500"></span>Input tokens</span
					>
					<span class="inline-flex items-center gap-1.5"
						><span class="h-0.5 w-4 rounded-full bg-violet-500"></span>Output tokens</span
					>
				</div>
				<span>{formatTokens(recentInput)} in · {formatTokens(recentOutput)} out</span>
			</div>
			<div class="overflow-hidden rounded-lg border p-1.5">
				<McpTimeSeriesChart
					option={tokenChartOption}
					group={telemetryGroup}
					height="standard"
					ariaLabel={`Estimated MCP-visible tokens in the last 60 seconds: ${recentInput} input and ${recentOutput} output`}
				/>
			</div>
		</div>

		<div class="app-subtle-surface min-w-0 rounded-xl border p-3">
			<div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-[0.62rem] app-muted">
				<span class="font-medium">API-equivalent simulated USD</span>
				<span>{formatUsd(recentCost)} recent</span>
			</div>
			<div class="overflow-hidden rounded-lg border p-1.5">
				<McpTimeSeriesChart
					option={costChartOption}
					group={telemetryGroup}
					height="standard"
					ariaLabel={`API-equivalent simulated MCP cost in the last 60 seconds: ${formatUsd(recentCost)}`}
				/>
			</div>
		</div>
	</div>

	<details class="app-surface border-t px-3 py-3 sm:px-4">
		<summary class="app-interactive cursor-pointer rounded-lg text-xs font-semibold"
			>Pricing details</summary
		>
		<div class="mt-3 grid grid-cols-2 gap-2 text-[0.68rem] sm:grid-cols-3 lg:grid-cols-6">
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Input rate</p>
				<p class="mt-1 font-medium tabular-nums">
					{formatRate(currentModel?.inputUsdPerMillion ?? null)}
				</p>
			</div>
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Cached input rate</p>
				<p class="mt-1 font-medium tabular-nums">
					{formatRate(currentModel?.cachedInputUsdPerMillion ?? null)}
				</p>
			</div>
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Output rate</p>
				<p class="mt-1 font-medium tabular-nums">
					{formatRate(currentModel?.outputUsdPerMillion ?? null)}
				</p>
			</div>
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Pricing status</p>
				<p class="mt-1 font-medium {pricingStatusClass()}">{pricingStatusLabel()}</p>
			</div>
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Registry</p>
				<p class="mt-1 break-all font-mono text-[0.62rem]">
					{currentModel?.pricingVersion ?? 'Unavailable'}
				</p>
			</div>
			<div class="app-subtle-surface rounded-lg border p-2.5">
				<p class="app-muted">Verified</p>
				<p class="mt-1 font-medium">{currentModel?.pricingVerifiedAt ?? 'Unavailable'}</p>
				{#if currentModel?.pricingValidThrough}
					<p class="mt-0.5 text-[0.58rem] app-muted">
						valid through {currentModel.pricingValidThrough}
					</p>
				{/if}
			</div>
		</div>
		{#if currentModel?.pricingSourceUrl}
			<p class="mt-2 break-words text-[0.65rem] app-muted">
				Source · <a
					class="app-accent hover:underline"
					href={currentModel.pricingSourceUrl}
					target="_blank"
					rel="noreferrer">{currentModel.pricingSourceLabel}</a
				>
			</p>
		{/if}
		<p class="mt-2 text-[0.65rem] leading-5 app-muted">
			Cached input token usage is unavailable to MCP, so cached-token cost is not inferred.
			Long-context multiplier not inferable from MCP-visible tokens.
		</p>
	</details>

	<div class="border-t px-3 py-3 text-[0.65rem] leading-5 app-muted sm:px-4">
		API-equivalent simulation from MCP-visible estimated tokens. <strong class="font-semibold"
			>Not your ChatGPT bill.</strong
		>
		Full prompt context, reasoning, cache usage, and final-answer tokens are not visible to MCP.
	</div>
</section>
