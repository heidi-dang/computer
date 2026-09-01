<script lang="ts">
	import type { McpTopologyNode } from '$lib/stores/mcp-traffic';

	type Props = {
		nodes: McpTopologyNode[];
		selectedClientId?: string | null;
		pulseClientIds?: Set<string>;
		errorClientIds?: Set<string>;
		onselect?: (clientId: string) => void;
	};

	let {
		nodes,
		selectedClientId = null,
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

	function handleKey(event: KeyboardEvent, clientId: string) {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			onselect?.(clientId);
		}
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
			<filter id="soft-glow" x="-60%" y="-60%" width="220%" height="220%">
				<feGaussianBlur stdDeviation="9" result="blur" />
				<feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
			</filter>
		</defs>

		<!-- Dynamic MCP clients terminate at the fixed connector. -->
		{#each nodes as node (node.id)}
			<path
				d={clientPath(node)}
				class:edge-active={node.active || pulseClientIds.has(node.id)}
				class:edge-error={errorClientIds.has(node.id)}
				class="topology-edge"
			/>
		{/each}

		<!-- Fixed infrastructure path: MCP Connector → CPTR MCP → CPTR Backend. -->
		<line
			x1={centerX}
			y1={connectorY + 44}
			x2={centerX}
			y2={centerY - 47}
			class:edge-active={anyActive}
			class:edge-error={anyError}
			class="topology-edge infrastructure-edge"
		/>
		<line
			x1={centerX}
			y1={centerY + 47}
			x2={centerX}
			y2={backendY - 40}
			class:edge-active={anyActive}
			class:edge-error={anyError}
			class="topology-edge infrastructure-edge"
		/>

		<!-- MCP Connector -->
		<g class="infrastructure-node connector-node" aria-label="MCP Connector">
			<circle cx={centerX} cy={connectorY} r="51" class="infra-halo" />
			<circle cx={centerX} cy={connectorY} r="41" class="infra-core" />
			<text x={centerX} y={connectorY - 2} text-anchor="middle" class="infra-title"
				>MCP Connector</text
			>
			<text x={centerX} y={connectorY + 17} text-anchor="middle" class="infra-subtitle"
				>TRANSPORT</text
			>
		</g>

		<!-- CPTR MCP center -->
		<circle cx={centerX} cy={centerY} r="98" class="center-glow" />
		{#if anyActive}
			<circle cx={centerX} cy={centerY} r="59" class="center-ripple" />
		{/if}
		<g class="center-node" aria-label="CPTR MCP server">
			<circle cx={centerX} cy={centerY} r="57" />
			<circle cx={centerX} cy={centerY} r="46" class="center-node-inner" />
			<text x={centerX} y={centerY - 3} text-anchor="middle" class="center-title">CPTR MCP</text>
			<text x={centerX} y={centerY + 18} text-anchor="middle" class="center-subtitle">SERVER</text>
		</g>

		<!-- CPTR Backend -->
		<g class="infrastructure-node backend-node" aria-label="CPTR Backend">
			<rect
				x={centerX - 65}
				y={backendY - 37}
				width="130"
				height="74"
				rx="23"
				class="backend-core"
			/>
			<text x={centerX} y={backendY - 2} text-anchor="middle" class="infra-title">CPTR Backend</text
			>
			<text x={centerX} y={backendY + 17} text-anchor="middle" class="infra-subtitle"
				>CONTROL API</text
			>
		</g>

		<!-- Dynamic client labels come only from telemetry node data. -->
		{#each nodes as node (node.id)}
			<g
				class="client-node"
				class:client-connected={node.connected}
				class:client-active={node.active || pulseClientIds.has(node.id)}
				class:client-error={errorClientIds.has(node.id)}
				class:client-selected={selectedClientId === node.id}
				role="button"
				tabindex="0"
				aria-label={`${node.label}: ${node.connected ? 'connected' : 'idle'}, ${node.activeRequests} active requests`}
				onclick={() => onselect?.(node.id)}
				onkeydown={(event) => handleKey(event, node.id)}
			>
				<circle cx={x(node)} cy={y(node)} r="43" class="client-halo" />
				<circle cx={x(node)} cy={y(node)} r="34" class="client-core" />
				<circle cx={x(node) + 25} cy={y(node) - 25} r="6" class="client-status" />
				<text x={x(node)} y={y(node) + 57} text-anchor="middle" class="client-label"
					>{node.label}</text
				>
				<text x={x(node)} y={y(node) + 73} text-anchor="middle" class="client-meta">
					{node.activeRequests > 0
						? `${node.activeRequests} active`
						: node.connected
							? 'connected'
							: 'recent'}
				</text>
			</g>
		{/each}

		<!-- Active request particles traverse the complete client → connector → MCP → backend path. -->
		{#each nodes.filter((node) => node.active || pulseClientIds.has(node.id)) as node (node.id)}
			<circle r="6" class="traffic-particle client-particle">
				<animateMotion dur="0.8s" repeatCount="indefinite" path={clientPath(node)} />
			</circle>
			<circle r="6" class="traffic-particle connector-particle">
				<animateMotion
					dur="0.8s"
					begin="0.22s"
					repeatCount="indefinite"
					path={`M ${centerX} ${connectorY + 44} L ${centerX} ${centerY - 47}`}
				/>
			</circle>
			<circle r="6" class="traffic-particle backend-particle">
				<animateMotion
					dur="0.8s"
					begin="0.44s"
					repeatCount="indefinite"
					path={`M ${centerX} ${centerY + 47} L ${centerX} ${backendY - 40}`}
				/>
			</circle>
		{/each}
	</svg>
</div>

<style>
	.topology-frame {
		color: var(--app-accent, #60a5fa);
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

	.topology-edge {
		fill: none;
		stroke: color-mix(in oklab, var(--app-fg) 16%, transparent);
		stroke-width: 2;
		stroke-dasharray: 6 9;
		transition:
			stroke 180ms ease,
			stroke-width 180ms ease,
			opacity 180ms ease;
	}

	.infrastructure-edge {
		stroke-dasharray: 4 7;
	}

	.topology-edge.edge-active {
		stroke: color-mix(in oklab, var(--app-accent) 84%, white 8%);
		stroke-width: 3;
		stroke-dasharray: 0;
	}

	.topology-edge.edge-error {
		stroke: #ef4444;
		stroke-width: 3;
	}

	.center-glow {
		fill: url(#cptr-center-glow);
		color: var(--app-accent);
	}

	.center-ripple {
		fill: none;
		stroke: color-mix(in oklab, var(--app-accent) 80%, white 5%);
		stroke-width: 3;
		animation: center-ripple 1.2s ease-out infinite;
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

	.client-node {
		cursor: pointer;
		outline: none;
	}

	.client-halo {
		fill: transparent;
		stroke: color-mix(in oklab, var(--app-fg) 12%, transparent);
		stroke-width: 1.5;
		transition:
			fill 180ms ease,
			stroke 180ms ease,
			stroke-width 180ms ease;
	}

	.client-core {
		fill: var(--app-surface-raised);
		stroke: color-mix(in oklab, var(--app-fg) 20%, var(--app-border));
		stroke-width: 2;
		transition:
			stroke 180ms ease,
			filter 180ms ease;
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
		stroke: color-mix(in oklab, var(--app-accent) 72%, white 4%);
		stroke-width: 2.5;
	}

	.client-active .client-core,
	.client-selected .client-core {
		stroke: color-mix(in oklab, var(--app-accent) 78%, white 4%);
		filter: url(#soft-glow);
	}

	.client-error .client-halo,
	.client-error .client-core {
		stroke: #ef4444;
	}

	.client-node:focus-visible .client-halo {
		stroke: var(--app-focus-ring, var(--app-accent));
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
		.center-ripple {
			animation: none !important;
		}
		.traffic-particle {
			display: none;
		}
	}
</style>
