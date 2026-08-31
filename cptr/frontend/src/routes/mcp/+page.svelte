<script lang="ts">
	import McpConsole from '$lib/components/mcp/McpConsole.svelte';
	import { t } from '$lib/i18n';
</script>

<svelte:head>
	<title>MCP Console / Computer</title>
</svelte:head>

<div class="flex flex-col h-full overflow-hidden">
	<!-- Page header -->
	<div class="flex items-center gap-3 px-4 py-3 border-b border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-950 shrink-0">
		<div class="flex items-center gap-2">
			<svg class="size-4 text-gray-500 dark:text-gray-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
				<path d="M12 22V18" /><path d="M9 3V7" /><path d="M15 3V7" />
				<path d="M18 7H6C5.44772 7 5 7.44772 5 8V13C5 15.7614 7.23858 18 10 18H14C16.7614 18 19 15.7614 19 13V8C19 7.44772 18.5523 7 18 7Z"/>
			</svg>
			<h1 class="text-sm font-semibold text-gray-800 dark:text-gray-100">MCP Console</h1>
		</div>
		<span class="text-xs text-gray-400 dark:text-gray-500">Model Context Protocol — inspect &amp; invoke tools directly</span>
	</div>

	<!-- Main content: full height console -->
	<div class="flex-1 overflow-hidden">
		<McpConsole />
	</div>
</div>
