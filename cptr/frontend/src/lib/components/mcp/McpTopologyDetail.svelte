<script lang="ts">
	import { updateMcpTopologyConfig, type McpTopologyConfig } from '$lib/apis/mcp';
	import type { McpTrafficClientState } from '$lib/stores/mcp-traffic';
	import type {
		McpBackendMetricsState,
		McpFailureState,
		McpLatencySummaryState
	} from '$lib/stores/mcp-diagnostics';
	import type { McpTopologySelection } from '$lib/stores/mcp-topology';
	import McpBackendMonitor from './McpBackendMonitor.svelte';

	type Props = {
		selection: NonNullable<McpTopologySelection>;
		canonicalName: string;
		aliases: Record<string, string>;
		latency?: McpLatencySummaryState | null;
		client?: McpTrafficClientState | null;
		systemHistory?: McpBackendMetricsState[];
		failures?: McpFailureState[];
		streamHealth?: { subscriberCount: number; slowSubscriberDrops: number } | null;
		onconfig?: (config: McpTopologyConfig) => void;
	};

	let {
		selection,
		canonicalName,
		aliases,
		latency = null,
		client = null,
		systemHistory = [],
		failures = [],
		streamHealth = null,
		onconfig
	}: Props = $props();

	let draft = $state('');
	let submitting = $state(false);
	let error = $state<string | null>(null);
	let editingKey = '';

	$effect(() => {
		const nextKey = `${selection.kind}:${selection.id}:${aliases[selection.id] ?? ''}`;
		if (nextKey === editingKey) return;
		editingKey = nextKey;
		draft = aliases[selection.id] ?? '';
		error = null;
	});

	function metricLabel(metric: McpLatencySummaryState['metricType']): string {
		if (metric === 'observed_request_time') return 'Observed request time';
		if (metric === 'adapter_handoff') return 'Adapter handoff';
		return 'Backend API RTT';
	}

	async function saveAlias() {
		if (submitting) return;
		submitting = true;
		error = null;
		try {
			const config = await updateMcpTopologyConfig({ [selection.id]: draft });
			onconfig?.(config);
		} catch (reason) {
			error = reason instanceof Error ? reason.message : 'Unable to save topology alias.';
		} finally {
			submitting = false;
		}
	}

	async function resetAlias() {
		if (submitting) return;
		submitting = true;
		error = null;
		try {
			const config = await updateMcpTopologyConfig({ [selection.id]: null });
			draft = '';
			onconfig?.(config);
		} catch (reason) {
			error = reason instanceof Error ? reason.message : 'Unable to reset topology alias.';
		} finally {
			submitting = false;
		}
	}
</script>

