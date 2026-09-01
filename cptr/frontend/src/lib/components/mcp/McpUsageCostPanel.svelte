<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		currentUsageModel,
		usageTimeline,
		usageTotals,
		type McpDiagnosticsState,
		type McpUsageTimelineBucket
	} from '$lib/stores/mcp-diagnostics';

	type Props = {
		state: McpDiagnosticsState | null;
	};

	let { state: diagnosticsState }: Props = $props();
	let nowMs = $state(Date.now());
	let clock: ReturnType<typeof setInterval> | null = null;

	const totals = $derived(
		diagnosticsState
			? usageTotals(diagnosticsState)
			: {
					inputTokensEstimated: 0,
					outputTokensEstimated: 0,
					totalTokensEstimated: 0,
					simulatedCostUsd: 0,
					pricedEvents: 0,
					staleEvents: 0,
					unpricedEvents: 0
				}
	);
	const currentModel = $derived(currentUsageModel(diagnosticsState));
	const buckets = $derived(
		diagnosticsState ? usageTimeline(diagnosticsState, nowMs) : emptyTimeline(nowMs)
	);
	const recentInput = $derived(buckets.reduce((sum, bucket) => sum + bucket.inputTokens, 0));
	const recentOutput = $derived(buckets.reduce((sum, bucket) => sum + bucket.outputTokens, 0));
	const recentCost = $derived(buckets.reduce((sum, bucket) => sum + bucket.simulatedCostUsd, 0));
	const pricedRequestCount = $derived(totals.pricedEvents + totals.staleEvents);
	const avgCost = $derived(
		pricedRequestCount > 0 ? totals.simulatedCostUsd / pricedRequestCount : null
	);
	const tokenMax = $derived(
		Math.max(1, ...buckets.map((bucket) => Math.max(bucket.inputTokens, bucket.outputTokens)))
	);
	const costMax = $derived(Math.max(0.000001, ...buckets.map((bucket) => bucket.simulatedCostUsd)));
	const inputPoints = $derived(
		polylinePoints(
			buckets.map((bucket) => bucket.inputTokens),
			tokenMax
		)
	);
	const outputPoints = $derived(
		polylinePoints(
			buckets.map((bucket) => bucket.outputTokens),
			tokenMax
		)
	);
	const costPoints = $derived(
		polylinePoints(
			buckets.map((bucket) => bucket.simulatedCostUsd),
			costMax
		)
	);

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

	function polylinePoints(values: number[], maximum: number): string {
		if (values.length === 0) return '';
		const width = 100;
		const height = 40;
		return values
			.map((value, index) => {
				const x = values.length === 1 ? width : (index / (values.length - 1)) * width;
				const y = height - (Math.max(0, value) / maximum) * (height - 4) - 2;
				return `${x.toFixed(2)},${y.toFixed(2)}`;
			})
			.join(' ');
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
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Estimated input</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">
				{formatTokens(totals.inputTokensEstimated)}
			</p>
			<p class="text-[0.58rem] app-muted">tool results → model · cumulative</p>
		</div>
		<div class="border-b border-r px-3 py-2.5 sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Estimated output</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">
				{formatTokens(totals.outputTokensEstimated)}
			</p>
			<p class="text-[0.58rem] app-muted">model → tool calls · cumulative</p>
		</div>
		<div class="border-b px-3 py-2.5 sm:border-r sm:px-4 lg:border-b-0">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Estimated total</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">
				{formatTokens(totals.totalTokensEstimated)}
			</p>
			<p class="text-[0.58rem] app-muted">since backend start</p>
		</div>
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Simulated cost (USD)</p>
			<p class="mt-1 text-lg font-semibold tabular-nums" style="color: var(--app-accent);">
				{formatUsd(totals.simulatedCostUsd)}
			</p>
			<p class="text-[0.58rem] app-muted">since backend start</p>
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
			<div class="relative h-32 overflow-hidden rounded-lg border px-2 py-2">
				<div class="pointer-events-none absolute inset-x-2 top-1/4 border-t opacity-40"></div>
				<div class="pointer-events-none absolute inset-x-2 top-1/2 border-t opacity-40"></div>
				<div class="pointer-events-none absolute inset-x-2 top-3/4 border-t opacity-40"></div>
				<svg
					class="relative h-full w-full overflow-visible"
					viewBox="0 0 100 40"
					preserveAspectRatio="none"
					role="img"
					aria-label={`Estimated MCP-visible tokens in the last 60 seconds: ${recentInput} input and ${recentOutput} output`}
				>
					<polyline
						points={inputPoints}
						fill="none"
						stroke="currentColor"
						class="text-sky-500"
						stroke-width="1.5"
						vector-effect="non-scaling-stroke"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
					<polyline
						points={outputPoints}
						fill="none"
						stroke="currentColor"
						class="text-violet-500"
						stroke-width="1.5"
						vector-effect="non-scaling-stroke"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
			</div>
		</div>

		<div class="app-subtle-surface min-w-0 rounded-xl border p-3">
			<div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-[0.62rem] app-muted">
				<span class="font-medium">API-equivalent simulated USD</span>
				<span>{formatUsd(recentCost)} recent</span>
			</div>
			<div class="relative h-32 overflow-hidden rounded-lg border px-2 py-2">
				<div class="pointer-events-none absolute inset-x-2 top-1/4 border-t opacity-40"></div>
				<div class="pointer-events-none absolute inset-x-2 top-1/2 border-t opacity-40"></div>
				<div class="pointer-events-none absolute inset-x-2 top-3/4 border-t opacity-40"></div>
				<svg
					class="relative h-full w-full overflow-visible"
					viewBox="0 0 100 40"
					preserveAspectRatio="none"
					role="img"
					aria-label={`API-equivalent simulated MCP cost in the last 60 seconds: ${formatUsd(recentCost)}`}
				>
					<polyline
						points={costPoints}
						fill="none"
						stroke="currentColor"
						class="app-accent"
						stroke-width="1.5"
						vector-effect="non-scaling-stroke"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
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
