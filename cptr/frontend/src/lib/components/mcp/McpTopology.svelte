<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getMcpTrafficSnapshot,
		openMcpTrafficStream,
		type McpTrafficEvent,
		type McpTrafficSnapshot
	} from '$lib/apis/mcp';
	import {
		applyMcpTrafficEvent,
		hydrateMcpTraffic,
		recentRequestRows,
		topologyNodes,
		type McpTrafficState
	} from '$lib/stores/mcp-traffic';
	import McpTopologyGraph from './McpTopologyGraph.svelte';
	import McpRecentRequests from './McpRecentRequests.svelte';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';

	const reconnectBackoffMs = [1000, 2000, 4000, 8000];
	const pulseDurationMs = 900;
	const errorDurationMs = 1200;

	let state = $state<McpTrafficState | null>(null);
	let status = $state<StreamStatus>('loading');
	let selectedClientId = $state<string | null>(null);
	let pulseClientIds = $state<Set<string>>(new Set());
	let errorClientIds = $state<Set<string>>(new Set());
	let reconnectAttempt = 0;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let closeStream: (() => void) | null = null;
	let destroyed = false;
	const pulseTimers = new Map<string, ReturnType<typeof setTimeout>>();
	const errorTimers = new Map<string, ReturnType<typeof setTimeout>>();

	const nodes = $derived(state ? topologyNodes(state) : []);
	const rows = $derived(state ? recentRequestRows(state) : []);
	const selectedClient = $derived(
		state && selectedClientId ? (state.clients[selectedClientId] ?? null) : null
	);

	function replaceSet(source: Set<string>, value: string, enabled: boolean): Set<string> {
		const next = new Set(source);
		if (enabled) next.add(value);
		else next.delete(value);
		return next;
	}

	function armClientPulse(clientId: string) {
		const prior = pulseTimers.get(clientId);
		if (prior) clearTimeout(prior);
		pulseClientIds = replaceSet(pulseClientIds, clientId, true);
		pulseTimers.set(
			clientId,
			setTimeout(() => {
				pulseTimers.delete(clientId);
				pulseClientIds = replaceSet(pulseClientIds, clientId, false);
			}, pulseDurationMs)
		);
	}

	function armClientError(clientId: string) {
		const prior = errorTimers.get(clientId);
		if (prior) clearTimeout(prior);
		errorClientIds = replaceSet(errorClientIds, clientId, true);
		errorTimers.set(
			clientId,
			setTimeout(() => {
				errorTimers.delete(clientId);
				errorClientIds = replaceSet(errorClientIds, clientId, false);
			}, errorDurationMs)
		);
	}

	function applySnapshot(snapshot: McpTrafficSnapshot) {
		state = hydrateMcpTraffic(snapshot);
		if (selectedClientId && !state.clients[selectedClientId]) selectedClientId = null;
	}

	function applyEvent(event: McpTrafficEvent) {
		if (!state) return;
		state = applyMcpTrafficEvent(state, event);
		if (
			event.event_type === 'request_started' ||
			event.event_type === 'request_finished' ||
			event.event_type === 'request_failed' ||
			event.event_type === 'tool_started' ||
			event.event_type === 'tool_finished' ||
			event.event_type === 'tool_failed'
		) {
			armClientPulse(event.client.id);
		}
		if (event.event_type === 'request_failed' || event.event_type === 'tool_failed') {
			armClientError(event.client.id);
		}
	}

	function stopStream() {
		closeStream?.();
		closeStream = null;
	}

	function clearReconnectTimer() {
		if (reconnectTimer) clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}

	async function refreshAndOpen() {
		if (destroyed) return;
		clearReconnectTimer();
		stopStream();
		try {
			applySnapshot(await getMcpTrafficSnapshot());
			if (destroyed) return;
			closeStream = openMcpTrafficStream({
				onSnapshot(snapshot) {
					applySnapshot(snapshot);
				},
				onTraffic(event) {
					applyEvent(event);
				},
				onOpen() {
					reconnectAttempt = 0;
					status = 'live';
				},
				onError() {
					scheduleReconnect();
				}
			});
		} catch {
			scheduleReconnect();
		}
	}

	function scheduleReconnect() {
		if (destroyed || reconnectTimer) return;
		stopStream();
		status = 'reconnecting';
		const delay = reconnectBackoffMs[Math.min(reconnectAttempt, reconnectBackoffMs.length - 1)];
		reconnectAttempt += 1;
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			void refreshAndOpen();
		}, delay);
	}

	onMount(() => {
		void refreshAndOpen();
	});

	onDestroy(() => {
		destroyed = true;
		stopStream();
		clearReconnectTimer();
		for (const timer of pulseTimers.values()) clearTimeout(timer);
		for (const timer of errorTimers.values()) clearTimeout(timer);
		pulseTimers.clear();
		errorTimers.clear();
	});
