<script lang="ts">
	import type { McpTopologyNode } from '$lib/stores/mcp-traffic';
	import type { McpLatencySummaryState } from '$lib/stores/mcp-diagnostics';
	import { displayTopologyLabel, type McpTopologySelection } from '$lib/stores/mcp-topology';

	type LatencyMap = Partial<
		Record<
			'client-mcp-connector' | 'mcp-connector-cptr-mcp' | 'cptr-mcp-cptr-backend',
			McpLatencySummaryState
		>
	>;

	type Props = {
		nodes: McpTopologyNode[];
		selection?: McpTopologySelection;
		aliases?: Record<string, string>;
		latency?: LatencyMap;
		pulseClientIds?: Set<string>;
		errorClientIds?: Set<string>;
		onselect?: (selection: NonNullable<McpTopologySelection>) => void;
	};

	let {
		nodes,
		selection = null,
		aliases = {},
		latency = {},
		pulseClientIds = new Set<string>(),
		errorClientIds = new Set<string>(),
		onselect
	}: Props = $props();

	const width = 1000;
	const height = 620;
	const centerX = width / 2;
	const centerY = height / 2;
	const connectorY = centerY - 120;
	const backendY = centerY + 120;

	const anyActive = $derived(nodes.some((node) => node.active || pulseClientIds.has(node.id)));
	const anyError = $derived(nodes.some((node) => errorClientIds.has(node.id)));

	function x(node: McpTopologyNode): number {
		return 90 + node.x * (width - 180);
	}

	function y(node: McpTopologyNode): number {
		return 32 + node.y * 92;
	}

	function clientPath(node: McpTopologyNode): string {
		const nodeX = x(node);
		const nodeY = y(node);
		const controlY = Math.max(nodeY + 34, connectorY - 62);
		return `M ${nodeX} ${nodeY + 42} C ${nodeX} ${controlY}, ${centerX} ${controlY}, ${centerX} ${connectorY - 44}`;
	}

	function choose(next: NonNullable<McpTopologySelection>) {
		onselect?.(next);
	}

	function handleKey(event: KeyboardEvent, next: NonNullable<McpTopologySelection>) {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			choose(next);
		}
	}

	function selected(kind: 'client' | 'node' | 'edge', id: string): boolean {
		return selection?.kind === kind && selection.id === id;
	}

	function metricName(edgeId: keyof LatencyMap): string {
		if (edgeId === 'client-mcp-connector') return 'Observed request time';
		if (edgeId === 'mcp-connector-cptr-mcp') return 'Adapter handoff';
		return 'Backend API RTT';
	}

	function edgeValue(edgeId: keyof LatencyMap): string {
		const sample = latency[edgeId];
		return sample ? `${sample.latestMs} ms` : '—';
	}

	function clientIdentityMeta(node: McpTopologyNode): string {
		const identity = [node.model, node.workspaceName].filter(Boolean).join(' · ');
		if (identity) return identity;
		if (node.activeRequests > 0) return `${node.activeRequests} active`;
		return node.connected ? 'connected' : 'recent';
	}

	function clientAriaLabel(node: McpTopologyNode): string {
		const parts = [
			nodeLabel(node.id, node.label),
			node.model,
			node.workspaceName,
			node.connected ? 'connected' : 'idle',
			`${node.activeRequests} active requests`
		].filter(Boolean);
		return parts.join(', ');
	}

	function latencyTone(edgeId: keyof LatencyMap): string {
		const health = latency[edgeId]?.health;
		if (health === 'error') return 'edge-health edge-health--error';
		if (health === 'degraded') return 'edge-health edge-health--degraded';
		if (health === 'healthy') return 'edge-health edge-health--healthy';
		return 'edge-health edge-health--unknown';
	}

	function nodeLabel(id: string, canonical: string): string {
		return displayTopologyLabel(id, canonical, aliases);
	}
</script>

<div
	class="topology-frame app-raised-surface relative min-h-[22rem] overflow-hidden rounded-2xl border shadow-sm sm:min-h-[28rem]"
