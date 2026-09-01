<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		requestOutcomeTotals,
		requestTimeline,
		type McpRequestTimelineBucket,
		type McpTrafficState
	} from '$lib/stores/mcp-traffic';

	type Props = {
		state: McpTrafficState | null;
	};

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
	const maxBucket = $derived(
		Math.max(1, ...buckets.map((bucket) => Math.max(bucket.success, bucket.failed, bucket.total)))
	);
	const successPoints = $derived(
		polylinePoints(
			buckets.map((bucket) => bucket.success),
			maxBucket
		)
	);
	const failurePoints = $derived(
		polylinePoints(
			buckets.map((bucket) => bucket.failed),
			maxBucket
		)
	);
	const successRate = $derived(
		totals.total > 0 ? Math.round((totals.success / totals.total) * 1000) / 10 : 100
	);

	function emptyTimeline(now: number): McpRequestTimelineBucket[] {
		return Array.from({ length: 12 }, (_, index) => ({
			startMs: now - 60_000 + index * 5_000,
			endMs: now - 55_000 + index * 5_000,
			success: 0,
			failed: 0,
			total: 0
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
			<h2 class="text-sm font-semibold">Live request statistics</h2>
			<p class="mt-0.5 text-[0.6875rem] app-muted">Last 60 seconds · 5-second buckets</p>
		</div>
		<div class="flex items-center gap-3 text-[0.65rem] tabular-nums app-muted">
			<span>{recentTotal} recent</span>
			<span>{successRate}% success</span>
		</div>
	</div>

	<div class="grid grid-cols-3 border-b sm:grid-cols-4">
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Successful</p>
			<p class="mt-1 text-lg font-semibold tabular-nums text-emerald-500">{totals.success}</p>
		</div>
		<div class="border-r px-3 py-2.5 sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Failed</p>
			<p class="mt-1 text-lg font-semibold tabular-nums text-red-500">{totals.failed}</p>
		</div>
		<div class="px-3 py-2.5 sm:border-r sm:px-4">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Active</p>
			<p class="mt-1 text-lg font-semibold tabular-nums" style="color: var(--app-accent);">
				{totals.active}
			</p>
		</div>
		<div class="hidden px-4 py-2.5 sm:block">
			<p class="text-[0.62rem] uppercase tracking-wide app-muted">Completed</p>
			<p class="mt-1 text-lg font-semibold tabular-nums">{totals.total}</p>
		</div>
	</div>

	<div class="px-3 py-3 sm:px-4">
		<div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-[0.62rem] app-muted">
			<div class="flex items-center gap-3">
				<span class="inline-flex items-center gap-1.5">
					<span class="h-0.5 w-4 rounded-full bg-emerald-500"></span>Successful
				</span>
				<span class="inline-flex items-center gap-1.5">
					<span class="h-0.5 w-4 rounded-full bg-red-500"></span>Failed
				</span>
			</div>
			<span>{recentSuccess} success · {recentFailed} failed</span>
		</div>

		<div
			class="app-subtle-surface relative h-32 overflow-hidden rounded-xl border px-2 py-2 sm:h-36"
		>
			<div class="pointer-events-none absolute inset-x-2 top-1/4 border-t opacity-40"></div>
			<div class="pointer-events-none absolute inset-x-2 top-1/2 border-t opacity-40"></div>
			<div class="pointer-events-none absolute inset-x-2 top-3/4 border-t opacity-40"></div>
			<svg
				class="relative h-full w-full overflow-visible"
				viewBox="0 0 100 40"
				preserveAspectRatio="none"
				role="img"
				aria-label={`MCP requests in the last 60 seconds: ${recentSuccess} successful and ${recentFailed} failed`}
			>
				<polyline
					points={successPoints}
					fill="none"
					stroke="currentColor"
					class="text-emerald-500"
					stroke-width="1.5"
					vector-effect="non-scaling-stroke"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
				<polyline
					points={failurePoints}
					fill="none"
					stroke="currentColor"
					class="text-red-500"
					stroke-width="1.5"
					vector-effect="non-scaling-stroke"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			</svg>
		</div>
	</div>
</section>
