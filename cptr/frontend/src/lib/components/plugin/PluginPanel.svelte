<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { fetchHandler } from '$lib/apis';
	import {
		archivePluginSession,
		confirmPluginSessionDelete,
		getPluginSession,
		getPluginSessionEvents,
		listPluginSessions,
		pluginSessionStreamUrl,
		pluginTerminalStreamUrl,
		renamePluginSession,
		requestPluginSessionDelete,
		clearPluginWorkspaceMemory,
		forgetPluginWorkspaceMemoryFact,
		getPluginWorkspaceMemoryContext,
		getPluginWorkspaceMemoryFacts,
		updatePluginWorkspaceMemoryFact,
		type PluginTerminalEvent,
		type PluginTerminalSnapshot,
		type WorkbenchSession,
		type WorkbenchSessionEvent,
		type WorkspaceMemoryContext,
		type WorkspaceMemoryFact
	} from '$lib/apis/plugin';
	import {
		appendPluginEvent,
		mergePluginSession,
		pluginConsole,
		replacePluginEvents,
		replacePluginSessions
	} from '$lib/stores/plugin';
	import PluginActivityMessage from './PluginActivityMessage.svelte';
	import PluginLiveTerminal, { type PluginTerminalLine } from './PluginLiveTerminal.svelte';
	import PluginSessionList from './PluginSessionList.svelte';

	let loading = $state(true);
	let error = $state('');
	let busySessionId = $state<string | null>(null);
	let terminalSnapshot = $state<PluginTerminalSnapshot | null>(null);
	let terminalLines = $state<PluginTerminalLine[]>([]);
	let terminalState = $state<'idle' | 'connecting' | 'live' | 'reconnecting' | 'unavailable' | 'error'>('idle');
	let memoryLoading = $state(false);
	let memoryError = $state('');
	let memoryContext = $state<WorkspaceMemoryContext | null>(null);
	let memoryFacts = $state<WorkspaceMemoryFact[]>([]);
	let memoryBusyFactId = $state<string | null>(null);
	let memoryClearing = $state(false);

	const selectedSessionId = $derived($pluginConsole.selectedSessionId);
	const selectedSession = $derived(
		$pluginConsole.sessions.find((session) => session.session_id === selectedSessionId) ?? null
	);
	const selectedEvents = $derived(
		selectedSessionId ? ($pluginConsole.eventsBySession[selectedSessionId] ?? []) : []
	);
	const terminalTargetKey = $derived(
		selectedSession?.active_target_type && selectedSession?.active_target_id
			? `${selectedSession.active_target_type}:${selectedSession.active_workspace_id ?? ''}:${selectedSession.active_target_id}`
			: ''
	);
	const selectedWorkspaceId = $derived(selectedSession?.workspace_id ?? selectedSession?.active_workspace_id ?? null);

	type ParsedSseEvent = { id?: string; type: string; data: string };

	function parseSseBlock(block: string): ParsedSseEvent | null {
		const lines = block.replaceAll('\r', '').split('\n');
		let id: string | undefined;
		let type = 'message';
		const data: string[] = [];
		for (const line of lines) {
			if (!line || line.startsWith(':')) continue;
			const separator = line.indexOf(':');
			const field = separator === -1 ? line : line.slice(0, separator);
			const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '');
			if (field === 'id') id = value;
			else if (field === 'event') type = value || 'message';
			else if (field === 'data') data.push(value);
		}
		return data.length ? { id, type, data: data.join('\n') } : null;
	}

	async function consumeSse(
		url: string,
		signal: AbortSignal,
		onEvent: (event: ParsedSseEvent) => void
	) {
		const response = await fetchHandler(url, {
			headers: { Accept: 'text/event-stream' },
			signal
		});
		if (!response.ok || !response.body) {
			throw new Error(`Live stream unavailable (${response.status})`);
		}
		const reader = response.body.getReader();
		const decoder = new TextDecoder();
		let buffer = '';
		try {
			while (true) {
				const next = await reader.read();
				if (next.done) break;
				buffer += decoder.decode(next.value, { stream: true });
				let boundary = buffer.indexOf('\n\n');
				while (boundary !== -1) {
					const parsed = parseSseBlock(buffer.slice(0, boundary));
					buffer = buffer.slice(boundary + 2);
					if (parsed) onEvent(parsed);
					boundary = buffer.indexOf('\n\n');
				}
			}
		} finally {
			reader.releaseLock();
		}
	}

	function jsonValue<T>(value: string): T | null {
		try {
			return JSON.parse(value) as T;
		} catch {
			return null;
		}
	}

	function terminalText(value: unknown): string | null {
		return typeof value === 'string' && value.length ? value : null;
	}

	function terminalLinesFromEvent(event: PluginTerminalEvent): PluginTerminalLine[] {
		const payload = event.payload ?? {};
		const candidates: [PluginTerminalLine['stream'], unknown][] = [
			['stdout', payload.stdout],
			['stderr', payload.stderr],
			['stdout', payload.output],
			['stdout', payload.text],
			['stdout', payload.chunk]
		];
		const seen = new Set<string>();
		return candidates.flatMap(([stream, value], index) => {
			const text = terminalText(value);
			if (!text || seen.has(text)) return [];
			seen.add(text);
			return [
				{
					sequence: event.sequence,
					text,
					stream,
					at: event.created_at,
					key: `${event.sequence}:${index}:${stream}`
				}
			];
		});
	}

	function mergeTerminalLines(lines: PluginTerminalLine[]) {
		const existing = new Set(terminalLines.map((line) => line.key));
		const additions = lines.filter((line) => !existing.has(line.key));
		if (additions.length) terminalLines = [...terminalLines, ...additions].slice(-360);
	}

	async function reloadSessions() {
		const response = await listPluginSessions();
		pluginConsole.update((state) => replacePluginSessions(state, response.sessions));
	}

	async function reloadSelectedSession(sessionId: string) {
		const [session, response] = await Promise.all([
			getPluginSession(sessionId),
			getPluginSessionEvents(sessionId, 0, 100)
		]);
		pluginConsole.update((state) =>
			replacePluginEvents(mergePluginSession(state, session), sessionId, response.events)
		);
	}

	async function selectSession(sessionId: string) {
		pluginConsole.update((state) => ({ ...state, selectedSessionId: sessionId }));
		try {
			await reloadSelectedSession(sessionId);
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not load this Plugin session');
		}
	}

	async function reloadWorkspaceMemory(workspaceId: string, refresh = false) {
		memoryLoading = true;
		memoryError = '';
		try {
			const [context, facts] = await Promise.all([
				getPluginWorkspaceMemoryContext(workspaceId, refresh),
				getPluginWorkspaceMemoryFacts(workspaceId)
			]);
			memoryContext = context;
			memoryFacts = facts.facts;
		} catch (reason) {
			memoryError = reason instanceof Error ? reason.message : 'Could not load workspace memory';
		} finally {
			memoryLoading = false;
		}
	}

	async function toggleMemoryFactPin(fact: WorkspaceMemoryFact) {
		if (!selectedWorkspaceId) return;
		memoryBusyFactId = fact.fact_id;
		try {
			const updated = await updatePluginWorkspaceMemoryFact(selectedWorkspaceId, fact.fact_id, {
				pinned: !fact.pinned
			});
			memoryFacts = memoryFacts.map((item) => (item.fact_id === updated.fact_id ? updated : item));
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not update workspace memory');
		} finally {
			memoryBusyFactId = null;
		}
	}

	async function editMemoryFact(fact: WorkspaceMemoryFact) {
		if (!selectedWorkspaceId) return;
		const content = window.prompt('Edit workspace memory fact', fact.content)?.trim();
		if (!content || content === fact.content) return;
		memoryBusyFactId = fact.fact_id;
		try {
			const updated = await updatePluginWorkspaceMemoryFact(selectedWorkspaceId, fact.fact_id, { content });
			memoryFacts = memoryFacts.map((item) => (item.fact_id === updated.fact_id ? updated : item));
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not edit workspace memory');
		} finally {
			memoryBusyFactId = null;
		}
	}

	async function forgetMemoryFact(fact: WorkspaceMemoryFact) {
		if (!selectedWorkspaceId || !window.confirm('Forget this workspace memory fact?')) return;
		memoryBusyFactId = fact.fact_id;
		try {
			await forgetPluginWorkspaceMemoryFact(selectedWorkspaceId, fact.fact_id);
			memoryFacts = memoryFacts.filter((item) => item.fact_id !== fact.fact_id);
			toast.success('Workspace memory fact forgotten');
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not forget workspace memory');
		} finally {
			memoryBusyFactId = null;
		}
	}

	async function clearWorkspaceMemory() {
		if (!selectedWorkspaceId) return;
		if (!window.confirm('Permanently clear all CPTR workspace-memory facts and recorded history for this workspace? This cannot be undone.')) return;
		memoryClearing = true;
		try {
			await clearPluginWorkspaceMemory(selectedWorkspaceId);
			memoryContext = null;
			memoryFacts = [];
			toast.success('Workspace memory cleared');
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not clear workspace memory');
		} finally {
			memoryClearing = false;
		}
	}

	async function renameSession(session: WorkbenchSession) {
		const name = window.prompt('Name this Workbench Session', session.name)?.trim();
		if (!name || name === session.name) return;
		busySessionId = session.session_id;
		try {
			const updated = await renamePluginSession(session.session_id, name);
			pluginConsole.update((state) => mergePluginSession(state, updated));
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not rename the session');
		} finally {
			busySessionId = null;
		}
	}

	async function archiveSession(session: WorkbenchSession) {
		busySessionId = session.session_id;
		try {
			const updated = await archivePluginSession(session.session_id);
			pluginConsole.update((state) => mergePluginSession(state, updated));
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not archive the session');
		} finally {
			busySessionId = null;
		}
	}

	async function deleteSession(session: WorkbenchSession) {
		busySessionId = session.session_id;
		try {
			const request = await requestPluginSessionDelete(session.session_id);
			const confirmed = window.confirm(
				`Delete “${session.name}”? Its durable activity history will be removed. This cannot be undone.`
			);
			if (!confirmed) return;
			await confirmPluginSessionDelete(request.confirmation_id);
			pluginConsole.update((state) => {
				const sessions = state.sessions.filter((item) => item.session_id !== session.session_id);
				const { [session.session_id]: _removed, ...eventsBySession } = state.eventsBySession;
				return {
					...state,
					sessions,
					eventsBySession,
					selectedSessionId:
						state.selectedSessionId === session.session_id
							? (sessions[0]?.session_id ?? null)
							: state.selectedSessionId
				};
			});
			toast.success('Workbench Session deleted');
		} catch (reason) {
			toast.error(reason instanceof Error ? reason.message : 'Could not delete the session');
		} finally {
			busySessionId = null;
		}
	}

	$effect(() => {
		if (!selectedSessionId) return;
		const sessionId: string = selectedSessionId;
		const controller = new AbortController();
		let stopped = false;
		async function stream() {
			while (!stopped) {
				const lastSequence = ($pluginConsole.eventsBySession[sessionId] ?? []).at(-1)?.sequence ?? 0;
				try {
					await consumeSse(pluginSessionStreamUrl(sessionId, lastSequence), controller.signal, (raw) => {
						const event = jsonValue<WorkbenchSessionEvent>(raw.data);
						if (!event || event.session_id !== sessionId) return;
						pluginConsole.update((state) => appendPluginEvent(state, event));
						if (event.event_type === 'workbench.target.bound' || event.event_type === 'session.renamed') {
							void getPluginSession(sessionId).then((session) =>
								pluginConsole.update((state) => mergePluginSession(state, session))
							);
						}
					});
				} catch (reason) {
					if (controller.signal.aborted) return;
					console.warn('Plugin session stream disconnected', reason);
				}
				if (!controller.signal.aborted) await new Promise((resolve) => setTimeout(resolve, 1800));
			}
		}
		void stream();
		return () => {
			stopped = true;
			controller.abort();
		};
	});

	$effect(() => {
		const target = terminalTargetKey;
		terminalSnapshot = null;
		terminalLines = [];
		if (!selectedSessionId || !target) {
			terminalState = 'unavailable';
			return;
		}
		const sessionId: string = selectedSessionId;
		const controller = new AbortController();
		let stopped = false;
		let lastSequence = 0;
		async function stream() {
			while (!stopped) {
				terminalState = lastSequence ? 'reconnecting' : 'connecting';
				try {
					await consumeSse(pluginTerminalStreamUrl(sessionId, lastSequence), controller.signal, (raw) => {
						if (raw.type === 'snapshot') {
							const snapshot = jsonValue<PluginTerminalSnapshot>(raw.data);
							if (snapshot) terminalSnapshot = snapshot;
							return;
						}
						const event = jsonValue<PluginTerminalEvent>(raw.data);
						if (!event || typeof event.sequence !== 'number') return;
						lastSequence = Math.max(lastSequence, event.sequence);
						mergeTerminalLines(terminalLinesFromEvent(event));
						terminalState = 'live';
					});
				} catch (reason) {
					if (controller.signal.aborted) return;
					console.warn('Plugin terminal stream disconnected', reason);
					terminalState = terminalSnapshot ? 'reconnecting' : 'error';
				}
				if (!controller.signal.aborted) await new Promise((resolve) => setTimeout(resolve, 1800));
			}
		}
		void stream();
		return () => {
			stopped = true;
			controller.abort();
		};
	});

			$effect(() => {
			const workspaceId = selectedWorkspaceId;
			memoryContext = null;
			memoryFacts = [];
			memoryError = '';
			if (!workspaceId) return;
			void reloadWorkspaceMemory(workspaceId);
		});

		onMount(() => {

		let mounted = true;
		void (async () => {
			try {
				await reloadSessions();
				const sessionId = $pluginConsole.selectedSessionId;
				if (sessionId) await reloadSelectedSession(sessionId);
			} catch (reason) {
				if (mounted) error = reason instanceof Error ? reason.message : 'Could not load Plugin sessions';
			} finally {
				if (mounted) loading = false;
			}
		})();
		return () => {
			mounted = false;
		};
	});
