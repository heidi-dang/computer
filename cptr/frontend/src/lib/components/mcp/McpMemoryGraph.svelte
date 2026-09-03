<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core';
	import { GraphChart } from 'echarts/charts';
	import { AriaComponent, TooltipComponent } from 'echarts/components';
	import { CanvasRenderer } from 'echarts/renderers';
	import type { McpMemoryEdge, McpMemoryNode } from '$lib/apis/mcp';

	use([GraphChart, TooltipComponent, AriaComponent, CanvasRenderer]);

	type Props = {
		nodes: McpMemoryNode[];
		edges: McpMemoryEdge[];
		selectedNodeId?: string | null;
		recentRecallNodeIds?: string[];
		onselect?: (nodeId: string) => void;
	};

	let {
		nodes,
		edges,
		selectedNodeId = null,
		recentRecallNodeIds = [],
		onselect
	}: Props = $props();
	let host: HTMLDivElement;
	let chart: ECharts | null = null;
	let observer: ResizeObserver | null = null;
	let motionQuery: MediaQueryList | null = null;
	let reducedMotion = false;

	const option = $derived.by((): EChartsCoreOption => {
		const recalled = new Set(recentRecallNodeIds);
		const showLabels = nodes.length <= 70;
		return {
			animation: !reducedMotion,
			animationDuration: reducedMotion ? 0 : 520,
			animationDurationUpdate: reducedMotion ? 0 : 360,
			tooltip: {
				trigger: 'item',
				confine: true,
				backgroundColor: 'rgba(9, 14, 24, 0.96)',
				borderColor: 'rgba(148, 163, 184, 0.25)',
				textStyle: { color: '#e5edf7', fontSize: 11 },
				formatter: (params: any) => {
					const data = params?.data;
					if (!data) return '';
					if (params.dataType === 'edge') return data.kind === 'related' ? 'Memory relationship' : 'Scope';
					const node = data.__node as McpMemoryNode | undefined;
					if (!node) return String(data.name ?? 'Memory');
					const scope = node.scope === 'user' ? 'User memory' : node.workspace_name || 'Workspace memory';
					const recalledText = Number(node.recall_count || 0) > 0 ? `<br/>Recalled ${node.recall_count}×` : '';
					return `<strong>${escapeHtml(node.label)}</strong><br/>${escapeHtml(scope)}${recalledText}`;
				}
			},
			series: [
				{
					type: 'graph',
					layout: 'force',
					roam: true,
					draggable: true,
					cursor: 'pointer',
					force: {
						repulsion: nodes.length > 160 ? 210 : 285,
						gravity: 0.06,
						edgeLength: [54, 128],
						friction: 0.58,
						layoutAnimation: !reducedMotion
					},
					label: {
						show: showLabels,
						position: 'right',
						distance: 5,
						fontSize: 9,
						color: 'rgba(226, 232, 240, 0.78)',
						formatter: (params: any) => String(params?.data?.name ?? '').slice(0, 30)
					},
					edgeSymbol: ['none', 'none'],
					lineStyle: {
						color: 'source',
						width: 0.8,
						opacity: 0.24,
						curveness: 0.08
					},
					emphasis: {
						focus: 'adjacency',
						scale: true,
						lineStyle: { width: 1.8, opacity: 0.72 },
						label: { show: true, color: '#f8fafc', fontSize: 10 }
					},
					data: nodes.map((node) => {
						const isScope = node.kind === 'scope';
						const isSelected = node.id === selectedNodeId;
						const isRecalled = recalled.has(node.id);
						const recallWeight = Math.min(12, Math.log2(Number(node.recall_count || 0) + 1) * 3.2);
						const baseSize = isScope ? 38 : 12 + recallWeight;
						const color = isScope
							? node.scope === 'user'
								? '#8b5cf6'
								: '#0ea5e9'
							: isRecalled
								? '#f59e0b'
								: node.scope === 'user'
									? '#a78bfa'
									: '#38bdf8';
						return {
							id: node.id,
							name: node.label,
							value: Number(node.recall_count || 0),
							symbolSize: isSelected ? baseSize + 8 : baseSize,
							draggable: true,
							itemStyle: {
								color,
								opacity: isScope ? 0.96 : 0.82,
								borderColor: isSelected ? '#f8fafc' : isRecalled ? '#fde68a' : 'rgba(255,255,255,0.16)',
								borderWidth: isSelected ? 2.5 : isRecalled ? 1.8 : 0.7,
								shadowBlur: isSelected ? 20 : isRecalled ? 14 : 4,
								shadowColor: isSelected ? '#ffffff55' : `${color}55`
							},
							__node: node
						};
					}),
					links: edges.map((edge) => ({
						id: edge.id,
						source: edge.source,
						target: edge.target,
						kind: edge.kind,
						lineStyle:
							edge.kind === 'related'
								? { width: 1.4, opacity: 0.4, curveness: 0.16, type: 'solid' }
								: { width: 0.7, opacity: 0.18, curveness: 0.03, type: 'dashed' }
					}))
				}
			],
			aria: {
				enabled: true,
				label: {
					description: `Memory graph containing ${nodes.filter((node) => node.kind === 'memory').length} memories and ${edges.length} relationships.`
				}
			}
		};
	});

	function escapeHtml(value: string): string {
		return value.replace(/[&<>'"]/g, (character) => {
			const entities: Record<string, string> = {
				'&': '&amp;',
				'<': '&lt;',
				'>': '&gt;',
				"'": '&#39;',
				'"': '&quot;'
			};
			return entities[character] || character;
		});
	}

	function render() {
		if (!chart) return;
		chart.setOption(option, { notMerge: true, lazyUpdate: true });
	}

	onMount(() => {
		motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
		reducedMotion = motionQuery.matches;
		chart = init(host, undefined, {
			renderer: 'canvas',
			devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2)
		});
		chart.on('click', (params: any) => {
			if (params?.dataType !== 'node') return;
			const nodeId = String(params?.data?.id || '');
			if (nodeId) onselect?.(nodeId);
		});
		observer = new ResizeObserver(() => chart?.resize());
		observer.observe(host);
		render();
	});

	$effect(() => {
		void option;
		render();
	});

	onDestroy(() => {
		observer?.disconnect();
		observer = null;
		chart?.dispose();
		chart = null;
		motionQuery = null;
	});
</script>

<div
	bind:this={host}
	class="memory-graph"
	role="img"
	aria-label={`Interactive CPTR memory graph with ${nodes.length} visible nodes`}
></div>

<style>
	.memory-graph {
		width: 100%;
		height: 100%;
		min-height: 28rem;
		touch-action: none;
		background:
			radial-gradient(circle at 50% 45%, color-mix(in oklab, var(--app-accent) 5%, transparent), transparent 42%),
			radial-gradient(circle at 20% 75%, rgba(139, 92, 246, 0.045), transparent 30%);
	}

	@media (max-width: 1023px) {
		.memory-graph {
			min-height: 24rem;
		}
	}

	@media (max-width: 639px) {
		.memory-graph {
			min-height: 20rem;
		}
	}
</style>
