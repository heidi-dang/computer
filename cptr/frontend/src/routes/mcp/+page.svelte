<script lang="ts">
	import McpConsole from '$lib/components/mcp/McpConsole.svelte';
	import McpTopology from '$lib/components/mcp/McpTopology.svelte';

	type McpView = 'topology' | 'console';
	let view = $state<McpView>('topology');
</script>

<svelte:head>
	<title>MCP / Computer</title>
</svelte:head>

<div class="flex h-full flex-col overflow-hidden bg-white dark:bg-gray-950">
	<div
		class="shrink-0 border-b border-gray-100 bg-white px-3 py-2.5 dark:border-gray-800 dark:bg-gray-950 sm:px-4 sm:py-3"
	>
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div class="flex min-w-0 items-center gap-3">
				<div class="flex items-center gap-2">
					<svg
						class="size-4 text-gray-500 dark:text-gray-400"
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
					<h1 class="text-sm font-semibold text-gray-800 dark:text-gray-100">MCP</h1>
				</div>
				<span class="hidden truncate text-xs text-gray-400 dark:text-gray-500 sm:block"
					>Model Context Protocol — live traffic and server console</span
				>
			</div>

			<div
				class="flex rounded-xl border border-gray-200 bg-gray-50 p-1 dark:border-white/10 dark:bg-white/5"
				role="tablist"
				aria-label="MCP view"
			>
				<button
					class="min-h-11 rounded-lg px-3 text-xs font-medium transition-colors sm:min-h-0 sm:py-1.5 {view ===
					'topology'
						? 'bg-white text-gray-800 shadow-sm dark:bg-white/10 dark:text-gray-100'
						: 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'}"
					role="tab"
					aria-selected={view === 'topology'}
					onclick={() => (view = 'topology')}
				>
					Topology
				</button>
				<button
					class="min-h-11 rounded-lg px-3 text-xs font-medium transition-colors sm:min-h-0 sm:py-1.5 {view ===
					'console'
						? 'bg-white text-gray-800 shadow-sm dark:bg-white/10 dark:text-gray-100'
						: 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'}"
					role="tab"
					aria-selected={view === 'console'}
					onclick={() => (view = 'console')}
				>
					Console
				</button>
			</div>
		</div>
	</div>

	<div class="min-h-0 flex-1 overflow-hidden">
		{#if view === 'topology'}
			<McpTopology />
		{:else}
			<McpConsole />
		{/if}
	</div>
</div>