</script>

<section class="plugin-panel" aria-label="CPTR Plugin activity">
	<PluginSessionList
		sessions={$pluginConsole.sessions}
		{selectedSessionId}
		{busySessionId}
		onselect={selectSession}
		onrename={renameSession}
		onarchive={archiveSession}
		requestDelete={deleteSession}
	/>
	<div class="plugin-console">
		<header class="plugin-console-header">
			<div>
				<h1>{selectedSession?.name ?? 'CPTR Plugin'}</h1>
				<p>
					{selectedSession
						? `${selectedSession.session_id} · CPTR-owned, redacted activity`
						: 'Open a Workbench Session from ChatGPT to view its CPTR activity here.'}
				</p>
			</div>
			<button type="button" onclick={() => void reloadSessions()} disabled={loading}>Refresh</button>
		</header>

		{#if loading}
			<div class="plugin-notice">Loading Plugin Workbench Sessions…</div>
		{:else if error}
			<div class="plugin-notice error">{error}</div>
		{:else}
			<div class="plugin-console-body">
									<PluginLiveTerminal snapshot={terminalSnapshot} lines={terminalLines} connectionState={terminalState} />
					<section class="plugin-memory" aria-label="Workspace memory">
						<div class="plugin-memory-heading">
							<div>
								<h2>Workspace Memory</h2>
								<p>Redacted, owner-scoped CPTR facts and current stage. Private prompts, reasoning, and raw terminal output are not stored.</p>
							</div>
							{#if selectedWorkspaceId}
								<div class="plugin-memory-actions">
									<button type="button" onclick={() => void reloadWorkspaceMemory(selectedWorkspaceId, true)} disabled={memoryLoading}>Verify freshness</button>
									<button type="button" class="danger" onclick={() => void clearWorkspaceMemory()} disabled={memoryClearing}>Clear memory</button>
								</div>
							{/if}
						</div>
						{#if !selectedWorkspaceId}
							<p class="plugin-notice">Bind this Workbench Session to a workspace to view its durable memory.</p>
						{:else if memoryLoading}
							<p class="plugin-notice">Loading workspace memory…</p>
						{:else if memoryError}
							<p class="plugin-notice error">{memoryError}</p>
						{:else}
							<div class="plugin-memory-status">
								<span>Cursor {memoryContext?.memory_cursor ?? 0}</span>
								<span class:stale={memoryContext?.freshness['matches_current_workspace_fingerprint'] === false}>
									{memoryContext?.freshness['matches_current_workspace_fingerprint'] === false ? 'Needs revalidation' : 'Current memory available'}
								</span>
							</div>
							{#if memoryContext?.workspace_stage['last_completed'] || memoryContext?.workspace_stage['active_goal']}
								<div class="plugin-memory-stage">
									{#if memoryContext?.workspace_stage['active_goal']}<p><strong>Goal:</strong> {String(memoryContext.workspace_stage['active_goal'])}</p>{/if}
									{#if memoryContext?.workspace_stage['last_completed']}<p><strong>Last completed:</strong> {String(memoryContext.workspace_stage['last_completed'])}</p>{/if}
								</div>
							{/if}
							{#if memoryFacts.length === 0}
								<p class="plugin-notice">No durable facts have been recorded yet. ChatGPT can use the workspace context tool and save only user-directed, verified facts.</p>
							{:else}
								<div class="plugin-memory-facts">
									{#each memoryFacts as fact (fact.fact_id)}
										<article class:stale={fact.status === 'STALE'}>
											<div class="plugin-memory-fact-meta"><span>{fact.category}</span><span>{fact.status.toLowerCase()}</span>{#if fact.pinned}<span>pinned</span>{/if}</div>
											<p>{fact.content}</p>
											<div class="plugin-memory-fact-actions">
												<button type="button" onclick={() => void toggleMemoryFactPin(fact)} disabled={memoryBusyFactId === fact.fact_id}>{fact.pinned ? 'Unpin' : 'Pin'}</button>
												<button type="button" onclick={() => void editMemoryFact(fact)} disabled={memoryBusyFactId === fact.fact_id}>Edit</button>
												<button type="button" class="danger" onclick={() => void forgetMemoryFact(fact)} disabled={memoryBusyFactId === fact.fact_id}>Forget</button>
											</div>
										</article>
									{/each}
								</div>
							{/if}
						{/if}
					</section>
					<section class="plugin-activity" aria-label="Plugin activity timeline">

					<div class="plugin-activity-heading">
						<h2>Activity</h2>
						<span>{selectedEvents.length ? `${selectedEvents.length} recent events` : 'Awaiting durable events'}</span>
					</div>
					{#if !selectedSession}
						<p class="plugin-notice">Select a session to review its redacted CPTR activity.</p>
					{:else if selectedEvents.length === 0}
						<p class="plugin-notice">This session has no durable activity events yet.</p>
					{:else}
						<div class="plugin-activity-feed">
							{#each selectedEvents as event (`${event.session_id}:${event.sequence}`)}
								<PluginActivityMessage {event} />
							{/each}
						</div>
					{/if}
				</section>
			</div>
		{/if}
	</div>
</section>

<style>
	.plugin-panel {
		display: grid;
		grid-template-columns: minmax(15rem, 21rem) minmax(0, 1fr);
		height: 100%;
		min-height: 0;
		background: var(--bg-primary, #fff);
	}

	.plugin-console {
		display: flex;
		min-width: 0;
		min-height: 0;
		flex-direction: column;
	}

	.plugin-console-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.9rem;
		padding: 0.9rem 1rem;
		border-bottom: 1px solid var(--border-color, #e5e7eb);
	}

	.plugin-console-header h1,
	.plugin-console-header p,
	.plugin-activity-heading h2,
	.plugin-activity-heading span {
		margin: 0;
	}

	.plugin-console-header h1 {
		overflow: hidden;
		font-size: 0.95rem;
		font-weight: 700;
		color: var(--text-primary, #111827);
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.plugin-console-header p,
	.plugin-activity-heading span {
		overflow: hidden;
		max-width: min(100%, 42rem);
		font: 0.68rem/1.35 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: var(--text-secondary, #64748b);
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.plugin-console-header button {
		flex: none;
		padding: 0.34rem 0.52rem;
		border: 1px solid var(--border-color, #cbd5e1);
		border-radius: 0.38rem;
		font-size: 0.72rem;
		color: var(--text-primary, #334155);
		background: transparent;
		cursor: pointer;
	}

	.plugin-console-header button:disabled {
		opacity: 0.55;
		cursor: progress;
	}

	.plugin-console-body {
		display: flex;
		min-height: 0;
		flex: 1;
		flex-direction: column;
		gap: 0.85rem;
		overflow: auto;
		padding: 0.85rem 1rem 1rem;
	}

	.plugin-memory,
	.plugin-activity {
		min-height: 0;
		padding: 0 0.1rem;
	}

	.plugin-memory {
		padding: 0.75rem;
		border: 1px solid var(--border-color, #dbe3ef);
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--bg-secondary, #f8fafc) 70%, transparent);
	}

	.plugin-memory-heading,
	.plugin-memory-actions,
	.plugin-memory-status,
	.plugin-memory-fact-meta,
	.plugin-memory-fact-actions {
		display: flex;
		align-items: center;
		gap: 0.45rem;
	}

	.plugin-memory-heading {
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
	}

	.plugin-memory-heading h2,
	.plugin-memory-heading p,
	.plugin-memory-stage p,
	.plugin-memory-facts p {
		margin: 0;
	}

	.plugin-memory-heading h2 {
		font-size: 0.8rem;
		font-weight: 700;
		color: var(--text-primary, #111827);
	}

	.plugin-memory-heading p {
		max-width: 46rem;
		margin-top: 0.2rem;
		font-size: 0.72rem;
		line-height: 1.35;
		color: var(--text-secondary, #64748b);
	}

	.plugin-memory-actions {
		flex: none;
	}

	.plugin-memory button {
		padding: 0.28rem 0.44rem;
		border: 1px solid var(--border-color, #cbd5e1);
		border-radius: 0.35rem;
		font-size: 0.68rem;
		color: var(--text-primary, #334155);
		background: transparent;
		cursor: pointer;
	}

	.plugin-memory button:disabled {
		opacity: 0.55;
		cursor: progress;
	}

	.plugin-memory button.danger {
		border-color: #fca5a5;
		color: #b91c1c;
	}

	.plugin-memory-status {
		justify-content: space-between;
		margin-top: 0.65rem;
		font: 0.66rem/1.3 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: var(--text-secondary, #64748b);
	}

	.plugin-memory-status .stale,
	.plugin-memory-facts article.stale .plugin-memory-fact-meta {
		color: #b45309;
	}

	.plugin-memory-stage {
		margin-top: 0.55rem;
		padding: 0.5rem 0.6rem;
		border-left: 2px solid #93c5fd;
		font-size: 0.75rem;
		line-height: 1.45;
		color: var(--text-primary, #334155);
	}

	.plugin-memory-facts {
		display: grid;
		gap: 0.45rem;
		margin-top: 0.65rem;
	}

	.plugin-memory-facts article {
		padding: 0.55rem 0.6rem;
		border: 1px solid var(--border-color, #dbe3ef);
		border-radius: 0.45rem;
		background: var(--bg-primary, #fff);
	}

	.plugin-memory-fact-meta {
		flex-wrap: wrap;
		font: 0.62rem/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		text-transform: uppercase;
		color: var(--text-secondary, #64748b);
	}

	.plugin-memory-facts p {
		margin-top: 0.35rem;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-primary, #334155);
	}

	.plugin-memory-fact-actions {
		margin-top: 0.45rem;
	}

	.plugin-activity-heading {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.15rem 0 0.35rem;
	}

	.plugin-activity-heading h2 {
		font-size: 0.8rem;
		font-weight: 700;
		color: var(--text-primary, #111827);
	}

	.plugin-activity-feed {
		display: grid;
	}

	.plugin-notice {
		margin: 0;
		padding: 1rem;
		border: 1px dashed var(--border-color, #cbd5e1);
		border-radius: 0.65rem;
		font-size: 0.8rem;
		color: var(--text-secondary, #64748b);
	}

	.plugin-notice.error {
		border-color: #fca5a5;
		color: #b91c1c;
	}

	:global(.dark) .plugin-console-header h1,
	:global(.dark) .plugin-activity-heading h2,
	:global(.dark) .plugin-memory-heading h2,
	:global(.dark) .plugin-memory-stage,
	:global(.dark) .plugin-memory-facts p {
		color: #e5e7eb;
	}
	:global(.dark) .plugin-console-header button {
		color: #cbd5e1;
	}

	@media (max-width: 720px) {
		.plugin-panel {
			grid-template-columns: minmax(0, 1fr);
			grid-template-rows: auto minmax(0, 1fr);
		}
		.plugin-console-header {
			padding: 0.7rem 0.75rem;
		}
		.plugin-console-body {
			gap: 0.65rem;
			padding: 0.65rem 0.75rem 0.85rem;
		}
		.plugin-memory-heading {
			flex-direction: column;
		}
		.plugin-memory-actions {
			width: 100%;
			justify-content: flex-end;
		}
	}

	@media (max-width: 390px) {
		.plugin-console-header {
			align-items: flex-start;
		}
		.plugin-console-header p {
			max-width: 15rem;
		}
		.plugin-console-body {
			padding: 0.55rem 0.6rem 0.75rem;
		}
	}
</style>
