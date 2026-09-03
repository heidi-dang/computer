<script lang="ts">
	import McpConsole from '$lib/components/mcp/McpConsole.svelte';
	import McpDarkFactory from '$lib/components/mcp/McpDarkFactory.svelte';
	import McpTopology from '$lib/components/mcp/McpTopology.svelte';

	type McpView = 'topology' | 'console' | 'factory';
	let view = $state<McpView>('topology');
	let focusRequestId = $state<string | null>(null);
	let focusCorrelationId = $state<string | null>(null);

	function revealActivity(requestId: string | null, correlationId: string | null) {
		focusRequestId = requestId;
		focusCorrelationId = correlationId;
		view = 'console';
	}
</script>

<svelte:head>
	<title>MCP / Computer</title>
</svelte:head>

<div class="app-theme flex h-full flex-col overflow-hidden">
	<header class="app-surface shrink-0 border-b px-2 py-2 sm:px-4 sm:py-2.5">
		<div class="flex min-w-0 items-center gap-2">
			<a
				href="/"
				aria-label="Back to CPTR Home"
				class="app-interactive flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-xl sm:min-h-9 sm:min-w-9"
			>
				<svg
					viewBox="0 0 24 24"
					class="size-4 app-muted"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					aria-hidden="true"
				>
					<path stroke-linecap="round" stroke-linejoin="round" d="m15 18-6-6 6-6" />
				</svg>
			</a>

			<div class="flex min-w-0 flex-1 items-center gap-2">
				<div
					class="app-accent-surface flex size-8 shrink-0 items-center justify-center rounded-xl border"
				>
					<svg
						class="size-4 app-accent"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M12 22V18" /><path d="M9 3V7" /><path d="M15 3V7" />
						<path
							d="M18 7H6C5.44772 7 5 7.44772 5 8V13C5 15.7614 7.23858 18 10 18H14C16.7614 18 19 15.7614 19 13V8C19 7.44772 18.5523 7 18 7Z"
						/>
					</svg>
				</div>
				<div class="min-w-0">
					<h1 class="truncate text-sm font-semibold">MCP</h1>
					<p class="hidden truncate text-[0.68rem] app-muted sm:block">
						Live topology, console, and Dark Factory operations
					</p>
				</div>
			</div>

			<div
				class="app-subtle-surface scrollbar-none flex max-w-[66vw] shrink-0 overflow-x-auto rounded-xl border p-1 sm:max-w-none"
				role="tablist"
				aria-label="MCP view"
			>
				<button
					class="app-interactive min-h-11 rounded-lg px-2.5 text-xs font-medium sm:min-h-0 sm:py-1.5 {view ===
					'topology'
						? 'app-interactive-active'
						: 'app-muted'}"
					role="tab"
					aria-selected={view === 'topology'}
					onclick={() => (view = 'topology')}
				>
					Topology
				</button>
				<button
					class="app-interactive min-h-11 shrink-0 rounded-lg px-2.5 text-xs font-medium sm:min-h-0 sm:py-1.5 {view ===
					'console'
						? 'app-interactive-active'
						: 'app-muted'}"
					role="tab"
					aria-selected={view === 'console'}
					onclick={() => (view = 'console')}
				>
					Console
				</button>
				<button
					class="app-interactive min-h-11 shrink-0 rounded-lg px-2.5 text-xs font-medium sm:min-h-0 sm:py-1.5 {view ===
					'factory'
						? 'app-interactive-active'
						: 'app-muted'}"
					role="tab"
					aria-selected={view === 'factory'}
					onclick={() => (view = 'factory')}
				>
					Dark Factory
				</button>
			</div>
		</div>
	</header>

	<div class="min-h-0 flex-1 overflow-hidden">
		{#if view === 'topology'}
			<McpTopology onrevealactivity={revealActivity} />
		{:else if view === 'console'}
			<McpConsole {focusRequestId} {focusCorrelationId} />
		{:else}
			<McpDarkFactory />
		{/if}
	</div>
</div>
