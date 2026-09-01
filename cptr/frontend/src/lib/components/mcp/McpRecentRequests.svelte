<script lang="ts">
	import type { McpFailureState } from '$lib/stores/mcp-diagnostics';
	import type { McpRecentRequestRow } from '$lib/stores/mcp-traffic';
	import McpDiagnosticDetail from './McpDiagnosticDetail.svelte';

	type Props = {
		rows: McpRecentRequestRow[];
		selectedClientId?: string | null;
		failures?: McpFailureState[];
		onrevealactivity?: (requestId: string | null, correlationId: string | null) => void;
	};

	let { rows, selectedClientId = null, failures = [], onrevealactivity }: Props = $props();
	let selectedRequestId = $state<string | null>(null);

	const visibleRows = $derived(
		selectedClientId ? rows.filter((row) => row.clientId === selectedClientId) : rows
	);
	const selected = $derived(
		selectedRequestId ? (rows.find((row) => row.requestId === selectedRequestId) ?? null) : null
	);
	const selectedDiagnostic = $derived(selected ? diagnosticFor(selected) : null);

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

	function diagnosticFor(row: McpRecentRequestRow): McpFailureState | null {
		const requestMatch = failures
			.filter((failure) => failure.requestId === row.requestId)
			.sort((left, right) => right.completedAtMs - left.completedAtMs)[0];
		if (requestMatch) return requestMatch;
		if (!row.correlationId) return null;
		return (
			failures
				.filter((failure) => failure.correlationId === row.correlationId)
				.sort((left, right) => right.completedAtMs - left.completedAtMs)[0] ?? null
		);
	}

	function toggle(row: McpRecentRequestRow) {
		selectedRequestId = selectedRequestId === row.requestId ? null : row.requestId;
	}
</script>

<section
	class="app-raised-surface flex min-h-0 flex-col overflow-hidden rounded-2xl border shadow-sm"
