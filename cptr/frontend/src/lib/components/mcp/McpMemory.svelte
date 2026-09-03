<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getMcpMemorySnapshot,
		openMcpMemoryStream,
		type McpMemoryEdge,
		type McpMemoryEvent,
		type McpMemoryNode,
		type McpMemorySnapshot
	} from '$lib/apis/mcp';
	import McpMemoryGraph from './McpMemoryGraph.svelte';

	type StreamStatus = 'loading' | 'live' | 'reconnecting' | 'error';

	let snapshot = $state<McpMemorySnapshot | null>(null);
	let selectedWorkspaceId = $state<string>('');
	let selectedNodeId = $state<string | null>(null);
	let searchQuery = $state('');
	let streamStatus = $state<StreamStatus>('loading');
	let errorMessage = $state<string | null>(null);
	let closeStream: (() => void) | null = null;
	let connectionGeneration = 0;

	const memoryNodes = $derived((snapshot?.nodes ?? []).filter((node) => node.kind === 'memory'));
	const selectedNode = $derived(
		selectedNodeId ? (snapshot?.nodes.find((node) => node.id === selectedNodeId) ?? null) : null
	);
	const recentRecallNodeIds = $derived(
		(snapshot?.recall_traces[0]?.items ?? []).map((item) => item.node_id).filter(Boolean)
	);
	const visibleNodes = $derived.by(() => filterNodes(snapshot?.nodes ?? [], searchQuery));
	const visibleNodeIds = $derived(new Set(visibleNodes.map((node) => node.id)));
	const visibleEdges = $derived(
		(snapshot?.edges ?? []).filter(
			(edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)
		)
	);
	const latestEvents = $derived(snapshot?.events ?? []);
	const latestRecall = $derived(snapshot?.recall_traces[0] ?? null);
	const selectedRelations = $derived(
		selectedNode ? relationRows(selectedNode, snapshot?.nodes ?? [], snapshot?.edges ?? []) : []
	);
	const recentCheckpoints = $derived(snapshot?.lifecycle?.checkpoints.slice(0, 6) ?? []);
	const recentSnapshots = $derived(snapshot?.lifecycle?.snapshots.slice(0, 4) ?? []);
	const recentBranches = $derived(snapshot?.lifecycle?.branches.slice(0, 4) ?? []);

	function filterNodes(nodes: McpMemoryNode[], query: string): McpMemoryNode[] {
		const term = query.trim().toLowerCase();
		if (!term) return nodes;
		const matches = new Set(
			nodes
				.filter((node) => {
					if (node.kind === 'scope') return false;
					return [node.label, node.path, node.heading, node.memory_id, node.preview, node.workspace_name]
						.filter(Boolean)
						.some((value) => String(value).toLowerCase().includes(term));
				})
				.map((node) => node.id)
		);
		const scopeIds = new Set<string>();
		for (const edge of snapshot?.edges ?? []) {
			if (edge.kind !== 'belongs_to' || !matches.has(edge.source)) continue;
			scopeIds.add(edge.target);
		}
		return nodes.filter((node) => matches.has(node.id) || scopeIds.has(node.id));
	}

	function relationRows(
		node: McpMemoryNode,
		nodes: McpMemoryNode[],
		edges: McpMemoryEdge[]
	): Array<{ node: McpMemoryNode; kind: string }> {
		const byId = new Map(nodes.map((item) => [item.id, item]));
		return edges
			.filter((edge) => edge.kind === 'related' && (edge.source === node.id || edge.target === node.id))
			.map((edge) => ({
				node: byId.get(edge.source === node.id ? edge.target : edge.source),
				kind: edge.label || 'Related'
			}))
			.filter((row): row is { node: McpMemoryNode; kind: string } => Boolean(row.node))
			.slice(0, 12);
	}

	function selectBestNode(next: McpMemorySnapshot) {
		if (selectedNodeId && next.nodes.some((node) => node.id === selectedNodeId)) return;
		const recalled = next.recall_traces[0]?.items.find((item) =>
			next.nodes.some((node) => node.id === item.node_id)
		)?.node_id;
		const hottest = [...next.nodes]
			.filter((node) => node.kind === 'memory')
			.sort((a, b) => Number(b.recall_count || 0) - Number(a.recall_count || 0))[0]?.id;
		selectedNodeId = recalled || hottest || next.nodes[0]?.id || null;
	}

	function applySnapshot(next: McpMemorySnapshot) {
		snapshot = next;
		selectBestNode(next);
		streamStatus = 'live';
		errorMessage = null;
	}

	async function connect(workspaceId: string | null) {
		const generation = ++connectionGeneration;
		closeStream?.();
		closeStream = null;
		streamStatus = snapshot ? 'reconnecting' : 'loading';
		errorMessage = null;
		try {
			const initial = await getMcpMemorySnapshot(workspaceId);
			if (generation !== connectionGeneration) return;
			applySnapshot(initial);
			closeStream = openMcpMemoryStream(workspaceId, {
				onSnapshot: (next) => {
					if (generation === connectionGeneration) applySnapshot(next);
				},
				onOpen: () => {
					if (generation === connectionGeneration) streamStatus = 'live';
				},
				onError: () => {
					if (generation !== connectionGeneration) return;
					streamStatus = 'reconnecting';
					errorMessage = 'Realtime memory stream reconnecting';
				}
			});
		} catch (error) {
			if (generation !== connectionGeneration) return;
			streamStatus = 'error';
			errorMessage = error instanceof Error ? error.message : 'Unable to load memory observatory';
		}
	}

	function changeWorkspace(value: string) {
		selectedWorkspaceId = value;
		selectedNodeId = null;
		void connect(value || null);
	}

	onMount(() => {
		void connect(null);
		return () => {
			connectionGeneration += 1;
			closeStream?.();
		};
	});

	function formatBytes(value: number | null | undefined): string {
		const bytes = Number(value || 0);
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10_240 ? 1 : 0)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}

	function relativeTime(value: number | null | undefined): string {
		if (!value) return 'Never';
		const seconds = Math.max(0, Math.round((Date.now() - value) / 1000));
		if (seconds < 60) return `${seconds}s ago`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m ago`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `${hours}h ago`;
		const days = Math.floor(hours / 24);
		if (days < 30) return `${days}d ago`;
		return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(
			new Date(value)
		);
	}

	function eventTone(event: McpMemoryEvent): 'recall' | 'write' | 'danger' | 'neutral' {
		if (event.event_type === 'recall') return 'recall';
		if (event.event_type === 'write') return 'write';
		if (event.event_type === 'write_rejected') return 'danger';
		return 'neutral';
	}

	function eventLabel(event: McpMemoryEvent): string {
		if (event.event_type === 'recall') return 'Recall';
		if (event.event_type === 'write') return 'Write';
		if (event.event_type === 'write_rejected') return 'Rejected';
		return event.event_type.replaceAll('_', ' ');
	}
</script>

<div class="memory-root app-theme h-full min-h-0 overflow-y-auto">
	<div class="mx-auto flex min-h-full w-full max-w-[118rem] flex-col gap-3 px-3 py-3 sm:gap-4 sm:px-5 sm:py-4 xl:px-6">
		<section class="memory-hero app-raised-surface border">
			<div class="flex flex-col gap-3 p-3 sm:p-4 xl:flex-row xl:items-center xl:justify-between">
				<div class="min-w-0">
					<div class="flex flex-wrap items-center gap-2">
						<h2 class="text-base font-semibold tracking-[-0.02em] sm:text-lg">Memory Observatory</h2>
						<span class="live-pill" data-status={streamStatus} title={errorMessage ?? streamStatus}>
							<span></span>{streamStatus === 'live' ? 'Live' : streamStatus === 'loading' ? 'Loading' : 'Syncing'}
						</span>
					</div>
					<p class="mt-1 max-w-3xl text-xs leading-relaxed app-muted">
						Canonical managed memory, graph relationships, recall provenance, mutation history, and safety state — projected from the CPTR backend in realtime.
					</p>
				</div>
				<div class="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
					<label class="field-shell">
						<span class="sr-only">Memory workspace</span>
						<select
							value={selectedWorkspaceId}
							onchange={(event) => changeWorkspace(event.currentTarget.value)}
						>
							<option value="">All memory</option>
							{#each snapshot?.workspaces ?? [] as workspace (workspace.workspace_id)}
								<option value={workspace.workspace_id}>{workspace.workspace_name}</option>
							{/each}
						</select>
					</label>
					<label class="field-shell min-w-0 sm:w-64">
						<span class="sr-only">Search memories</span>
						<svg viewBox="0 0 24 24" class="size-4 shrink-0 app-muted" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
						<input bind:value={searchQuery} placeholder="Search memory graph…" />
					</label>
				</div>
			</div>
			<div class="health-rail border-t">
				<span class="health-chip"><i data-ok={snapshot?.health.enabled}></i>Memory {snapshot?.health.enabled ? 'enabled' : 'disabled'}</span>
				<span class="health-chip"><i data-ok={snapshot?.health.required_for_execution}></i><strong>Gate</strong> {snapshot?.health.required_for_execution ? 'fail closed' : 'optional'}</span>
				<span class="health-chip"><i data-ok={snapshot?.health.maintenance_enabled}></i>Maintenance</span>
				<span class="health-chip"><strong>Version</strong> {snapshot?.metrics.memory_version ?? 0}</span>
				<span class="health-chip"><strong>Queue</strong> {snapshot?.metrics.pending_memory_jobs ?? 0} pending · {snapshot?.metrics.running_memory_jobs ?? 0} running</span>
				<span class="health-chip"><strong>Stale</strong> {snapshot?.metrics.stale_verification_nodes ?? 0}</span>
				<span class="health-chip"><strong>Truth</strong> {snapshot?.health.canonical_store ?? '—'}</span>
				<span class="health-chip"><strong>Recall</strong> {snapshot?.health.retrieval ?? '—'}</span>
			</div>
		</section>

		{#if streamStatus === 'error' && !snapshot}
			<section class="app-raised-surface flex min-h-80 items-center justify-center rounded-2xl border p-6 text-center">
				<div class="max-w-md">
					<h3 class="text-base font-semibold">Memory telemetry unavailable</h3>
					<p class="mt-2 text-sm app-muted">{errorMessage ?? 'The memory projection API could not be reached.'}</p>
					<button class="app-interactive app-accent-surface mt-4 rounded-xl border px-4 py-2 text-xs font-semibold" onclick={() => void connect(selectedWorkspaceId || null)}>Retry</button>
				</div>
			</section>
		{:else}
			<section class="metric-grid app-raised-surface border">
				<div class="metric-cell"><p>Memories</p><strong>{snapshot?.metrics.memory_nodes ?? 0}</strong><small>{snapshot?.metrics.managed_memory_nodes ?? 0} managed · {snapshot?.metrics.canonical_memory_nodes ?? 0} canonical</small></div>
				<div class="metric-cell"><p>Entities</p><strong>{snapshot?.metrics.entity_nodes ?? 0}</strong><small>{snapshot?.metrics.edge_count ?? 0} graph edges</small></div>
				<div class="metric-cell"><p>Recalls · 24h</p><strong>{snapshot?.metrics.recalls_24h ?? 0}</strong><small>{snapshot?.metrics.writes_24h ?? 0} writes · {latestRecall ? `${latestRecall.items.length} latest refs` : 'no trace yet'}</small></div>
				<div class="metric-cell"><p>Memory version</p><strong>{snapshot?.metrics.memory_version ?? 0}</strong><small>{snapshot?.metrics.checkpoint_count ?? 0} durable checkpoints</small></div>
				<div class="metric-cell"><p>Snapshots</p><strong>{snapshot?.metrics.snapshot_count ?? 0}</strong><small>{snapshot?.metrics.branch_count ?? 0} non-destructive branches</small></div>
				<div class="metric-cell"><p>Maintenance</p><strong>{(snapshot?.metrics.pending_memory_jobs ?? 0) + (snapshot?.metrics.running_memory_jobs ?? 0)}</strong><small>{snapshot?.metrics.failed_memory_jobs ?? 0} failed · {snapshot?.metrics.stale_verification_nodes ?? 0} stale</small></div>
			</section>

			<div class="observatory-grid">
				<section class="graph-panel app-raised-surface border">
					<div class="panel-heading border-b">
						<div>
							<p class="kicker">Live constellation</p>
							<h3>Knowledge graph</h3>
						</div>
						<div class="flex items-center gap-3 text-[0.65rem] app-muted">
							<span>{visibleNodes.filter((node) => node.kind === 'memory').length} visible</span>
							<span>Drag · pinch · zoom</span>
						</div>
					</div>
					<div class="graph-wrap">
						{#if snapshot && visibleNodes.length > 0}
							<McpMemoryGraph
								nodes={visibleNodes}
								edges={visibleEdges}
								{selectedNodeId}
								{recentRecallNodeIds}
								onselect={(nodeId) => (selectedNodeId = nodeId)}
							/>
						{:else}
							<div class="flex h-full min-h-80 items-center justify-center p-6 text-center">
								<div><p class="text-sm font-semibold">No memory nodes match this view</p><p class="mt-1 text-xs app-muted">Clear the search or select another workspace.</p></div>
							</div>
						{/if}
					</div>
					<div class="graph-legend border-t">
						<span><i class="legend-dot user"></i>User memory</span>
						<span><i class="legend-dot workspace"></i>Workspace memory</span>
						<span><i class="legend-dot recalled"></i>Latest recall</span>
						<span><i class="legend-line"></i>Explicit [[relationship]]</span>
					</div>
				</section>

				<aside class="inspector app-raised-surface border">
					<div class="panel-heading border-b">
						<div><p class="kicker">Inspector</p><h3>{selectedNode?.kind === 'scope' ? 'Memory scope' : selectedNode?.kind === 'entity' ? 'Derived entity' : 'Memory provenance'}</h3></div>
						{#if selectedNode?.kind !== 'scope' && selectedNode}<span class="confidence">{Math.round((selectedNode.confidence || 0) * 100)}% confidence</span>{/if}
					</div>
					{#if selectedNode}
						<div class="inspector-scroll">
							<div class="memory-title-block">
								<div class="scope-badge" data-scope={selectedNode.scope}>{selectedNode.scope === 'user' ? 'User' : selectedNode.workspace_name ?? 'Workspace'}</div>
								<h4>{selectedNode.label}</h4>
								{#if selectedNode.preview}<p>{selectedNode.preview}</p>{/if}
							</div>
							<div class="property-grid">
								<div><span>Trust</span><strong>{selectedNode.trust_level}</strong></div>
								<div><span>Status</span><strong>{selectedNode.status}</strong></div>
								<div><span>Layer</span><strong>{selectedNode.source_layer ?? 'managed markdown'}</strong></div>
								<div><span>Importance</span><strong>{selectedNode.importance == null ? '—' : `${Math.round(selectedNode.importance * 100)}%`}</strong></div>
								<div><span>Recalled</span><strong>{selectedNode.recall_count ?? 0}×</strong></div>
								<div><span>Last recall</span><strong>{relativeTime(selectedNode.last_recalled_at_ms)}</strong></div>
								<div><span>Updated</span><strong>{relativeTime(selectedNode.modified_at_ms)}</strong></div>
								<div><span>Verification</span><strong>{selectedNode.verification_stale ? 'stale · reverify' : selectedNode.verified_at_ms ? 'fresh' : 'not verified'}</strong></div>
							</div>
							{#if selectedNode.path}
								<div class="inspector-section"><span>Canonical path</span><code>{selectedNode.path}</code>{#if selectedNode.memory_id}<code class="mt-1">id: {selectedNode.memory_id}</code>{/if}</div>
							{/if}
							<div class="inspector-section">
								<div class="flex items-center justify-between"><span>Graph relationships</span><small>{selectedRelations.length}</small></div>
								{#if selectedRelations.length}
									<div class="relation-list">
										{#each selectedRelations as relation (relation.node.id)}
											<button class="app-interactive" onclick={() => (selectedNodeId = relation.node.id)}><i data-scope={relation.node.scope}></i><span><strong>{relation.node.label}</strong><small>{relation.kind}</small></span></button>
										{/each}
									</div>
								{:else}<p class="empty-copy">No explicit relationship from this memory.</p>{/if}
							</div>
						</div>
					{:else}
						<div class="flex min-h-72 items-center justify-center p-6 text-center"><p class="text-xs app-muted">Select a node to inspect its provenance.</p></div>
					{/if}
				</aside>
			</div>

			<div class="lifecycle-grid">
				<section class="stream-panel app-raised-surface border">
					<div class="panel-heading border-b"><div><p class="kicker">Restart continuity</p><h3>Recent checkpoints</h3></div><span class="panel-count">{snapshot?.metrics.checkpoint_count ?? 0}</span></div>
					<div class="compact-list">
						{#if recentCheckpoints.length === 0}<div class="panel-empty">Task checkpoints appear after memory context and tool completion.</div>{:else}
							{#each recentCheckpoints as checkpoint (checkpoint.checkpoint_id)}
								<div class="compact-row"><span><strong>{checkpoint.stage.replaceAll('_', ' ')}</strong><small>task {checkpoint.task_key_hash} · checkpoint v{checkpoint.version}</small></span><time>mem v{checkpoint.memory_version} · {relativeTime(checkpoint.created_at_ms)}</time></div>
							{/each}
						{/if}
					</div>
				</section>
				<section class="stream-panel app-raised-surface border">
					<div class="panel-heading border-b"><div><p class="kicker">Time travel</p><h3>Snapshots & branches</h3></div><span class="panel-count">{(snapshot?.metrics.snapshot_count ?? 0) + (snapshot?.metrics.branch_count ?? 0)}</span></div>
					<div class="compact-list">
						{#if recentSnapshots.length === 0 && recentBranches.length === 0}<div class="panel-empty">No memory snapshots or branches yet.</div>{:else}
							{#each recentSnapshots as item (item.snapshot_id)}<div class="compact-row"><span><strong>{item.label || 'Snapshot'}</strong><small>{item.record_count} records · memory v{item.memory_version}</small></span><time>{relativeTime(item.created_at_ms)}</time></div>{/each}
							{#each recentBranches as item (item.branch_id)}<div class="compact-row"><span><strong>{item.name}</strong><small>branch · {item.status}</small></span><time>{relativeTime(item.updated_at_ms)}</time></div>{/each}
						{/if}
					</div>
				</section>
				<section class="stream-panel app-raised-surface border">
					<div class="panel-heading border-b"><div><p class="kicker">Durability health</p><h3>Temporal & queue state</h3></div><span class="panel-count">v{snapshot?.metrics.memory_version ?? 0}</span></div>
					<div class="health-summary">
						<div><span>Fail-closed gate</span><strong>{snapshot?.health.required_for_execution ? 'Required' : 'Optional'}</strong></div>
						<div><span>Stale verification</span><strong>{snapshot?.metrics.stale_verification_nodes ?? 0}</strong></div>
						<div><span>Superseded history</span><strong>{snapshot?.metrics.superseded_memory_nodes ?? 0}</strong></div>
						<div><span>Queued jobs</span><strong>{snapshot?.metrics.pending_memory_jobs ?? 0}</strong></div>
						<div><span>Running jobs</span><strong>{snapshot?.metrics.running_memory_jobs ?? 0}</strong></div>
						<div><span>Failed jobs</span><strong>{snapshot?.metrics.failed_memory_jobs ?? 0}</strong></div>
					</div>
				</section>
			</div>

			<div class="stream-grid">
				<section class="stream-panel app-raised-surface border">
					<div class="panel-heading border-b"><div><p class="kicker">Realtime journal</p><h3>Memory pulse</h3></div><span class="panel-count">{latestEvents.length}</span></div>
					<div class="event-list">
						{#if latestEvents.length === 0}<div class="panel-empty">Recall and mutation events will appear here as CPTR uses memory.</div>{:else}
							{#each latestEvents.slice(0, 24) as event (event.event_id)}
								<div class="event-row">
									<span class="event-orb" data-tone={eventTone(event)}></span>
									<div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-2"><strong>{eventLabel(event)}</strong>{#if event.scope}<span class="event-scope">{event.scope}</span>{/if}</div><p>{event.reason ?? 'Memory fabric event'}</p></div>
									<time>{relativeTime(event.created_at_ms)}</time>
								</div>
							{/each}
						{/if}
					</div>
				</section>

				<section class="stream-panel app-raised-surface border">
					<div class="panel-heading border-b"><div><p class="kicker">Why CPTR remembered</p><h3>Recall traces</h3></div><span class="panel-count">{snapshot?.recall_traces.length ?? 0}</span></div>
					<div class="trace-list">
						{#if (snapshot?.recall_traces.length ?? 0) === 0}<div class="panel-empty">The next memory-hydrated CPTR prompt will create a provenance trace.</div>{:else}
							{#each (snapshot?.recall_traces ?? []).slice(0, 10) as trace (trace.event_id)}
								<article class="trace-row">
									<div class="trace-head"><strong>{trace.items.length} injected references</strong><span>{trace.context_chars.toLocaleString()} chars · {relativeTime(trace.created_at_ms)}</span></div>
									<div class="trace-items">
										{#each trace.items.slice(0, 5) as item}
											<button class="app-interactive" disabled={!item.node_id} onclick={() => item.node_id && (selectedNodeId = item.node_id)}><span>{item.heading || item.path}</span><small>{item.reason}</small></button>
										{/each}
									</div>
								</article>
							{/each}
						{/if}
					</div>
				</section>
			</div>
		{/if}
	</div>
</div>

<style>
	.memory-root { background: radial-gradient(circle at 50% -12rem, color-mix(in oklab, var(--app-accent) 5%, transparent), transparent 46rem), var(--app-bg); }
	.memory-hero, .graph-panel, .inspector, .stream-panel, .metric-grid { border-color: var(--app-border); border-radius: 1rem; overflow: hidden; }
	.live-pill { display:inline-flex; align-items:center; gap:.38rem; min-height:1.55rem; border:1px solid var(--app-border); border-radius:999px; padding:0 .55rem; font-size:.6rem; font-weight:700; color:var(--app-fg-muted); }
	.live-pill span { width:.42rem; height:.42rem; border-radius:99px; background:var(--app-fg-subtle); }
	.live-pill[data-status='live'] span { background:#34d399; box-shadow:0 0 0 .18rem #34d3991f; }
	.live-pill[data-status='reconnecting'] span { background:#f59e0b; animation:pulse 1s ease-in-out infinite; }
	.live-pill[data-status='error'] span { background:#fb7185; }
	.field-shell { display:flex; min-height:2.55rem; align-items:center; gap:.5rem; border:1px solid var(--app-border); border-radius:.7rem; background:var(--app-surface-subtle); padding:0 .7rem; }
	.field-shell select, .field-shell input { min-width:0; width:100%; border:0; outline:0; background:transparent; color:var(--app-fg); font-size:.72rem; }
	.field-shell select { min-width:9rem; }
	.health-rail { display:flex; overflow-x:auto; gap:.4rem; border-color:var(--app-divider); padding:.55rem .75rem; }
	.health-chip { display:inline-flex; flex:0 0 auto; min-height:1.65rem; align-items:center; gap:.38rem; border:1px solid var(--app-border); border-radius:999px; padding:0 .55rem; font-size:.58rem; color:var(--app-fg-muted); }
	.health-chip strong { color:var(--app-fg); font-weight:650; }
	.health-chip i { width:.38rem; height:.38rem; border-radius:99px; background:#fb7185; }
	.health-chip i[data-ok='true'] { background:#34d399; }
	.metric-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); }
	.metric-cell { min-width:0; border-right:1px solid var(--app-divider); padding:.75rem .85rem; }
	.metric-cell:last-child { border-right:0; }
	.metric-cell p, .kicker { font-size:.58rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:var(--app-fg-subtle); }
	.metric-cell strong { display:block; margin-top:.28rem; font-size:1.18rem; line-height:1; font-weight:720; letter-spacing:-.025em; font-variant-numeric:tabular-nums; }
	.metric-cell small { display:block; margin-top:.3rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.58rem; color:var(--app-fg-muted); }
	.observatory-grid { display:grid; min-height:34rem; grid-template-columns:minmax(0,1.9fr) minmax(19rem,.72fr); gap:1rem; }
	.graph-panel { display:grid; min-width:0; grid-template-rows:auto minmax(0,1fr) auto; }
	.graph-wrap { min-height:0; background:color-mix(in oklab,var(--app-surface) 70%,transparent); }
	.panel-heading { display:flex; min-height:3.4rem; align-items:center; justify-content:space-between; gap:1rem; padding:.65rem .85rem; border-color:var(--app-divider); }
	.panel-heading h3 { margin-top:.08rem; font-size:.8rem; font-weight:700; }
	.panel-count, .confidence { border:1px solid var(--app-border); border-radius:.45rem; padding:.18rem .4rem; font-size:.58rem; color:var(--app-fg-muted); font-variant-numeric:tabular-nums; }
	.graph-legend { display:flex; flex-wrap:wrap; gap:.8rem 1rem; min-height:2.4rem; align-items:center; border-color:var(--app-divider); padding:.45rem .8rem; font-size:.58rem; color:var(--app-fg-muted); }
	.graph-legend span { display:inline-flex; align-items:center; gap:.35rem; }
	.legend-dot { width:.48rem; height:.48rem; border-radius:99px; }
	.legend-dot.user { background:#a78bfa; }.legend-dot.workspace { background:#38bdf8; }.legend-dot.recalled { background:#f59e0b; }
	.legend-line { width:1.1rem; height:1px; background:#94a3b8; opacity:.55; }
	.inspector { min-width:0; display:grid; grid-template-rows:auto minmax(0,1fr); }
	.inspector-scroll { min-height:0; overflow-y:auto; }
	.memory-title-block { padding:1rem; border-bottom:1px solid var(--app-divider); }
	.scope-badge { display:inline-flex; min-height:1.4rem; align-items:center; border:1px solid currentColor; border-radius:999px; padding:0 .45rem; font-size:.55rem; font-weight:750; color:#a78bfa; }
	.scope-badge[data-scope='workspace'] { color:#38bdf8; }
	.memory-title-block h4 { margin-top:.6rem; font-size:1rem; font-weight:700; line-height:1.25; }
	.memory-title-block p { margin-top:.5rem; font-size:.7rem; line-height:1.55; color:var(--app-fg-muted); }
	.property-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); border-bottom:1px solid var(--app-divider); }
	.property-grid div { min-width:0; padding:.65rem .8rem; border-right:1px solid var(--app-divider); border-bottom:1px solid var(--app-divider); }
	.property-grid div:nth-child(2n) { border-right:0; }.property-grid div:nth-last-child(-n+2) { border-bottom:0; }
	.property-grid span, .inspector-section > span, .inspector-section > div > span { display:block; font-size:.55rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--app-fg-subtle); }
	.property-grid strong { display:block; margin-top:.2rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.66rem; font-weight:650; }
	.inspector-section { padding:.8rem; border-bottom:1px solid var(--app-divider); }
	.inspector-section code { display:block; margin-top:.45rem; overflow-wrap:anywhere; border-left:1px solid var(--app-divider); padding-left:.5rem; font-size:.6rem; color:var(--app-fg-muted); }
	.empty-copy { margin-top:.55rem; font-size:.65rem; color:var(--app-fg-muted); }
	.relation-list { display:grid; gap:.3rem; margin-top:.55rem; }
	.relation-list button { display:flex; width:100%; align-items:center; gap:.55rem; border-radius:.55rem; padding:.45rem .5rem; text-align:left; }
	.relation-list i { width:.45rem; height:.45rem; flex:0 0 auto; border-radius:99px; background:#a78bfa; }.relation-list i[data-scope='workspace'] { background:#38bdf8; }
	.relation-list span { min-width:0; }.relation-list strong, .relation-list small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.relation-list strong { font-size:.64rem; }.relation-list small { margin-top:.08rem; font-size:.55rem; color:var(--app-fg-muted); }
	.lifecycle-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1rem; }
	.stream-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1rem; }
	.compact-list { max-height:16rem; overflow-y:auto; }
	.compact-row { display:flex; align-items:flex-start; justify-content:space-between; gap:.75rem; border-bottom:1px solid var(--app-divider); padding:.58rem .75rem; }
	.compact-row:last-child { border-bottom:0; }.compact-row span { min-width:0; }.compact-row strong,.compact-row small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.compact-row strong { font-size:.63rem; }.compact-row small { margin-top:.1rem; font-size:.54rem; color:var(--app-fg-muted); }.compact-row time { flex:0 0 auto; max-width:48%; text-align:right; font-size:.54rem; color:var(--app-fg-subtle); }
	.health-summary { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); }
	.health-summary div { min-width:0; border-right:1px solid var(--app-divider); border-bottom:1px solid var(--app-divider); padding:.7rem .75rem; }.health-summary div:nth-child(2n) { border-right:0; }.health-summary div:nth-last-child(-n+2) { border-bottom:0; }.health-summary span { display:block; font-size:.54rem; color:var(--app-fg-muted); }.health-summary strong { display:block; margin-top:.18rem; font-size:.72rem; font-variant-numeric:tabular-nums; }
	.event-list, .trace-list { max-height:24rem; overflow-y:auto; }
	.event-row { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:start; gap:.6rem; border-bottom:1px solid var(--app-divider); padding:.65rem .8rem; }
	.event-orb { width:.48rem; height:.48rem; margin-top:.2rem; border-radius:99px; background:var(--app-fg-subtle); box-shadow:0 0 0 .16rem color-mix(in oklab,var(--app-fg-subtle) 12%,transparent); }
	.event-orb[data-tone='recall'] { background:#f59e0b; }.event-orb[data-tone='write'] { background:#34d399; }.event-orb[data-tone='danger'] { background:#fb7185; }
	.event-row strong { font-size:.65rem; }.event-row p { margin-top:.18rem; overflow-wrap:anywhere; font-size:.6rem; line-height:1.4; color:var(--app-fg-muted); }.event-row time { font-size:.55rem; color:var(--app-fg-subtle); white-space:nowrap; }
	.event-scope { border-radius:.35rem; background:var(--app-hover); padding:.08rem .3rem; font-size:.52rem; color:var(--app-fg-muted); }
	.trace-row { border-bottom:1px solid var(--app-divider); padding:.7rem .8rem; }.trace-row:last-child,.event-row:last-child { border-bottom:0; }
	.trace-head { display:flex; align-items:center; justify-content:space-between; gap:.75rem; }.trace-head strong { font-size:.66rem; }.trace-head span { font-size:.55rem; color:var(--app-fg-subtle); }
	.trace-items { display:grid; gap:.26rem; margin-top:.5rem; }.trace-items button { display:flex; min-width:0; align-items:center; justify-content:space-between; gap:.7rem; border-radius:.45rem; padding:.32rem .42rem; text-align:left; }.trace-items button:disabled { cursor:default; opacity:.7; }.trace-items span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.6rem; }.trace-items small { flex:0 0 auto; max-width:42%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.52rem; color:var(--app-fg-muted); }
	.panel-empty { padding:1.4rem .9rem; text-align:center; font-size:.68rem; color:var(--app-fg-muted); }
	@keyframes pulse { 50% { opacity:.35; transform:scale(.75); } }
	@media (max-width:1279px) { .metric-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }.metric-cell:nth-child(3n) { border-right:0; }.metric-cell:nth-child(-n+3) { border-bottom:1px solid var(--app-divider); } }
	@media (max-width:1023px) { .observatory-grid { grid-template-columns:1fr; }.inspector { min-height:20rem; }.lifecycle-grid,.stream-grid { grid-template-columns:1fr; } }
	@media (max-width:639px) { .metric-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }.metric-cell:nth-child(3n) { border-right:1px solid var(--app-divider); }.metric-cell:nth-child(2n) { border-right:0; }.metric-cell:nth-child(-n+4) { border-bottom:1px solid var(--app-divider); }.health-rail { padding-inline:.55rem; }.observatory-grid { gap:.7rem; }.stream-grid { gap:.7rem; }.panel-heading { padding-inline:.7rem; }.graph-legend { gap:.5rem .7rem; } }
	@media (prefers-reduced-motion:reduce) { .live-pill span { animation:none!important; } }
</style>
