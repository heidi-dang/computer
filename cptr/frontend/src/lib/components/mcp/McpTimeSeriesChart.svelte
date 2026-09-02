<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { connect, init, use, type ECharts, type EChartsCoreOption } from 'echarts/core';
	import { BarChart, LineChart } from 'echarts/charts';
	import {
		AriaComponent,
		AxisPointerComponent,
		DataZoomComponent,
		GridComponent,
		LegendComponent,
		TooltipComponent
	} from 'echarts/components';
	import { CanvasRenderer } from 'echarts/renderers';

	use([
		LineChart,
		BarChart,
		GridComponent,
		TooltipComponent,
		DataZoomComponent,
		LegendComponent,
		AxisPointerComponent,
		AriaComponent,
		CanvasRenderer
	]);

	type ChartHeight = 'compact' | 'standard' | 'tall';
	type Props = {
		option: EChartsCoreOption;
		ariaLabel: string;
		height?: ChartHeight;
		group?: string | null;
	};

	let { option, ariaLabel, height = 'standard', group = null }: Props = $props();
	let host: HTMLDivElement;
	let chart: ECharts | null = null;
	let observer: ResizeObserver | null = null;
	let motionQuery: MediaQueryList | null = null;
	let reducedMotion = false;

	function render(next: EChartsCoreOption) {
		if (!chart) return;
		chart.setOption(
			{
				...next,
				animation: !reducedMotion,
				animationDuration: reducedMotion ? 0 : 360,
				animationDurationUpdate: reducedMotion ? 0 : 240,
				animationEasingUpdate: 'cubicOut'
			},
			{ notMerge: false, lazyUpdate: true }
		);
	}

	function handleMotionChange(event: MediaQueryListEvent) {
		reducedMotion = event.matches;
		render(option);
	}

	onMount(() => {
		motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
		reducedMotion = motionQuery.matches;
		motionQuery.addEventListener('change', handleMotionChange);

		chart = init(host, undefined, {
			renderer: 'canvas',
			devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2)
		});
		if (group) {
			chart.group = group;
			connect(group);
		}

		observer = new ResizeObserver(() => chart?.resize());
		observer.observe(host);
		render(option);
	});

	$effect(() => {
		const next = option;
		render(next);
	});

	onDestroy(() => {
		observer?.disconnect();
		observer = null;
		motionQuery?.removeEventListener('change', handleMotionChange);
		motionQuery = null;
		chart?.dispose();
		chart = null;
	});
</script>

<div bind:this={host} class="mcp-chart mcp-chart--{height}" role="img" aria-label={ariaLabel}></div>

<style>
	.mcp-chart {
		width: 100%;
		min-width: 0;
		touch-action: pan-y;
	}

	.mcp-chart--compact {
		height: 7.5rem;
	}

	.mcp-chart--standard {
		height: 12rem;
	}

	.mcp-chart--tall {
		height: 15rem;
	}

	@media (max-width: 640px) {
		.mcp-chart--compact {
			height: 6.75rem;
		}

		.mcp-chart--standard {
			height: 10.5rem;
		}

		.mcp-chart--tall {
			height: 12rem;
		}
	}
</style>
