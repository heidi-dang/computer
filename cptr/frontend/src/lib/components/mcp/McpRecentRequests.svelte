<script lang="ts">
	import type { McpRecentRequestRow } from '$lib/stores/mcp-traffic';

	type Props = {
		rows: McpRecentRequestRow[];
		selectedClientId?: string | null;
	};

	let { rows, selectedClientId = null }: Props = $props();
	let selectedRequestId = $state<string | null>(null);

	const visibleRows = $derived(
		selectedClientId ? rows.filter((row) => row.clientId === selectedClientId) : rows
	);
	const selected = $derived(
		selectedRequestId ? (rows.find((row) => row.requestId === selectedRequestId) ?? null) : null
	);

	function bytes(value: number | null): string {
		if (value == null) return '—';
		if (value < 1024) return `${value} B`;
		if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KB`;
		return `${(value / (1024 * 1024)).toFixed(1)} MB`;
	}

	function when(timestamp: number): string {
		if (!timestamp) return '—';
		const delta = Math.max(0, Date.now() - timestamp);
		if (delta < 5_000) return 'now';
		if (delta < 60_000) return `${Math.floor(delta / 1000)}s`;
		if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m`;
		return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}

	function shortId(value: string | null): string {
		if (!value) return '—';
		return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
	}

	function methodTool(row: McpRecentRequestRow): string {
		return row.toolName || row.method || 'MCP request';
	}
</script>

<section
	class="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-gray-200/80 bg-white/80 shadow-sm dark:border-white/10 dark:bg-gray-950/75"
>
	<div
		class="flex items-center justify-between border-b border-gray-100 px-4 py-3 dark:border-white/8"
	>
		<div>
			<h2 class="text-sm font-semibold text-gray-800 dark:text-gray-100">Recent requests</h2>
			<p class="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500">
				{selectedClientId ? 'Filtered to selected client' : 'Live inbound MCP traffic'}
			</p>
		</div>
		<span
			class="rounded-full bg-gray-100 px-2 py-1 text-[0.65rem] tabular-nums text-gray-500 dark:bg-white/7 dark:text-gray-400"
			>{visibleRows.length}</span
		>
	</div>

	<div class="min-h-0 overflow-auto">
		<table class="w-full min-w-[39rem] border-collapse text-left text-xs">
			<thead
				class="sticky top-0 z-10 bg-gray-50/95 text-[0.65rem] font-medium uppercase tracking-wide text-gray-400 backdrop-blur dark:bg-gray-900/95 dark:text-gray-500"
			>
				<tr>
					<th class="px-3 py-2.5">Client</th>
					<th class="px-3 py-2.5">Method / Tool</th>
					<th class="px-3 py-2.5">In / Out</th>
					<th class="px-3 py-2.5">Status</th>
					<th class="px-3 py-2.5 text-right">When</th>
				</tr>
			</thead>
			<tbody class="divide-y divide-gray-100 dark:divide-white/7">
				{#if visibleRows.length === 0}
					<tr>
						<td colspan="5" class="px-4 py-10 text-center text-xs text-gray-400 dark:text-gray-500">
							No MCP requests observed yet.
						</td>
					</tr>
				{:else}
					{#each visibleRows as row (row.requestId)}
						<tr
							class="cursor-pointer transition-colors hover:bg-gray-50 dark:hover:bg-white/4 {selectedRequestId ===
							row.requestId
								? 'bg-blue-50/70 dark:bg-blue-500/8'
								: ''}"
							onclick={() =>
								(selectedRequestId = selectedRequestId === row.requestId ? null : row.requestId)}
						>
							<td class="px-3 py-2.5">
								<div class="font-medium text-gray-700 dark:text-gray-200">{row.clientLabel}</div>
								{#if row.clientVersion}<div class="mt-0.5 text-[0.65rem] text-gray-400">
										v{row.clientVersion}
									</div>{/if}
							</td>
							<td class="max-w-56 px-3 py-2.5">
								<div
									class="truncate font-mono text-[0.7rem] text-gray-600 dark:text-gray-300"
									title={methodTool(row)}
								>
									{methodTool(row)}
								</div>
								{#if row.toolName && row.method}<div
										class="mt-0.5 truncate text-[0.62rem] text-gray-400"
									>
										{row.method}
									</div>{/if}
							</td>
							<td
								class="px-3 py-2.5 font-mono text-[0.68rem] tabular-nums text-gray-500 dark:text-gray-400"
								>{bytes(row.requestBytes)} / {bytes(row.responseBytes)}</td
							>
							<td class="px-3 py-2.5">
								<span
									class="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[0.65rem] font-medium {row.status ===
									'active'
										? 'bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-300'
										: row.status === 'error'
											? 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-300'
											: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'}"
								>
									<span
										class="size-1.5 rounded-full {row.status === 'active'
											? 'animate-pulse bg-blue-500'
											: row.status === 'error'
												? 'bg-red-500'
												: 'bg-emerald-500'}"
									></span>
									{row.status}
								</span>
							</td>
							<td class="px-3 py-2.5 text-right text-[0.68rem] tabular-nums text-gray-400"
								>{when(row.completedAt ?? row.startedAt)}</td
							>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>

	{#if selected}
		<div class="border-t border-gray-100 bg-gray-50/70 p-4 dark:border-white/8 dark:bg-white/3">
			<div class="mb-3 flex items-center justify-between gap-3">
				<div>
					<p class="text-xs font-semibold text-gray-700 dark:text-gray-200">Request detail</p>
					<p class="mt-0.5 font-mono text-[0.65rem] text-gray-400">{shortId(selected.requestId)}</p>
				</div>
				<button
					class="min-h-11 rounded-lg px-3 text-xs text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/7 sm:min-h-0 sm:py-1.5"
					onclick={() => (selectedRequestId = null)}>Close</button
				>
			</div>
			<dl class="grid grid-cols-2 gap-x-4 gap-y-3 text-[0.7rem] sm:grid-cols-3">
				<div>
					<dt class="text-gray-400">Client</dt>
					<dd class="mt-0.5 text-gray-700 dark:text-gray-200">
						{selected.clientLabel}{selected.clientVersion ? ` · ${selected.clientVersion}` : ''}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Method</dt>
					<dd class="mt-0.5 break-all font-mono text-gray-700 dark:text-gray-200">
						{selected.method ?? '—'}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Tool</dt>
					<dd class="mt-0.5 break-all font-mono text-gray-700 dark:text-gray-200">
						{selected.toolName ?? '—'}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Duration</dt>
					<dd class="mt-0.5 text-gray-700 dark:text-gray-200">
						{selected.durationMs == null ? '—' : `${selected.durationMs} ms`}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Bytes</dt>
					<dd class="mt-0.5 text-gray-700 dark:text-gray-200">
						{bytes(selected.requestBytes)} / {bytes(selected.responseBytes)}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Error code</dt>
					<dd class="mt-0.5 font-mono text-gray-700 dark:text-gray-200">
						{selected.errorCode ?? '—'}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Session</dt>
					<dd class="mt-0.5 font-mono text-gray-700 dark:text-gray-200">
						{shortId(selected.sessionId)}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Started</dt>
					<dd class="mt-0.5 text-gray-700 dark:text-gray-200">
						{new Date(selected.startedAt).toLocaleString()}
					</dd>
				</div>
				<div>
					<dt class="text-gray-400">Completed</dt>
					<dd class="mt-0.5 text-gray-700 dark:text-gray-200">
						{selected.completedAt ? new Date(selected.completedAt).toLocaleString() : '—'}
					</dd>
				</div>
			</dl>
		</div>
	{/if}
</section>
