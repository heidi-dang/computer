<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { getMcpActivitySnapshot, openMcpActivityStream } from '$lib/apis/mcp';
	import {
		applyMcpActivityEvent,
		hydrateMcpActivity,
		type McpActivityRow,
		type McpActivityState
	} from '$lib/stores/mcp-activity';
	import McpCallCard from './McpCallCard.svelte';

	type StreamStatus = 'loading' | 'live' | 'reconnecting';
	type Props = {
		consoleRows?: McpActivityRow[];
		focusRequestId?: string | null;
		focusCorrelationId?: string | null;
		onClearConsole?: () => void;
	};

	let {
		consoleRows = [],
		focusRequestId = null,
		focusCorrelationId = null,
		onClearConsole
	}: Props = $props();
	let state = $state<McpActivityState | null>(null);
	let status = $state<StreamStatus>('loading');
	let hiddenBeforeSequence = $state(0);
	let reconnectAttempt = 0;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let closeStream: (() => void) | null = null;
	let destroyed = false;
	let autoFollow = true;
	let scrollEl: HTMLDivElement;
	const reconnectBackoffMs = [1000, 2000, 4000, 8000];

	const pluginRows = $derived(
		state
			? state.rows.filter((row) => row.source === 'plugin' && row.sequence > hiddenBeforeSequence)
			: []
	);
	const rows = $derived(
		[...pluginRows, ...consoleRows].sort(
			(a, b) => a.startedAt - b.startedAt || a.id.localeCompare(b.id)
		)
	);
	const revision = $derived(rows.map((row) => `${row.id}:${row.phase}:${row.sequence}`).join('|'));
	const focusedRowId = $derived(
		rows.find(
			(row) =>
				(Boolean(focusRequestId) && row.requestId === focusRequestId) ||
				(Boolean(focusCorrelationId) && row.correlationId === focusCorrelationId)
		)?.id ?? null
	);

	$effect(() => {
		revision;
		if (!autoFollow) return;
		void tick().then(() => {
			if (!scrollEl) return;
			scrollEl.scrollTo({ top: scrollEl.scrollHeight, behavior: 'smooth' });
		});
	});

	$effect(() => {
		revision;
		const targetId = focusedRowId;
		if (!targetId || typeof document === 'undefined') return;
		autoFollow = false;
		void tick().then(() => {
			document
				.getElementById(`mcp-activity-${targetId}`)
				?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		});
	});

	function stopStream() {
		closeStream?.();
		closeStream = null;
	}

	function clearReconnectTimer() {
		if (reconnectTimer) clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}

	function applySnapshot(snapshot: Awaited<ReturnType<typeof getMcpActivitySnapshot>>) {
		state = hydrateMcpActivity(snapshot);
		if (snapshot.sequence < hiddenBeforeSequence) hiddenBeforeSequence = 0;
	}

	async function refreshAndOpen(restoreHistory = false) {
		if (destroyed) return;
		if (restoreHistory) hiddenBeforeSequence = 0;
		clearReconnectTimer();
		stopStream();
		try {
			applySnapshot(await getMcpActivitySnapshot());
			if (destroyed) return;
			closeStream = openMcpActivityStream({
				onSnapshot(snapshot) {
					applySnapshot(snapshot);
				},
				onActivity(event) {
					if (state) state = applyMcpActivityEvent(state, event);
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

	function clearPresentation() {
		hiddenBeforeSequence = state?.sequence ?? hiddenBeforeSequence;
		onClearConsole?.();
	}

	function handleScroll() {
		if (!scrollEl) return;
		const distanceFromBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
		autoFollow = distanceFromBottom <= 48;
	}

	onMount(() => {
		void refreshAndOpen();
	});

	onDestroy(() => {
		destroyed = true;
		stopStream();
		clearReconnectTimer();
	});
</script>

<div class="app-theme flex h-full min-h-0 flex-col overflow-hidden">
	<div class="app-surface flex shrink-0 items-center justify-between border-b px-3 py-2.5 sm:px-4">
		<div class="flex min-w-0 items-center gap-2">
			<span class="text-xs font-semibold">Activity</span>
			<span
				class="app-subtle-surface rounded-full border px-2 py-0.5 text-[0.62rem] tabular-nums app-muted"
				>{rows.length}</span
			>
			<span
				class="inline-flex items-center gap-1.5 text-[0.62rem] {status === 'live'
					? 'text-emerald-500'
					: 'text-amber-500'}"
			>
				<span
					class="size-1.5 rounded-full {status === 'live'
						? 'bg-emerald-500'
						: 'animate-pulse bg-amber-500'}"
				></span>
				{status}
			</span>
		</div>
		<div class="flex items-center gap-1">
			<button
				type="button"
				class="app-interactive min-h-11 rounded-lg px-2.5 text-[0.68rem] app-muted sm:min-h-0 sm:py-1.5"
				onclick={() => void refreshAndOpen(true)}>Refresh</button
			>
			<button
				type="button"
				class="app-interactive min-h-11 rounded-lg px-2.5 text-[0.68rem] app-muted sm:min-h-0 sm:py-1.5"
				onclick={clearPresentation}>Clear</button
			>
		</div>
	</div>

	<div
		bind:this={scrollEl}
		onscroll={handleScroll}
		class="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3 sm:px-4 sm:py-4"
	>
		{#if rows.length === 0}
			<div class="flex h-full min-h-48 items-center justify-center px-5 text-center">
				<div>
					<p class="text-sm font-medium">No tool activity yet</p>
					<p class="mt-1 max-w-sm text-xs leading-5 app-muted">
						Real ChatGPT MCP tool calls appear here automatically. Manual downstream tests are
						labeled Console invocation.
					</p>
				</div>
			</div>
		{:else}
			{#each rows as record (record.id)}
				<div
					id={`mcp-activity-${record.id}`}
					class="rounded-xl {focusedRowId === record.id
						? 'app-accent-surface ring-1 ring-[var(--app-focus-ring)]'
						: ''}"
				>
					<McpCallCard {record} />
				</div>
			{/each}
		{/if}
	</div>
</div>