<section class="app-raised-surface space-y-4 rounded-2xl border p-4 shadow-sm">
	<div class="flex flex-wrap items-start justify-between gap-3">
		<div class="min-w-0">
			<p class="text-[0.65rem] uppercase tracking-wide app-muted">Topology detail</p>
			<h3 class="mt-1 truncate text-sm font-semibold">{aliases[selection.id] || canonicalName}</h3>
		</div>
		<span class="app-subtle-surface rounded-full border px-2 py-1 text-[0.65rem] app-muted">
			{selection.kind}
		</span>
	</div>

	<div class="grid gap-3 sm:grid-cols-2">
		<div class="app-subtle-surface rounded-xl border p-3 text-[0.7rem]">
			<p class="app-muted">Canonical ID</p>
			<p class="mt-1 break-all font-mono">{selection.id}</p>
		</div>
		<div class="app-subtle-surface rounded-xl border p-3 text-[0.7rem]">
			<p class="app-muted">Canonical name</p>
			<p class="mt-1 font-medium">{canonicalName}</p>
		</div>
	</div>

	{#if client && (client.sessionName || client.model || client.workspaceName)}
		<div class="app-surface rounded-xl border p-3">
			<p class="text-[0.68rem] font-medium">ChatGPT session identity</p>
			<dl class="mt-3 grid gap-3 text-[0.7rem] sm:grid-cols-3">
				<div>
					<dt class="app-muted">Session</dt>
					<dd class="mt-1 break-words font-medium">{client.sessionName ?? 'Unavailable'}</dd>
				</div>
				<div>
					<dt class="app-muted">Model</dt>
					<dd class="mt-1 break-words font-medium">{client.model ?? 'Unavailable'}</dd>
				</div>
				<div>
					<dt class="app-muted">Workspace</dt>
					<dd class="mt-1 break-words font-medium">{client.workspaceName ?? 'Unavailable'}</dd>
				</div>
			</dl>
		</div>
	{/if}

	<div class="app-surface rounded-xl border p-3">
		<label for="topology-alias" class="text-[0.68rem] font-medium">Display alias</label>
		<div class="mt-2 flex flex-col gap-2 sm:flex-row">
			<input
				id="topology-alias"
				bind:value={draft}
				maxlength="80"
				placeholder={canonicalName}
				class="app-subtle-surface min-h-11 min-w-0 flex-1 rounded-lg border px-3 text-sm outline-none focus:ring-2 focus:ring-[var(--app-focus-ring)]"
			/>
			<div class="grid grid-cols-2 gap-2 sm:flex">
				<button
					type="button"
					disabled={submitting}
					class="app-interactive min-h-11 rounded-lg px-4 text-xs font-medium app-accent-surface disabled:opacity-50"
					onclick={() => void saveAlias()}>Save</button
				>
				<button
					type="button"
					disabled={submitting}
					class="app-interactive min-h-11 rounded-lg px-4 text-xs app-muted disabled:opacity-50"
					onclick={() => void resetAlias()}>Reset to default</button
				>
			</div>
		</div>
		{#if error}<p class="mt-2 text-[0.68rem] text-red-400">{error}</p>{/if}
	</div>

	{#if latency}
		<div class="app-surface rounded-xl border p-3">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<div>
					<p class="text-xs font-semibold">{metricLabel(latency.metricType)}</p>
					<p class="mt-0.5 text-[0.65rem] app-muted">
						Measured at the controlled boundary shown by this edge.
					</p>
				</div>
				<span
					class="rounded-full px-2 py-1 text-[0.65rem] {latency.health === 'error'
						? 'bg-red-500/10 text-red-400'
						: latency.health === 'degraded'
							? 'bg-amber-500/10 text-amber-400'
							: 'bg-emerald-500/10 text-emerald-400'}">{latency.health}</span
				>
			</div>
			<dl class="mt-3 grid grid-cols-3 gap-2 text-[0.7rem] sm:grid-cols-6">
				<div>
					<dt class="app-muted">Latest</dt>
					<dd class="mt-1 font-semibold tabular-nums">{latency.latestMs} ms</dd>
				</div>
				<div>
					<dt class="app-muted">Average</dt>
					<dd class="mt-1 tabular-nums">{latency.averageMs.toFixed(1)} ms</dd>
				</div>
				<div>
					<dt class="app-muted">P50</dt>
					<dd class="mt-1 tabular-nums">{latency.p50Ms} ms</dd>
				</div>
				<div>
					<dt class="app-muted">P95</dt>
					<dd class="mt-1 tabular-nums">{latency.p95Ms} ms</dd>
				</div>
				<div>
					<dt class="app-muted">Max</dt>
					<dd class="mt-1 tabular-nums">{latency.maxMs} ms</dd>
				</div>
				<div>
					<dt class="app-muted">Samples</dt>
					<dd class="mt-1 tabular-nums">{latency.sampleCount}</dd>
				</div>
			</dl>
		</div>
	{/if}

	{#if client}
		<div
			class="app-subtle-surface grid grid-cols-2 gap-3 rounded-xl border p-3 text-[0.7rem] sm:grid-cols-5"
		>
			<div>
				<p class="app-muted">Sessions</p>
				<p class="mt-1 font-semibold">{client.activeSessions}</p>
			</div>
			<div>
				<p class="app-muted">Active</p>
				<p class="mt-1 font-semibold">{client.activeRequests}</p>
			</div>
			<div>
				<p class="app-muted">Requests</p>
				<p class="mt-1 font-semibold">{client.totalRequests}</p>
			</div>
			<div>
				<p class="app-muted">Errors</p>
				<p class="mt-1 font-semibold">{client.errors}</p>
			</div>
			<div class="col-span-2 sm:col-span-1">
				<p class="app-muted">Last tool</p>
				<p class="mt-1 truncate font-mono">{client.lastTool ?? '—'}</p>
			</div>
		</div>
	{/if}

	{#if failures.length > 0}
		<div class="app-subtle-surface rounded-xl border p-3">
			<p class="text-xs font-semibold">Recent correlated failures</p>
			<div class="mt-2 space-y-2">
				{#each failures.slice(-5).reverse() as failure (failure.diagnosticId)}
					<div class="app-surface rounded-lg border px-3 py-2 text-[0.68rem]">
						<div class="flex items-center justify-between gap-2">
							<span class="font-mono text-red-400">{failure.stage}</span>
							<span class="app-muted">{failure.httpStatus ?? failure.errorCode}</span>
						</div>
						<p class="mt-1 leading-5">{failure.summary}</p>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	{#if selection.kind === 'node' && selection.id === 'cptr-backend'}
		<McpBackendMonitor history={systemHistory} {streamHealth} />
	{/if}
</section>
