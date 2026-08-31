<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import type { McpContentItem } from '$lib/apis/mcp';

	export interface McpCallRecord {
		id: string;
		serverId: string;
		serverName: string;
		toolName: string;
		arguments: Record<string, unknown>;
		status: 'pending' | 'running' | 'done' | 'error';
		result: McpContentItem[];
		startedAt: number;
		doneAt?: number;
		errorMessage?: string;
	}

	interface Props {
		record: McpCallRecord;
	}
	let { record }: Props = $props();

	let expanded = $state(true);

	$effect(() => {
		// Auto-expand running/error cards, auto-collapse done cards after a beat
		if (record.status === 'running') expanded = true;
	});

	const elapsed = $derived.by(() => {
		if (!record.doneAt) return null;
		const ms = record.doneAt - record.startedAt;
		return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
	});

	const argsFormatted = $derived.by(() => {
		try {
			return JSON.stringify(record.arguments, null, 2);
		} catch {
			return String(record.arguments);
		}
	});

	function toggleExpanded() {
		expanded = !expanded;
	}
</script>

<div class="w-full min-w-0 flex flex-col">
	<!-- Header row (always visible, clickable) -->
	<div
		role="button"
		tabindex="0"
		class="w-full text-left flex items-center gap-1.5 py-1 text-sm cursor-pointer
			text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors select-none
			{record.status === 'running' ? 'shimmer' : ''}"
		onclick={toggleExpanded}
		onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && toggleExpanded()}
	>
		<!-- Status icon -->
		{#if record.status === 'running'}
			<!-- Spinner -->
			<svg class="size-4 shrink-0" viewBox="0 0 24 24" fill="currentColor">
				<style>.mcp-spin{transform-origin:center;animation:mcp-spin-a 0.75s infinite linear}@keyframes mcp-spin-a{100%{transform:rotate(360deg)}}</style>
				<path d="M12,1A11,11,0,1,0,23,12,11,11,0,0,0,12,1Zm0,19a8,8,0,1,1,8-8A8,8,0,0,1,12,20Z" opacity=".25"/>
				<path d="M10.14,1.16a11,11,0,0,0-9,8.92A1.59,1.59,0,0,0,2.46,12,1.52,1.52,0,0,0,4.11,10.7a8,8,0,0,1,6.66-6.61A1.42,1.42,0,0,0,12,2.69h0A1.57,1.57,0,0,0,10.14,1.16Z" class="mcp-spin"/>
			</svg>
		{:else if record.status === 'done'}
			<svg class="size-4 shrink-0 text-emerald-500 dark:text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/>
			</svg>
		{:else if record.status === 'error'}
			<svg class="size-4 shrink-0 text-red-400 dark:text-red-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
				<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
			</svg>
		{:else}
			<!-- pending -->
			<svg class="size-4 shrink-0 text-gray-300 dark:text-gray-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
				<circle cx="12" cy="12" r="9"/>
			</svg>
		{/if}

		<!-- Tool name + server -->
		<div class="flex-1 min-w-0 flex items-baseline gap-1.5">
			<span class="font-mono text-sm font-medium text-gray-800 dark:text-gray-100 truncate">{record.toolName}</span>
			<span class="text-[0.6rem] text-gray-400 dark:text-gray-500 shrink-0">{record.serverName}</span>
		</div>

		<!-- Elapsed time -->
		{#if elapsed}
			<span class="text-[0.6rem] text-gray-400 dark:text-gray-500 shrink-0 tabular-nums">{elapsed}</span>
		{/if}

		<!-- Chevron -->
		<svg
			viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"
			class="size-2.5 text-gray-400 shrink-0 transition-transform duration-150 {expanded ? 'rotate-180' : ''}"
		>
			<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5"/>
		</svg>
	</div>

	<!-- Expanded body -->
	{#if expanded}
		<div transition:slide={{ duration: 200, easing: quintOut, axis: 'y' }}>
			<div class="border border-gray-100 dark:border-gray-800 rounded-2xl my-1.5 p-3 space-y-3 overflow-hidden">

				<!-- Arguments -->
				{#if Object.keys(record.arguments).length > 0}
					<div>
						<div class="text-[0.6rem] uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1.5 px-0.5">Input</div>
						<pre class="text-xs font-mono text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 rounded-xl px-3 py-2 overflow-x-auto whitespace-pre-wrap break-words max-h-48">{argsFormatted}</pre>
					</div>
				{/if}

				<!-- Error -->
				{#if record.status === 'error' && record.errorMessage}
					<div class="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/20 rounded-xl px-3 py-2">
						{record.errorMessage}
					</div>
				{/if}

				<!-- Running pulse -->
				{#if record.status === 'running'}
					<div class="flex items-center gap-2 text-xs text-gray-400 dark:text-gray-500">
						<span class="flex gap-0.5">
							<span class="size-1 rounded-full bg-gray-300 dark:bg-gray-600 animate-bounce" style="animation-delay:0ms"></span>
							<span class="size-1 rounded-full bg-gray-300 dark:bg-gray-600 animate-bounce" style="animation-delay:150ms"></span>
							<span class="size-1 rounded-full bg-gray-300 dark:bg-gray-600 animate-bounce" style="animation-delay:300ms"></span>
						</span>
						<span>Calling tool…</span>
					</div>
				{/if}

				<!-- Result content items -->
				{#if record.result.length > 0}
					<div>
						<div class="text-[0.6rem] uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1.5 px-0.5">Output</div>
						<div class="space-y-2">
							{#each record.result as item, i (i)}
								{#if item.type === 'text'}
									<pre class="text-xs text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-words font-mono max-h-64 overflow-auto bg-gray-50 dark:bg-gray-900 rounded-xl px-3 py-2 leading-relaxed">{item.text}</pre>
								{:else if item.type === 'image' && item.data}
									<img
										src="data:{item.mimeType ?? 'image/png'};base64,{item.data}"
										alt="MCP tool result"
										class="max-w-full rounded-xl border border-gray-100 dark:border-gray-800"
									/>
								{:else if item.type === 'resource'}
									<div class="text-xs text-blue-600 dark:text-blue-400 font-mono px-1">{item.uri}</div>
								{:else}
									<pre class="text-xs text-gray-500 dark:text-gray-400 font-mono px-1 whitespace-pre-wrap">{JSON.stringify(item, null, 2)}</pre>
								{/if}
							{/each}
						</div>
					</div>
				{/if}

			</div>
		</div>
	{/if}
</div>
