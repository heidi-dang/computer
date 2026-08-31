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

	function x(node: McpTopologyNode): number {
		return node.x * width;
	}

	function y(node: McpTopologyNode): number {
		return node.y * height;
	}

	function handleKey(event: KeyboardEvent, clientId: string) {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			onselect?.(clientId);
		}
	}
</script>

<div
	class="topology-frame relative min-h-[22rem] overflow-hidden rounded-2xl border border-gray-200/80 bg-white/70 shadow-sm dark:border-white/10 dark:bg-gray-950/70 sm:min-h-[28rem]"
>
	{#if nodes.length === 0}
		<div class="absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
			<div class="max-w-sm">
				<div
					class="mx-auto mb-3 flex size-12 items-center justify-center rounded-2xl border border-gray-200 bg-gray-50 text-gray-400 dark:border-white/10 dark:bg-white/5 dark:text-gray-500"
				>
					<svg
						viewBox="0 0 24 24"
						class="size-6"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						aria-hidden="true"
					>
						<circle cx="12" cy="12" r="3" />
						<path
							d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"
						/>
					</svg>
				</div>
				<p class="text-sm font-medium text-gray-700 dark:text-gray-200">Waiting for MCP clients</p>
				<p class="mt-1 text-xs leading-5 text-gray-400 dark:text-gray-500">
					Connected ChatGPT, Claude, Gemini and compatible clients will appear around CPTR MCP when
					real transport telemetry arrives.
				</p>
			</div>
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
				<feGaussianBlur stdDeviation="10" result="blur" />
				<feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
			</filter>
		</defs>

		<!-- Edges first -->
		{#each nodes as node (node.id)}
			<line
				x1={x(node)}
				y1={y(node)}
				x2={centerX}
				y2={centerY}
				class:edge-active={node.active || pulseClientIds.has(node.id)}
				class:edge-error={errorClientIds.has(node.id)}
				class="topology-edge"
			/>
		{/each}

		<!-- Center glow and node -->
		<circle cx={centerX} cy={centerY} r="118" class="center-glow" />
		{#if nodes.some((node) => node.active || pulseClientIds.has(node.id))}
			<circle cx={centerX} cy={centerY} r="72" class="center-ripple" />
		{/if}
		<g class="center-node" aria-label="CPTR MCP server">
			<circle cx={centerX} cy={centerY} r="70" />
			<circle cx={centerX} cy={centerY} r="55" class="center-node-inner" />
			<text x={centerX} y={centerY - 4} text-anchor="middle" class="center-title">CPTR MCP</text>
			<text x={centerX} y={centerY + 20} text-anchor="middle" class="center-subtitle">SERVER</text>
		</g>

		<!-- Client nodes -->
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
				<circle cx={x(node)} cy={y(node)} r="49" class="client-halo" />
				<circle cx={x(node)} cy={y(node)} r="39" class="client-core" />
				<circle cx={x(node) + 29} cy={y(node) - 29} r="7" class="client-status" />
				<text x={x(node)} y={y(node) + 68} text-anchor="middle" class="client-label"
					>{node.label}</text
				>
				<text x={x(node)} y={y(node) + 85} text-anchor="middle" class="client-meta">
					{node.activeRequests > 0
						? `${node.activeRequests} active`
						: node.connected
							? 'connected'
							: 'recent'}
				</text>
			</g>
		{/each}

		<!-- Transient request particles last -->
		{#each nodes.filter((node) => node.active || pulseClientIds.has(node.id)) as node (node.id)}
			<circle r="7" class="traffic-particle">
				<animateMotion
					dur="0.9s"
					repeatCount="indefinite"
					path={`M ${x(node)} ${y(node)} L ${centerX} ${centerY}`}
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
				circle at 50% 50%,
				color-mix(in oklab, var(--app-accent, #60a5fa) 8%, transparent),
				transparent 38%
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
		stroke: color-mix(in oklab, var(--app-fg) 16%, transparent);
		stroke-width: 2;
		stroke-dasharray: 6 9;
		transition:
			stroke 180ms ease,
			stroke-width 180ms ease,
			opacity 180ms ease;
	}

	.topology-edge.edge-active {
		stroke: color-mix(in oklab, var(--app-accent, #60a5fa) 80%, white 8%);
		stroke-width: 3;
		stroke-dasharray: 0;
	}

	.topology-edge.edge-error {
		stroke: #ef4444;
		stroke-width: 3;
	}

	.center-glow {
		fill: url(#cptr-center-glow);
		color: var(--app-accent, #60a5fa);
	}

	.center-ripple {
		fill: none;
		stroke: color-mix(in oklab, var(--app-accent, #60a5fa) 80%, white 5%);
		stroke-width: 3;
		animation: center-ripple 1.2s ease-out infinite;
	}

	.center-node circle:first-child {
		fill: color-mix(in oklab, var(--app-accent, #60a5fa) 15%, var(--app-surface-raised, #111827));
		stroke: color-mix(in oklab, var(--app-accent, #60a5fa) 68%, white 8%);
		stroke-width: 2.5;
		filter: url(#soft-glow);
	}

	.center-node-inner {
		fill: color-mix(in oklab, var(--app-surface-raised, #111827) 92%, transparent);
		stroke: color-mix(in oklab, var(--app-fg) 12%, transparent);
		stroke-width: 1;
	}

	.center-title {
		fill: var(--app-fg, #e5e7eb);
		font-size: 18px;
		font-weight: 700;
		letter-spacing: -0.02em;
		pointer-events: none;
	}

	.center-subtitle {
		fill: var(--app-fg-subtle, #9ca3af);
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.18em;
		pointer-events: none;
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
		fill: color-mix(in oklab, var(--app-surface-raised, #111827) 94%, transparent);
		stroke: color-mix(in oklab, var(--app-fg) 20%, transparent);
		stroke-width: 2;
		transition:
			stroke 180ms ease,
			filter 180ms ease;
	}

	.client-status {
		fill: #6b7280;
		stroke: color-mix(in oklab, var(--app-surface-raised, #111827) 95%, transparent);
		stroke-width: 3;
	}

	.client-connected .client-status {
		fill: #22c55e;
	}

	.client-active .client-halo,
	.client-selected .client-halo {
		fill: color-mix(in oklab, var(--app-accent, #60a5fa) 8%, transparent);
		stroke: color-mix(in oklab, var(--app-accent, #60a5fa) 72%, white 4%);
		stroke-width: 2.5;
	}

	.client-active .client-core,
	.client-selected .client-core {
		stroke: color-mix(in oklab, var(--app-accent, #60a5fa) 78%, white 4%);
		filter: url(#soft-glow);
	}

	.client-error .client-halo,
	.client-error .client-core {
		stroke: #ef4444;
	}

	.client-node:focus-visible .client-halo {
		stroke: var(--app-accent, #60a5fa);
		stroke-width: 4;
	}

	.client-label {
		fill: var(--app-fg, #e5e7eb);
		font-size: 14px;
		font-weight: 650;
		pointer-events: none;
	}

	.client-meta {
		fill: var(--app-fg-subtle, #9ca3af);
		font-size: 10px;
		pointer-events: none;
	}

	.traffic-particle {
		fill: color-mix(in oklab, var(--app-accent, #60a5fa) 88%, white 12%);
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