</script>

<div
	class="flex h-full min-h-0 flex-col overflow-auto bg-gray-50/70 p-3 dark:bg-gray-950/60 sm:p-4"
>
	<div class="mx-auto flex w-full max-w-[100rem] flex-1 flex-col gap-3 sm:gap-4">
		<div
			class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-gray-200/80 bg-white/80 px-4 py-3 shadow-sm dark:border-white/10 dark:bg-gray-950/75"
		>
			<div>
				<div class="flex items-center gap-2">
					<h2 class="text-sm font-semibold text-gray-800 dark:text-gray-100">
						MCP traffic topology
					</h2>
					<span
						class="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[0.65rem] font-medium {status ===
						'live'
							? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'
							: status === 'reconnecting'
								? 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-300'
								: 'bg-gray-100 text-gray-500 dark:bg-white/7 dark:text-gray-400'}"
					>
						<span
							class="size-1.5 rounded-full {status === 'live'
								? 'bg-emerald-500'
								: status === 'reconnecting'
									? 'animate-pulse bg-amber-500'
									: 'bg-gray-400'}"
						></span>
						{status}
					</span>
				</div>
				<p class="mt-1 text-[0.7rem] text-gray-400 dark:text-gray-500">
					Real inbound MCP requests animate from connected clients into CPTR MCP.
				</p>
			</div>
			<div
				class="flex items-center gap-4 text-[0.7rem] tabular-nums text-gray-500 dark:text-gray-400"
			>
				<div>
					<span class="font-semibold text-gray-700 dark:text-gray-200">{nodes.length}</span> clients
				</div>
				<div>
					<span class="font-semibold text-gray-700 dark:text-gray-200"
						>{state ? Object.keys(state.activeRequests).length : 0}</span
					> active
				</div>
			</div>
		</div>

		<div
			class="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.45fr)_minmax(22rem,0.85fr)] lg:gap-4"
		>
			<div class="flex min-h-0 flex-col gap-3">
				<McpTopologyGraph
					{nodes}
					{selectedClientId}
					{pulseClientIds}
					{errorClientIds}
					onselect={(clientId) =>
						(selectedClientId = selectedClientId === clientId ? null : clientId)}
				/>

				{#if selectedClient}
					<div
						class="grid grid-cols-2 gap-3 rounded-2xl border border-gray-200/80 bg-white/80 p-4 text-xs shadow-sm dark:border-white/10 dark:bg-gray-950/75 sm:grid-cols-5"
					>
						<div class="col-span-2 sm:col-span-1">
							<p class="text-[0.65rem] uppercase tracking-wide text-gray-400">Client</p>
							<p class="mt-1 truncate font-semibold text-gray-700 dark:text-gray-200">
								{selectedClient.label}
							</p>
						</div>
						<div>
							<p class="text-[0.65rem] uppercase tracking-wide text-gray-400">Sessions</p>
							<p class="mt-1 font-semibold text-gray-700 dark:text-gray-200">
								{selectedClient.activeSessions}
							</p>
						</div>
						<div>
							<p class="text-[0.65rem] uppercase tracking-wide text-gray-400">Active</p>
							<p class="mt-1 font-semibold text-gray-700 dark:text-gray-200">
								{selectedClient.activeRequests}
							</p>
						</div>
						<div>
							<p class="text-[0.65rem] uppercase tracking-wide text-gray-400">Requests</p>
							<p class="mt-1 font-semibold text-gray-700 dark:text-gray-200">
								{selectedClient.totalRequests}
							</p>
						</div>
						<div>
							<p class="text-[0.65rem] uppercase tracking-wide text-gray-400">Errors</p>
							<p class="mt-1 font-semibold text-gray-700 dark:text-gray-200">
								{selectedClient.errors}
							</p>
						</div>
					</div>
				{/if}
			</div>

			<McpRecentRequests {rows} {selectedClientId} />
		</div>
	</div>
</div>
