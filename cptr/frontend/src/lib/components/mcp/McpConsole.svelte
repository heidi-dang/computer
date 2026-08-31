<script lang="ts">
	import { onMount, tick } from 'svelte';
	import McpCallCard from './McpCallCard.svelte';
	import McpToolForm from './McpToolForm.svelte';
	import McpServerList from './McpServerList.svelte';
	import type { McpCallRecord } from './McpCallCard.svelte';
	import { invokeToolStreaming, type McpToolSpec, type McpContentItem } from '$lib/apis/mcp';
	import { toast } from 'svelte-sonner';

	// ── State ────────────────────────────────────────────────────────────────────
	let callLog = $state<McpCallRecord[]>([]);
	let selectedServerId = $state<string | null>(null);
	let selectedTool = $state<McpToolSpec | null>(null);
	let isInvoking = $state(false);

	let logEl: HTMLDivElement;

	// ── Scroll to bottom whenever log grows ──────────────────────────────────────
	$effect(() => {
		callLog.length; // reactive dependency
		tick().then(() => {
			logEl?.scrollTo({ top: logEl.scrollHeight, behavior: 'smooth' });
		});
	});

	// ── Invoke ───────────────────────────────────────────────────────────────────
	async function handleInvoke(
		serverId: string,
		toolName: string,
		args: Record<string, unknown>
	) {
		if (isInvoking) return;
		isInvoking = true;

		// Look up server name from the selected tool context
		const serverName = selectedTool?._server_name ?? serverId;

		const record: McpCallRecord = {
			id: crypto.randomUUID(),
			serverId,
			serverName,
			toolName,
			arguments: args,
			status: 'running',
			result: [],
			startedAt: Date.now(),
		};

		callLog = [...callLog, record];
		const idx = callLog.length - 1;

		try {
			await invokeToolStreaming(serverId, toolName, args, {
				onChunk(item: McpContentItem) {
					callLog = callLog.map((r, i) =>
						i === idx ? { ...r, result: [...r.result, item] } : r
					);
				},
				onDone(result: McpContentItem[]) {
					callLog = callLog.map((r, i) =>
						i === idx ? { ...r, status: 'done', result, doneAt: Date.now() } : r
					);
				},
				onError(message: string) {
					callLog = callLog.map((r, i) =>
						i === idx ? { ...r, status: 'error', errorMessage: message, doneAt: Date.now() } : r
					);
					toast.error(`Tool error: ${message}`);
				},
			});
		} catch (e: any) {
			callLog = callLog.map((r, i) =>
				i === idx ? { ...r, status: 'error', errorMessage: e.message, doneAt: Date.now() } : r
			);
		} finally {
			isInvoking = false;
		}
	}

	function clearLog() {
		callLog = [];
	}

	function handleSelectTool(serverId: string, tool: McpToolSpec) {
		selectedServerId = serverId;
		selectedTool = { ...tool, _server_name: tool._server_name ?? serverId };
	}
</script>

<div class="flex h-full overflow-hidden">

	<!-- ── Left panel: server + tool list ─────────────────────────────────────── -->
	<aside class="w-56 shrink-0 flex flex-col border-r border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-950 overflow-hidden">
		<McpServerList
			bind:selectedServerId
			bind:selectedTool
			onSelectTool={handleSelectTool}
		/>
	</aside>

	<!-- ── Center: call log ───────────────────────────────────────────────────── -->
	<main class="flex-1 flex flex-col overflow-hidden bg-white dark:bg-gray-950">

		<!-- Log header -->
		<div class="flex items-center justify-between px-4 py-2.5 border-b border-gray-100 dark:border-gray-800 shrink-0">
			<div class="flex items-center gap-2">
				<span class="text-xs font-medium text-gray-500 dark:text-gray-400">Console</span>
				{#if callLog.length > 0}
					<span class="text-[0.6rem] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 tabular-nums">
						{callLog.length}
					</span>
				{/if}
			</div>
			{#if callLog.length > 0}
				<button
					class="text-[0.65rem] text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
					onclick={clearLog}
				>
					Clear
				</button>
			{/if}
		</div>

		<!-- Call log (scrollable) -->
		<div
			bind:this={logEl}
			class="flex-1 overflow-y-auto px-4 py-4 space-y-4"
		>
			{#if callLog.length === 0}
				<!-- Empty state -->
				<div class="flex flex-col items-center justify-center h-full text-center text-gray-400 dark:text-gray-500 select-none">
					<svg class="size-12 mb-3 opacity-30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
						<path d="M12 22V18" /><path d="M9 3V7" /><path d="M15 3V7" />
						<path d="M18 7H6C5.44772 7 5 7.44772 5 8V13C5 15.7614 7.23858 18 10 18H14C16.7614 18 19 15.7614 19 13V8C19 7.44772 18.5523 7 18 7Z"/>
					</svg>
					<p class="text-sm mb-1">No tool calls yet</p>
					<p class="text-xs opacity-60">Select a server → tool → invoke</p>
				</div>
			{:else}
				{#each callLog as record (record.id)}
					<McpCallCard {record} />
				{/each}
			{/if}
		</div>
	</main>

	<!-- ── Right panel: tool form ─────────────────────────────────────────────── -->
	<aside class="w-72 shrink-0 flex flex-col border-l border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-900/50 overflow-hidden">
		<McpToolForm
			tool={selectedTool}
			serverId={selectedServerId}
			onInvoke={handleInvoke}
			disabled={isInvoking}
		/>
	</aside>

</div>