>
	<div class="app-surface flex items-center justify-between border-b px-3 py-2.5 sm:px-4 sm:py-3">
		<div class="min-w-0">
			<h2 class="text-sm font-semibold">Recent requests</h2>
			<p class="mt-0.5 truncate text-[0.6875rem] app-muted">
				{selectedClientId ? 'Filtered to selected client' : 'Live inbound MCP traffic'}
			</p>
		</div>
		<span
			class="app-subtle-surface rounded-full border px-2 py-1 text-[0.65rem] tabular-nums app-muted"
			>{visibleRows.length}</span
		>
	</div>

	<!-- Compact mobile list: no horizontal table overflow. -->
	<div class="min-h-0 flex-1 overflow-y-auto sm:hidden">
		{#if visibleRows.length === 0}
			<div class="px-4 py-10 text-center text-xs app-muted">No MCP requests observed yet.</div>
		{:else}
			{#each visibleRows as row (row.requestId)}
				<button
					type="button"
					class="app-interactive w-full min-h-11 border-b px-3 py-2.5 text-left {selectedRequestId ===
					row.requestId
						? 'app-interactive-active'
						: ''}"
					onclick={() => toggle(row)}
				>
					<div class="flex min-w-0 items-center justify-between gap-3">
						<span class="truncate text-xs font-semibold">{row.clientLabel}</span>
						<span class="flex shrink-0 items-center gap-1.5 text-[0.62rem] app-muted">
							<span
								class="size-1.5 rounded-full {row.status === 'active'
									? 'animate-pulse bg-blue-500'
									: row.status === 'error'
										? 'bg-red-500'
										: 'bg-emerald-500'}"
							></span>
							{row.status} · {when(row.completedAt ?? row.startedAt)}
						</span>
					</div>
					<div class="mt-1 flex min-w-0 items-center justify-between gap-3 text-[0.68rem]">
						<span class="min-w-0 truncate font-mono app-muted" title={methodTool(row)}
							>{methodTool(row)}</span
						>
						<span class="shrink-0 font-mono tabular-nums app-muted"
							>{bytes(row.requestBytes)} / {bytes(row.responseBytes)}</span
						>
					</div>
				</button>
			{/each}
		{/if}
	</div>

	<!-- Desktop table. -->
	<div class="hidden min-h-0 flex-1 overflow-auto sm:block">
		<table class="w-full border-collapse text-left text-xs">
			<thead
				class="app-subtle-surface sticky top-0 z-10 text-[0.65rem] font-medium uppercase tracking-wide app-muted backdrop-blur"
			>
				<tr>
					<th class="px-3 py-2.5">Client</th>
					<th class="px-3 py-2.5">Method / Tool</th>
					<th class="px-3 py-2.5">In / Out</th>
					<th class="px-3 py-2.5">Status</th>
					<th class="px-3 py-2.5 text-right">When</th>
				</tr>
			</thead>
			<tbody class="divide-y">
				{#if visibleRows.length === 0}
					<tr
						><td colspan="5" class="px-4 py-10 text-center text-xs app-muted"
							>No MCP requests observed yet.</td
						></tr
					>
				{:else}
					{#each visibleRows as row (row.requestId)}
						<tr
							class="app-interactive cursor-pointer {selectedRequestId === row.requestId
								? 'app-interactive-active'
								: ''}"
							onclick={() => toggle(row)}
						>
							<td class="px-3 py-2.5">
								<div class="font-medium">{row.clientLabel}</div>
								{#if row.clientVersion}<div class="mt-0.5 text-[0.65rem] app-muted">
										v{row.clientVersion}
									</div>{/if}
							</td>
							<td class="max-w-56 px-3 py-2.5">
								<div class="truncate font-mono text-[0.7rem]" title={methodTool(row)}>
									{methodTool(row)}
								</div>
								{#if row.toolName && row.method}<div
										class="mt-0.5 truncate text-[0.62rem] app-muted"
									>
										{row.method}
									</div>{/if}
							</td>
							<td class="px-3 py-2.5 font-mono text-[0.68rem] tabular-nums app-muted"
								>{bytes(row.requestBytes)} / {bytes(row.responseBytes)}</td
							>
							<td class="px-3 py-2.5">
								<span
									class="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[0.65rem] font-medium {row.status ===
									'active'
										? 'bg-blue-500/10 text-blue-400'
										: row.status === 'error'
											? 'bg-red-500/10 text-red-400'
											: 'bg-emerald-500/10 text-emerald-400'}"
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
							<td class="px-3 py-2.5 text-right text-[0.68rem] tabular-nums app-muted"
								>{when(row.completedAt ?? row.startedAt)}</td
							>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>

	{#if selected}
		<div class="app-subtle-surface border-t p-3 sm:p-4">
			<div class="mb-3 flex items-center justify-between gap-3">
				<div class="min-w-0">
					<p class="text-xs font-semibold">Request detail</p>
					<p class="mt-0.5 truncate font-mono text-[0.65rem] app-muted">
						{shortId(selected.requestId)}
					</p>
				</div>
				<button
					class="app-interactive min-h-11 rounded-lg px-3 text-xs app-muted sm:min-h-0 sm:py-1.5"
					onclick={() => (selectedRequestId = null)}>Close</button
				>
			</div>
			<dl class="grid grid-cols-2 gap-x-4 gap-y-3 text-[0.7rem] sm:grid-cols-3">
				<div>
					<dt class="app-muted">Client</dt>
					<dd class="mt-0.5">
						{selected.clientLabel}{selected.clientVersion ? ` · ${selected.clientVersion}` : ''}
					</dd>
				</div>
				<div>
					<dt class="app-muted">Method</dt>
					<dd class="mt-0.5 break-all font-mono">{selected.method ?? '—'}</dd>
				</div>
				<div>
					<dt class="app-muted">Tool</dt>
					<dd class="mt-0.5 break-all font-mono">{selected.toolName ?? '—'}</dd>
				</div>
				<div>
					<dt class="app-muted">Duration</dt>
					<dd class="mt-0.5">{selected.durationMs == null ? '—' : `${selected.durationMs} ms`}</dd>
				</div>
				<div>
					<dt class="app-muted">Bytes</dt>
					<dd class="mt-0.5">{bytes(selected.requestBytes)} / {bytes(selected.responseBytes)}</dd>
				</div>
				<div>
					<dt class="app-muted">Error code</dt>
					<dd class="mt-0.5 font-mono">{selected.errorCode ?? '—'}</dd>
				</div>
				<div>
					<dt class="app-muted">Session</dt>
					<dd class="mt-0.5 font-mono">{shortId(selected.sessionId)}</dd>
				</div>
				<div>
					<dt class="app-muted">Correlation ID</dt>
					<dd class="mt-0.5 font-mono" title={selected.correlationId ?? undefined}>
						{shortId(selected.correlationId)}
					</dd>
				</div>
				<div>
					<dt class="app-muted">Started</dt>
					<dd class="mt-0.5">{new Date(selected.startedAt).toLocaleString()}</dd>
				</div>
				<div>
					<dt class="app-muted">Completed</dt>
					<dd class="mt-0.5">
						{selected.completedAt ? new Date(selected.completedAt).toLocaleString() : '—'}
					</dd>
				</div>
			</dl>

			{#if selected.status === 'error'}
				<div class="mt-4">
					{#if selectedDiagnostic}
						<McpDiagnosticDetail diagnostic={selectedDiagnostic} {onrevealactivity} />
					{:else}
						<div class="app-surface rounded-xl border p-3 text-[0.7rem]">
							<p class="font-medium">No deeper diagnostic was captured.</p>
							<p class="mt-1 leading-5 app-muted">
								The traffic record still preserves the safe error code, timing, byte counts, request
								ID, and correlation ID shown above.
							</p>
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</section>