>
	{#if nodes.length === 0}
		<div
			class="pointer-events-none absolute left-3 top-3 z-10 rounded-xl border app-subtle-surface px-3 py-2 text-[0.68rem] app-muted sm:left-4 sm:top-4"
		>
			Waiting for an MCP client connection
		</div>
	{/if}

	<svg
		viewBox={`0 0 ${width} ${height}`}
		class="h-full min-h-[22rem] w-full sm:min-h-[28rem]"
		role="img"
		aria-label="Live MCP client traffic topology"
	>
		<defs>
			<radialGradient id="cptr-center-glow">
				<stop offset="0%" stop-color="currentColor" stop-opacity="0.22" />
				<stop offset="100%" stop-color="currentColor" stop-opacity="0" />
			</radialGradient>
			<linearGradient id="edge-flow-gradient" x1="0%" y1="0%" x2="100%" y2="100%">
				<stop offset="0%" stop-color="#38bdf8" />
				<stop offset="52%" stop-color="#22d3ee" />
				<stop offset="100%" stop-color="#a78bfa" />
			</linearGradient>
			<filter id="edge-glow" x="-40%" y="-40%" width="180%" height="180%">
				<feGaussianBlur stdDeviation="3.5" result="edgeBlur" />
				<feMerge><feMergeNode in="edgeBlur" /><feMergeNode in="SourceGraphic" /></feMerge>
			</filter>
			<filter id="soft-glow" x="-60%" y="-60%" width="220%" height="220%">
				<feGaussianBlur stdDeviation="9" result="blur" />
				<feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
			</filter>
		</defs>

		<!-- The client-facing metric is adapter-observed request time, never fabricated internet RTT. -->
		<g
			role="button"
			tabindex="0"
			class:topology-selected={selected('edge', 'client-mcp-connector')}
			aria-label={`${metricName('client-mcp-connector')}: ${edgeValue('client-mcp-connector')}`}
			onclick={() => choose({ kind: 'edge', id: 'client-mcp-connector' })}
			onkeydown={(event) => handleKey(event, { kind: 'edge', id: 'client-mcp-connector' })}
		>
			{#each nodes as node (node.id)}
				<path d={clientPath(node)} class="edge-hit" />
				<path d={clientPath(node)} class="edge-route-underlay" />
				<path
					d={clientPath(node)}
					class:edge-active={node.active || pulseClientIds.has(node.id)}
					class:edge-flow={node.active || pulseClientIds.has(node.id)}
					class:edge-error={errorClientIds.has(node.id)}
					class="topology-edge"
				/>
			{/each}
			<g class="edge-badge" transform={`translate(${centerX + 92} ${connectorY - 50})`}>
				<rect x="-70" y="-13" width="140" height="26" rx="13" />
				<circle cx="-58" cy="0" r="3.5" class={latencyTone('client-mcp-connector')} />
				<text text-anchor="middle" y="3"
					>Observed request time · {edgeValue('client-mcp-connector')}</text
				>
			</g>
		</g>

		<g
			role="button"
			tabindex="0"
			class:topology-selected={selected('edge', 'mcp-connector-cptr-mcp')}
			aria-label={`${metricName('mcp-connector-cptr-mcp')}: ${edgeValue('mcp-connector-cptr-mcp')}`}
			onclick={() => choose({ kind: 'edge', id: 'mcp-connector-cptr-mcp' })}
			onkeydown={(event) => handleKey(event, { kind: 'edge', id: 'mcp-connector-cptr-mcp' })}
		>
			<line x1={centerX} y1={connectorY + 44} x2={centerX} y2={centerY - 47} class="edge-hit" />
			<line
				x1={centerX}
				y1={connectorY + 44}
				x2={centerX}
				y2={centerY - 47}
				class="edge-route-underlay"
			/>
			<line
				x1={centerX}
				y1={connectorY + 44}
				x2={centerX}
				y2={centerY - 47}
				class:edge-active={anyActive}
				class:edge-flow={anyActive}
				class:edge-error={anyError}
				class="topology-edge infrastructure-edge"
			/>
			<g class="edge-badge" transform={`translate(${centerX + 82} ${centerY - 60})`}>
				<rect x="-60" y="-13" width="120" height="26" rx="13" />
				<circle cx="-48" cy="0" r="3.5" class={latencyTone('mcp-connector-cptr-mcp')} />
				<text text-anchor="middle" y="3"
					>Adapter handoff · {edgeValue('mcp-connector-cptr-mcp')}</text
				>
			</g>
		</g>

		<g
			role="button"
			tabindex="0"
			class:topology-selected={selected('edge', 'cptr-mcp-cptr-backend')}
			aria-label={`${metricName('cptr-mcp-cptr-backend')}: ${edgeValue('cptr-mcp-cptr-backend')}`}
			onclick={() => choose({ kind: 'edge', id: 'cptr-mcp-cptr-backend' })}
			onkeydown={(event) => handleKey(event, { kind: 'edge', id: 'cptr-mcp-cptr-backend' })}
		>
			<line x1={centerX} y1={centerY + 47} x2={centerX} y2={backendY - 40} class="edge-hit" />
			<line
				x1={centerX}
				y1={centerY + 47}
				x2={centerX}
				y2={backendY - 40}
				class="edge-route-underlay"
			/>
			<line
				x1={centerX}
				y1={centerY + 47}
				x2={centerX}
				y2={backendY - 40}
				class:edge-active={anyActive}
				class:edge-flow={anyActive}
				class:edge-error={anyError}
				class="topology-edge infrastructure-edge"
			/>
			<g class="edge-badge" transform={`translate(${centerX + 82} ${centerY + 60})`}>
				<rect x="-62" y="-13" width="124" height="26" rx="13" />
				<circle cx="-50" cy="0" r="3.5" class={latencyTone('cptr-mcp-cptr-backend')} />
				<text text-anchor="middle" y="3"
					>Backend API RTT · {edgeValue('cptr-mcp-cptr-backend')}</text
				>
			</g>
		</g>

		<g
			class="infrastructure-node connector-node"
			class:node-selected={selected('node', 'mcp-connector')}
			role="button"
			tabindex="0"
			aria-label={`${nodeLabel('mcp-connector', 'MCP Connector')} transport node`}
			onclick={() => choose({ kind: 'node', id: 'mcp-connector' })}
			onkeydown={(event) => handleKey(event, { kind: 'node', id: 'mcp-connector' })}
		>
			<circle cx={centerX} cy={connectorY} r="57" class="node-selected-ring" />
			<circle cx={centerX} cy={connectorY} r="51" class="infra-halo" />
			<circle cx={centerX} cy={connectorY} r="41" class="infra-core" />
			<text x={centerX} y={connectorY - 2} text-anchor="middle" class="infra-title"
				>{nodeLabel('mcp-connector', 'MCP Connector')}</text
			>
			<text x={centerX} y={connectorY + 17} text-anchor="middle" class="infra-subtitle"
				>TRANSPORT</text
			>
		</g>

		<circle cx={centerX} cy={centerY} r="98" class="center-glow" />
		{#if anyActive}<circle cx={centerX} cy={centerY} r="59" class="center-ripple" />{/if}
		<g
			class="center-node"
			class:node-selected={selected('node', 'cptr-mcp')}
			role="button"
			tabindex="0"
			aria-label={`${nodeLabel('cptr-mcp', 'CPTR MCP')} server node`}
			onclick={() => choose({ kind: 'node', id: 'cptr-mcp' })}
			onkeydown={(event) => handleKey(event, { kind: 'node', id: 'cptr-mcp' })}
		>
			<circle cx={centerX} cy={centerY} r="65" class="node-selected-ring" />
			<circle cx={centerX} cy={centerY} r="57" />
			<circle cx={centerX} cy={centerY} r="46" class="center-node-inner" />
			<text x={centerX} y={centerY - 3} text-anchor="middle" class="center-title"
				>{nodeLabel('cptr-mcp', 'CPTR MCP')}</text
			>
			<text x={centerX} y={centerY + 18} text-anchor="middle" class="center-subtitle">SERVER</text>
		</g>

		<g
			class="infrastructure-node backend-node"
			class:node-selected={selected('node', 'cptr-backend')}
			role="button"
			tabindex="0"
			aria-label={`${nodeLabel('cptr-backend', 'CPTR Backend')} control API node`}
			onclick={() => choose({ kind: 'node', id: 'cptr-backend' })}
			onkeydown={(event) => handleKey(event, { kind: 'node', id: 'cptr-backend' })}
		>
			<rect
				x={centerX - 72}
				y={backendY - 44}
				width="144"
				height="88"
				rx="29"
				class="node-selected-ring"
			/>
			<rect
				x={centerX - 65}
				y={backendY - 37}
				width="130"
				height="74"
				rx="23"
				class="backend-core"
			/>
			<text x={centerX} y={backendY - 2} text-anchor="middle" class="infra-title"
				>{nodeLabel('cptr-backend', 'CPTR Backend')}</text
			>
			<text x={centerX} y={backendY + 17} text-anchor="middle" class="infra-subtitle"
				>CONTROL API</text
			>
		</g>

		{#each nodes as node (node.id)}
			<g
				class="client-node"
				class:client-connected={node.connected}
				class:client-active={node.active || pulseClientIds.has(node.id)}
				class:client-error={errorClientIds.has(node.id)}
				class:client-selected={selected('client', node.id)}
				role="button"
				tabindex="0"
				aria-label={clientAriaLabel(node)}
				onclick={() => choose({ kind: 'client', id: node.id })}
				onkeydown={(event) => handleKey(event, { kind: 'client', id: node.id })}
			>
				<circle cx={x(node)} cy={y(node)} r="49" class="node-selected-ring" />
				<circle cx={x(node)} cy={y(node)} r="43" class="client-halo" />
				<circle cx={x(node)} cy={y(node)} r="34" class="client-core" />
				<circle cx={x(node) + 25} cy={y(node) - 25} r="6" class="client-status" />
				<text x={x(node)} y={y(node) + 57} text-anchor="middle" class="client-label"
					>{nodeLabel(node.id, node.label)}</text
				>
				<text x={x(node)} y={y(node) + 73} text-anchor="middle" class="client-meta">
					{clientIdentityMeta(node)}
				</text>
			</g>
		{/each}

		{#each nodes.filter((node) => node.active || pulseClientIds.has(node.id)) as node (node.id)}
			<circle r="6" class="traffic-particle client-particle"
				><animateMotion dur="0.8s" repeatCount="indefinite" path={clientPath(node)} /></circle
			>
			<circle r="6" class="traffic-particle connector-particle"
				><animateMotion
					dur="0.8s"
					begin="0.22s"
					repeatCount="indefinite"
					path={`M ${centerX} ${connectorY + 44} L ${centerX} ${centerY - 47}`}
				/></circle
			>
			<circle r="6" class="traffic-particle backend-particle"
				><animateMotion
					dur="0.8s"
					begin="0.44s"
					repeatCount="indefinite"
					path={`M ${centerX} ${centerY + 47} L ${centerX} ${backendY - 40}`}
				/></circle
			>
		{/each}
	</svg>
</div>

<style>
	.topology-frame {
		color: var(--app-accent);
		background-image:
			radial-gradient(
				circle at 50% 42%,
				color-mix(in oklab, var(--app-accent) 8%, transparent),
				transparent 42%
			),
			linear-gradient(color-mix(in oklab, var(--app-fg) 3%, transparent) 1px, transparent 1px),
			linear-gradient(
				90deg,
				color-mix(in oklab, var(--app-fg) 3%, transparent) 1px,
				transparent 1px
			);
		background-size:
			auto,
			28px 28px,
			28px 28px;
	}
	.edge-route-underlay {
		fill: none;
		stroke: color-mix(in oklab, var(--app-accent) 8%, transparent);
		stroke-width: 8;
		stroke-linecap: round;
		pointer-events: none;
	}
	.topology-edge {
		fill: none;
		stroke: color-mix(in oklab, var(--app-fg) 16%, transparent);
		stroke-width: 2;
		stroke-dasharray: 6 9;
		transition:
			stroke 180ms ease,
			stroke-width 180ms ease;
		pointer-events: none;
	}
	.infrastructure-edge {
		stroke-dasharray: 4 7;
	}
	.topology-edge.edge-active {
		stroke: color-mix(in oklab, var(--app-accent) 84%, white 8%);
		stroke-width: 3;
	}
	.topology-edge.edge-flow {
		stroke: url(#edge-flow-gradient);
		stroke-width: 3.25;
		stroke-dasharray: 15 10;
		filter: url(#edge-glow);
		animation: edge-flow 1.15s linear infinite;
	}
	.topology-edge.edge-error {
		stroke: #ef4444;
		stroke-width: 3;
	}
	.edge-hit {
		fill: none;
		stroke: transparent;
		stroke-width: 22;
		cursor: pointer;
	}
	.edge-badge {
		cursor: pointer;
	}
	.edge-badge rect {
		fill: var(--app-surface-raised);
		stroke: var(--app-border);
	}
	.edge-health {
		stroke: var(--app-surface-raised);
		stroke-width: 1.5;
	}
	.edge-health--healthy {
		fill: #34d399;
	}
	.edge-health--degraded {
		fill: #f59e0b;
	}
	.edge-health--error {
		fill: #fb7185;
	}
	.edge-health--unknown {
		fill: #64748b;
	}
	.edge-badge text {
		fill: var(--app-fg-muted);
		font-size: 8px;
		font-weight: 650;
		pointer-events: none;
	}
	.topology-selected .edge-badge rect {
		stroke: var(--app-accent);
		stroke-width: 2;
	}
	.topology-selected .topology-edge {
		stroke: var(--app-accent);
		stroke-width: 3;
	}
	.center-glow {
		fill: url(#cptr-center-glow);
		color: var(--app-accent);
		pointer-events: none;
	}
	.center-ripple {
		fill: none;
		stroke: color-mix(in oklab, var(--app-accent) 80%, white 5%);
		stroke-width: 3;
		animation: center-ripple 1.2s ease-out infinite;
		pointer-events: none;
	}
	.center-node,
	.infrastructure-node,
	.client-node {
		cursor: pointer;
		outline: none;
	}
	.node-selected-ring {
		fill: none;
		stroke: var(--app-accent);
		stroke-width: 2;
		opacity: 0;
		filter: url(#soft-glow);
		transition:
			opacity 180ms ease,
			stroke-width 180ms ease;
		pointer-events: none;
	}
	.node-selected .node-selected-ring,
	.client-selected .node-selected-ring {
		opacity: 0.9;
		stroke-width: 3;
	}
	.center-node circle:first-child,
	.infra-core,
	.backend-core {
		fill: color-mix(in oklab, var(--app-accent) 12%, var(--app-surface-raised));
		stroke: color-mix(in oklab, var(--app-accent) 56%, var(--app-border));
		stroke-width: 2;
	}
	.center-node circle:first-child {
		filter: url(#soft-glow);
	}
	.center-node-inner {
		fill: var(--app-surface-raised);
		stroke: var(--app-border);
		stroke-width: 1;
	}
	.infra-halo {
		fill: color-mix(in oklab, var(--app-accent) 5%, transparent);
		stroke: color-mix(in oklab, var(--app-accent) 24%, var(--app-border));
		stroke-width: 1.5;
	}
	.node-selected .infra-halo,
	.node-selected .backend-core,
	.node-selected circle:first-child {
		stroke: var(--app-accent);
		stroke-width: 4;
		filter: url(#soft-glow);
	}
	.center-title,
	.infra-title,
	.client-label {
		fill: var(--app-fg);
		font-weight: 700;
		pointer-events: none;
	}
	.center-title {
		font-size: 16px;
	}
	.infra-title {
		font-size: 13px;
	}
	.center-subtitle,
	.infra-subtitle,
	.client-meta {
		fill: var(--app-fg-subtle);
		font-weight: 650;
		pointer-events: none;
	}
	.center-subtitle,
	.infra-subtitle {
		font-size: 8px;
		letter-spacing: 0.15em;
	}
	.client-halo {
		fill: transparent;
		stroke: color-mix(in oklab, var(--app-fg) 12%, transparent);
		stroke-width: 1.5;
	}
	.client-core {
		fill: var(--app-surface-raised);
		stroke: color-mix(in oklab, var(--app-fg) 20%, var(--app-border));
		stroke-width: 2;
	}
	.client-status {
		fill: #6b7280;
		stroke: var(--app-surface-raised);
		stroke-width: 3;
	}
	.client-connected .client-status {
		fill: #22c55e;
	}
	.client-active .client-halo,
	.client-selected .client-halo {
		fill: color-mix(in oklab, var(--app-accent) 8%, transparent);
		stroke: var(--app-accent);
		stroke-width: 2.5;
	}
	.client-active .client-core,
	.client-selected .client-core {
		stroke: var(--app-accent);
		filter: url(#soft-glow);
	}
	.client-error .client-halo,
	.client-error .client-core {
		stroke: #ef4444;
	}
	.client-node:focus-visible .client-halo,
	.center-node:focus-visible circle:first-child,
	.infrastructure-node:focus-visible .infra-halo,
	.infrastructure-node:focus-visible .backend-core,
	g[role='button']:focus-visible .edge-badge rect {
		stroke: var(--app-focus-ring);
		stroke-width: 4;
	}
	.client-label {
		font-size: 13px;
	}
	.client-meta {
		font-size: 9px;
	}
	.traffic-particle {
		fill: color-mix(in oklab, var(--app-accent) 88%, white 12%);
		filter: url(#soft-glow);
		pointer-events: none;
	}
	@keyframes edge-flow {
		to {
			stroke-dashoffset: -50;
		}
	}
	@keyframes center-ripple {
		0% {
			opacity: 0.9;
			transform-origin: center;
			transform: scale(0.9);
		}
		100% {
			opacity: 0;
			transform-origin: center;
			transform: scale(1.55);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.traffic-particle,
		.center-ripple,
		.edge-flow {
			animation: none !important;
		}
		.traffic-particle {
			display: none;
		}
	}
</style>
