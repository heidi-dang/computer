<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getMcpDiagnosticsSnapshot,
		getMcpTopologyConfig,
		getMcpTrafficSnapshot,
		openMcpDiagnosticsStream,
		openMcpTrafficStream,
		type McpDiagnosticsEvent,
		type McpTopologyConfig,
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
	import {
		applyMcpDiagnosticsEvent,
		hydrateMcpDiagnostics,
		type McpDiagnosticsState,
		type McpFailureState,
		type McpLatencySummaryState
	} from '$lib/stores/mcp-diagnostics';
	import {
		displayTopologyLabel,
		hydrateMcpTopologyConfig,
		type McpTopologyConfigState,
		type McpTopologySelection
	} from '$lib/stores/mcp-topology';
	import McpTopologyGraph from './McpTopologyGraph.svelte';
	import McpTopologyDetail from './McpTopologyDetail.svelte';
	import McpRecentRequests from './McpRecentRequests.svelte';
	import McpRequestChart from './McpRequestChart.svelte';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';
	type Props = {
		onrevealactivity?: (requestId: string | null, correlationId: string | null) => void;
	};

	let { onrevealactivity }: Props = $props();

	const reconnectBackoffMs = [1000, 2000, 4000, 8000];
	const pulseDurationMs = 900;
	const errorDurationMs = 1200;
	const canonicalInfrastructure: Record<string, string> = {
		'mcp-connector': 'MCP Connector',
		'cptr-mcp': 'CPTR MCP',
		'cptr-backend': 'CPTR Backend'
	};
	const canonicalEdges: Record<string, string> = {
		'client-mcp-connector': 'Observed request time',
		'mcp-connector-cptr-mcp': 'Adapter handoff',
		'cptr-mcp-cptr-backend': 'Backend API RTT'
	};

	let traffic = $state<McpTrafficState | null>(null);
	let diagnostics = $state<McpDiagnosticsState | null>(null);
	let topologyConfig = $state<McpTopologyConfigState | null>(null);
	let trafficStatus = $state<StreamStatus>('loading');
	let diagnosticsStatus = $state<StreamStatus>('loading');
	let selection = $state<McpTopologySelection>(null);
	let pulseClientIds = $state<Set<string>>(new Set());
	let errorClientIds = $state<Set<string>>(new Set());
	let trafficReconnectAttempt = 0;
	let diagnosticsReconnectAttempt = 0;
	let trafficReconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let diagnosticsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let closeTrafficStream: (() => void) | null = null;
	let closeDiagnosticsStream: (() => void) | null = null;
	let destroyed = false;
	const pulseTimers = new Map<string, ReturnType<typeof setTimeout>>();
	const errorTimers = new Map<string, ReturnType<typeof setTimeout>>();

	const nodes = $derived(traffic ? topologyNodes(traffic) : []);
	const rows = $derived(traffic ? recentRequestRows(traffic) : []);
	const aliases = $derived(topologyConfig?.aliases ?? {});
	const selectedClientId = $derived(selection?.kind === 'client' ? selection.id : null);
	const selectedClient = $derived(
		traffic && selectedClientId ? (traffic.clients[selectedClientId] ?? null) : null
	);
	const selectedCanonicalName = $derived(canonicalName(selection));
	const selectedLatency = $derived(latencyFor(selection));
	const selectedFailures = $derived(failuresFor(selection));

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

	function applyTrafficSnapshot(snapshot: McpTrafficSnapshot) {
		traffic = hydrateMcpTraffic(snapshot);
		if (selection?.kind === 'client' && !traffic.clients[selection.id]) selection = null;
	}

	function applyTrafficEvent(event: McpTrafficEvent) {
		if (!traffic) return;
		traffic = applyMcpTrafficEvent(traffic, event);
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

	function applyDiagnosticEvent(event: McpDiagnosticsEvent) {
		if (!diagnostics) return;
		diagnostics = applyMcpDiagnosticsEvent(diagnostics, event);
	}

	function stopTrafficStream() {
		closeTrafficStream?.();
		closeTrafficStream = null;
	}

	function stopDiagnosticsStream() {
		closeDiagnosticsStream?.();
		closeDiagnosticsStream = null;
	}

	function clearTrafficReconnect() {
		if (trafficReconnectTimer) clearTimeout(trafficReconnectTimer);
		trafficReconnectTimer = null;
	}

	function clearDiagnosticsReconnect() {
		if (diagnosticsReconnectTimer) clearTimeout(diagnosticsReconnectTimer);
		diagnosticsReconnectTimer = null;
	}

	async function loadTopologyConfig() {
		try {
			topologyConfig = hydrateMcpTopologyConfig(await getMcpTopologyConfig());
		} catch {
			// Aliases are an optional projection; canonical topology remains usable.
		}
	}

	async function refreshTrafficAndOpen() {
		if (destroyed) return;
		clearTrafficReconnect();
		stopTrafficStream();
		try {
			applyTrafficSnapshot(await getMcpTrafficSnapshot());
			if (destroyed) return;
			closeTrafficStream = openMcpTrafficStream({
				onSnapshot: applyTrafficSnapshot,
				onTraffic: applyTrafficEvent,
				onOpen() {
					trafficReconnectAttempt = 0;
					trafficStatus = 'live';
				},
				onError() {
					scheduleTrafficReconnect();
				}
			});
		} catch {
			scheduleTrafficReconnect();
		}
	}

	async function refreshDiagnosticsAndOpen() {
		if (destroyed) return;
		clearDiagnosticsReconnect();
		stopDiagnosticsStream();
		try {
			diagnostics = hydrateMcpDiagnostics(await getMcpDiagnosticsSnapshot());
			if (destroyed) return;
			closeDiagnosticsStream = openMcpDiagnosticsStream({
				onSnapshot(snapshot) {
					diagnostics = hydrateMcpDiagnostics(snapshot);
				},
				onLatency: applyDiagnosticEvent,
				onFailure: applyDiagnosticEvent,
				onSystem: applyDiagnosticEvent,
				onOpen() {
					diagnosticsReconnectAttempt = 0;
					diagnosticsStatus = 'live';
				},
				onError() {
					scheduleDiagnosticsReconnect();
				}
			});
		} catch {
			diagnosticsStatus = 'error';
			scheduleDiagnosticsReconnect();
		}
	}

	function scheduleTrafficReconnect() {
		if (destroyed || trafficReconnectTimer) return;
		stopTrafficStream();
		trafficStatus = 'reconnecting';
		const delay =
			reconnectBackoffMs[Math.min(trafficReconnectAttempt, reconnectBackoffMs.length - 1)];
		trafficReconnectAttempt += 1;
		trafficReconnectTimer = setTimeout(() => {
			trafficReconnectTimer = null;
			void refreshTrafficAndOpen();
		}, delay);
	}

	function scheduleDiagnosticsReconnect() {
		if (destroyed || diagnosticsReconnectTimer) return;
		stopDiagnosticsStream();
		diagnosticsStatus = 'reconnecting';
		const delay =
			reconnectBackoffMs[Math.min(diagnosticsReconnectAttempt, reconnectBackoffMs.length - 1)];
		diagnosticsReconnectAttempt += 1;
		diagnosticsReconnectTimer = setTimeout(() => {
			diagnosticsReconnectTimer = null;
			void refreshDiagnosticsAndOpen();
		}, delay);
	}

	function choose(next: NonNullable<McpTopologySelection>) {
		selection = selection?.kind === next.kind && selection.id === next.id ? null : next;
	}

	function handleConfig(config: McpTopologyConfig) {
		topologyConfig = hydrateMcpTopologyConfig(config);
	}

	function canonicalName(current: McpTopologySelection): string {
		if (!current) return '';
		if (current.kind === 'client') return traffic?.clients[current.id]?.label ?? current.id;
		if (current.kind === 'edge') return canonicalEdges[current.id] ?? current.id;
		return (
			topologyConfig?.canonicalLabels[current.id] ??
			canonicalInfrastructure[current.id] ??
			current.id
		);
	}

	function latencyFor(current: McpTopologySelection): McpLatencySummaryState | null {
		if (!current || !diagnostics) return null;
		if (current.kind === 'edge') {
			return diagnostics.latency[current.id as keyof typeof diagnostics.latency] ?? null;
		}
		if (current.kind === 'client') return diagnostics.latency['client-mcp-connector'] ?? null;
		if (current.id === 'mcp-connector')
			return diagnostics.latency['mcp-connector-cptr-mcp'] ?? null;
		if (current.id === 'cptr-mcp') return diagnostics.latency['mcp-connector-cptr-mcp'] ?? null;
		if (current.id === 'cptr-backend') return diagnostics.latency['cptr-mcp-cptr-backend'] ?? null;
		return null;
	}

	function failuresFor(current: McpTopologySelection): McpFailureState[] {
		if (!current || !diagnostics) return [];
		if (current.kind === 'client') {
			return diagnostics.failures.filter((failure) => failure.clientId === current.id);
		}
		const stagesById: Record<string, string[]> = {
			'mcp-connector': [
				'client_transport',
				'mcp_connector',
				'traffic_delivery',
				'activity_delivery'
			],
			'cptr-mcp': ['cptr_mcp'],
			'cptr-backend': ['cptr_backend'],
			'client-mcp-connector': ['client_transport', 'mcp_connector'],
			'mcp-connector-cptr-mcp': ['mcp_connector', 'cptr_mcp'],
			'cptr-mcp-cptr-backend': ['cptr_backend']
		};
		const stages = stagesById[current.id] ?? [];
		return diagnostics.failures.filter((failure) => stages.includes(failure.stage));
	}

	onMount(() => {
		void loadTopologyConfig();
		void refreshTrafficAndOpen();
		void refreshDiagnosticsAndOpen();
	});

	onDestroy(() => {
		destroyed = true;
		stopTrafficStream();
		stopDiagnosticsStream();
		clearTrafficReconnect();
		clearDiagnosticsReconnect();
		for (const timer of pulseTimers.values()) clearTimeout(timer);
		for (const timer of errorTimers.values()) clearTimeout(timer);
		pulseTimers.clear();
		errorTimers.clear();
	});
</script>

<div class="app-theme flex h-full min-h-0 flex-col overflow-auto p-3 sm:p-4">
	<div class="mx-auto flex w-full max-w-[100rem] flex-1 flex-col gap-3 sm:gap-4">
		<div
			class="app-raised-surface flex flex-wrap items-center justify-between gap-3 rounded-2xl border px-4 py-3 shadow-sm"
		>
			<div>
				<div class="flex flex-wrap items-center gap-2">
					<h2 class="text-sm font-semibold">MCP traffic topology</h2>
					<span
						class="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[0.65rem] font-medium {trafficStatus ===
						'live'
							? 'bg-emerald-500/10 text-emerald-400'
							: trafficStatus === 'reconnecting'
								? 'bg-amber-500/10 text-amber-400'
								: 'app-subtle-surface app-muted'}"
					>
						<span
							class="size-1.5 rounded-full {trafficStatus === 'live'
								? 'bg-emerald-500'
								: trafficStatus === 'reconnecting'
									? 'animate-pulse bg-amber-500'
									: 'bg-current opacity-60'}"
						></span>
						traffic {trafficStatus}
					</span>
					<span
						class="inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[0.65rem] app-subtle-surface app-muted"
					>
						<span
							class="size-1.5 rounded-full {diagnosticsStatus === 'live'
								? 'bg-emerald-500'
								: diagnosticsStatus === 'reconnecting'
									? 'animate-pulse bg-amber-500'
									: 'bg-gray-500'}"
						></span>
						diagnostics {diagnosticsStatus}
					</span>
				</div>
				<p class="mt-1 text-[0.7rem] app-muted">
					Real inbound MCP requests animate through MCP Connector → CPTR MCP → CPTR Backend.
				</p>
			</div>
			<div class="flex items-center gap-4 text-[0.7rem] tabular-nums app-muted">
				<div><span class="font-semibold">{nodes.length}</span> clients</div>
				<div>
					<span class="font-semibold"
						>{traffic ? Object.keys(traffic.activeRequests).length : 0}</span
					> active
				</div>
			</div>
		</div>

		<McpRequestChart state={traffic} />

		<div
			class="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.45fr)_minmax(22rem,0.85fr)] lg:gap-4"
		>
			<div class="flex min-h-0 flex-col gap-3">
				<McpTopologyGraph
					{nodes}
					{selection}
					{aliases}
					latency={diagnostics?.latency ?? {}}
					{pulseClientIds}
					{errorClientIds}
					onselect={choose}
				/>

				{#if selection}
					<McpTopologyDetail
						{selection}
						canonicalName={selectedCanonicalName}
						{aliases}
						latency={selectedLatency}
						client={selectedClient}
						systemHistory={diagnostics?.system ?? []}
						failures={selectedFailures}
						streamHealth={diagnostics?.streamHealth ?? null}
						onconfig={handleConfig}
					/>
				{/if}
			</div>

			<McpRecentRequests
				{rows}
				{selectedClientId}
				failures={diagnostics?.failures ?? []}
				{onrevealactivity}
			/>
		</div>
	</div>
</div>
