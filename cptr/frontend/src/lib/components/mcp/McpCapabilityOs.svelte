<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getMcpCapabilityOsOperatorSnapshot,
		getMcpCapabilityOsTasks,
		openMcpCapabilityOsStream,
		type McpCapabilityOsOperatorSnapshot,
		type McpCapabilityOsStreamError,
		type McpCapabilityOsTask
	} from '$lib/apis/mcp';

	type StreamStatus = 'connecting' | 'live' | 'reconnecting' | 'error';

	const sections = [
		['capability-health', 'Health'],
		['task-causality', 'Causality'],
		['forge', 'Forge'],
		['mcp-fabric', 'MCP Fabric'],
		['sandbox', 'Sandbox'],
		['evolution', 'Evolution']
	] as const;

	let tasks = $state<McpCapabilityOsTask[]>([]);
	let selectedTaskId = $state('');
	let snapshot = $state<McpCapabilityOsOperatorSnapshot | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let errorMessage = $state<string | null>(null);
	let streamError = $state<string | null>(null);
	let streamStatus = $state<'connecting' | 'live' | 'reconnecting' | 'error'>('connecting');
	let lastUpdatedAt = $state<number | null>(null);
	let lastTaskListAt = $state(0);
	let nowMs = $state(Date.now());
	let refreshGeneration = 0;
	let stopStream: (() => void) | null = null;

	const selectedTask = $derived(tasks.find((task) => task.taskId === selectedTaskId) ?? null);
	const views = $derived(snapshot?.views ?? null);
	const artifactEntries = $derived(
		Object.entries(snapshot?.artifactStates ?? {}).sort((a, b) => b[1] - a[1])
	);
	const evidenceEntries = $derived(
		Object.entries(snapshot?.evidenceKinds ?? {})
			.sort((a, b) => b[1] - a[1])
			.slice(0, 8)
	);
	const runtimeEntries = $derived(
		Object.entries(views?.sandbox.runtime ?? {}).filter(
			([, value]) => typeof value === 'boolean'
		) as Array<[string, boolean]>
	);
	const availableRuntimeCount = $derived(
		runtimeEntries.filter(([, available]) => available).length
	);
	const chainHealthy = $derived(isEvidenceChainHealthy(views?.taskCausality.evidenceChain));
	const qualifiedArtifacts = $derived(
		artifactEntries.find(([state]) => state.toLowerCase() === 'qualified')?.[1] ?? 0
	);
	const qualificationPercent = $derived(
		views && views.capabilityHealth.artifacts > 0
			? Math.round((qualifiedArtifacts / views.capabilityHealth.artifacts) * 100)
			: 0
	);
	const evolutionPercent = $derived(
		views && views.evolution.experiments > 0
			? Math.round((views.evolution.activeExperiments / views.evolution.experiments) * 100)
			: 0
	);
	const streamStatusLabel = $derived(streamLabel(streamStatus));
	const taskCoreState = $derived.by(() => {
		if (!snapshot) return 'waiting';
		if (!snapshot.task.active) return 'idle';
		if (!snapshot.task.executionAllowed) return 'blocked';
		if (snapshot.views.taskCausality.activeRuns > 0) return 'running';
		return 'ready';
	});

	function shortId(value: string | null | undefined, length = 12): string {
		if (!value) return '—';
		return value.length > length ? `${value.slice(0, length)}…` : value;
	}

	function titleCase(value: string): string {
		return value
			.replaceAll('-', ' ')
			.replaceAll('_', ' ')
			.split(' ')
			.filter(Boolean)
			.map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
			.join(' ');
	}

	function relativeTime(timestamp: number | null | undefined): string {
		if (!timestamp) return 'never';
		const seconds = Math.max(0, Math.round((nowMs - timestamp) / 1000));
		if (seconds < 5) return 'now';
		if (seconds < 60) return `${seconds}s ago`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m ago`;
		const hours = Math.floor(minutes / 60);
		return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
	}

	function leaseRemaining(expiresAtMs: number): string {
		const remaining = Math.max(0, expiresAtMs - nowMs);
		if (remaining <= 0) return 'expired';
		const seconds = Math.ceil(remaining / 1000);
		if (seconds < 60) return `${seconds}s`;
		const minutes = Math.ceil(seconds / 60);
		return minutes < 60 ? `${minutes}m` : `${Math.ceil(minutes / 60)}h`;
	}

	function isEvidenceChainHealthy(chain: Record<string, unknown> | null | undefined): boolean {
		if (!chain) return true;
		for (const key of ['valid', 'ok', 'verified', 'intact']) {
			if (chain[key] === false) return false;
		}
		return true;
	}

	function statePercent(value: number): number {
		const total = views?.capabilityHealth.artifacts ?? 0;
		return total > 0 ? Math.max(3, Math.round((value / total) * 100)) : 0;
	}

	function streamLabel(status: StreamStatus): string {
		if (status === 'live') return 'Live';
		if (status === 'reconnecting') return 'Reconnecting';
		if (status === 'error') return 'Offline';
		return 'Connecting';
	}

	function scrollToSection(id: string) {
		const target = document.getElementById(id);
		if (!target) return;
		const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		target.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
	}

	function streamFailureMessage(error: unknown, detail?: McpCapabilityOsStreamError): string {
		if (detail?.code === 'CAPABILITY_OS_TASK_NOT_FOUND')
			return 'Selected task is no longer available';
		if (error instanceof Error && error.message) return error.message;
		return 'Capability OS live stream interrupted';
	}

	function connectStream(taskId: string): void {
		stopStream?.();
		stopStream = null;
		streamError = null;
		if (!taskId) {
			streamStatus = 'error';
			return;
		}
		streamStatus = 'connecting';
		stopStream = openMcpCapabilityOsStream(taskId, {
			onSnapshot: (next) => {
				if (taskId !== selectedTaskId) return;
				snapshot = next;
				lastUpdatedAt = Date.now();
				errorMessage = null;
				streamError = null;
				streamStatus = 'live';
			},
			onOpen: () => {
				if (taskId !== selectedTaskId) return;
				streamStatus = 'live';
				streamError = null;
			},
			onError: (error, detail) => {
				if (taskId !== selectedTaskId) return;
				streamError = streamFailureMessage(error, detail);
				if (detail?.code === 'CAPABILITY_OS_TASK_NOT_FOUND') {
					streamStatus = 'error';
					errorMessage = streamError;
				} else {
					streamStatus = 'reconnecting';
				}
			}
		});
	}

	async function loadTasks(): Promise<boolean> {
		const previousTaskId = selectedTaskId;
		const response = await getMcpCapabilityOsTasks(30);
		tasks = response.tasks;
		if (!selectedTaskId || !tasks.some((task) => task.taskId === selectedTaskId)) {
			selectedTaskId = tasks[0]?.taskId ?? '';
		}
		lastTaskListAt = Date.now();
		return selectedTaskId !== previousTaskId;
	}

	async function loadSnapshot(taskId: string): Promise<void> {
		if (!taskId) {
			snapshot = null;
			return;
		}
		const generation = ++refreshGeneration;
		try {
			const next = await getMcpCapabilityOsOperatorSnapshot(taskId);
			if (generation !== refreshGeneration || taskId !== selectedTaskId) return;
			snapshot = next;
			lastUpdatedAt = Date.now();
			errorMessage = null;
		} catch (error) {
			if (generation !== refreshGeneration) return;
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
		}
	}

	async function bootstrap(): Promise<void> {
		loading = true;
		try {
			await loadTasks();
			if (selectedTaskId) connectStream(selectedTaskId);
			else streamStatus = 'error';
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
			streamStatus = 'error';
		} finally {
			loading = false;
		}
	}

	async function selectTask(taskId: string): Promise<void> {
		if (!taskId || taskId === selectedTaskId) return;
		selectedTaskId = taskId;
		snapshot = null;
		errorMessage = null;
		streamError = null;
		connectStream(taskId);
	}

	async function refresh(): Promise<void> {
		if (refreshing) return;
		refreshing = true;
		try {
			const changed = await loadTasks();
			if (changed) {
				snapshot = null;
				if (selectedTaskId) connectStream(selectedTaskId);
			}
			if (selectedTaskId) await loadSnapshot(selectedTaskId);
			if (streamStatus === 'error' && selectedTaskId) connectStream(selectedTaskId);
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
		} finally {
			refreshing = false;
		}
	}

	onMount(() => {
		void bootstrap();
		const interval = window.setInterval(() => {
			nowMs = Date.now();
			if (document.visibilityState !== 'visible' || refreshing) return;
			if (nowMs - lastTaskListAt >= 30_000) {
				void loadTasks()
					.then((changed) => {
						if (!changed) return;
						snapshot = null;
						if (selectedTaskId) connectStream(selectedTaskId);
					})
					.catch(() => undefined);
			}
		}, 1000);
		return () => {
			window.clearInterval(interval);
			stopStream?.();
			stopStream = null;
			refreshGeneration += 1;
		};
	});
</script>

<div class="operator-root app-theme">
	<div class="operator-shell">
		<section class="operator-hero" aria-labelledby="capability-os-heading">
			<div class="ambient-grid" aria-hidden="true"></div>
			<div class="ambient-glow ambient-glow-one" aria-hidden="true"></div>
			<div class="ambient-glow ambient-glow-two" aria-hidden="true"></div>

			<header class="hero-toolbar">
				<div class="hero-title">
					<div class="eyebrow-row">
						<span class="eyebrow-mark" aria-hidden="true"></span>
						<span>Capability OS</span>
						<span class="stream-pill" data-status={streamStatus} aria-live="polite">
							<span class="stream-dot" aria-hidden="true"></span>{streamStatusLabel}
						</span>
					</div>
					<h2 id="capability-os-heading">Operational intelligence, in motion.</h2>
					<p>Live task causality, capability health, authority, MCP fabric, and evolution.</p>
				</div>

				<div class="hero-controls">
					<label class="task-picker">
						<span>Task</span>
						<select
							value={selectedTaskId}
							disabled={loading || tasks.length === 0}
							onchange={(event) =>
								void selectTask((event.currentTarget as HTMLSelectElement).value)}
						>
							{#if tasks.length === 0}
								<option value="">No Capability OS task</option>
							{:else}
								{#each tasks as task (task.taskId)}
									<option value={task.taskId}
										>{task.source.toUpperCase()} · {task.label.slice(0, 68)}</option
									>
								{/each}
							{/if}
						</select>
					</label>
					<button
						class="refresh-button"
						type="button"
						disabled={refreshing}
						onclick={() => void refresh()}
						aria-label="Refresh Capability OS telemetry"
					>
						<svg viewBox="0 0 24 24" aria-hidden="true"
							><path d="M20 11a8 8 0 1 0-2.34 5.66M20 4v7h-7" /></svg
						>
						<span>{refreshing ? 'Refreshing' : 'Refresh'}</span>
					</button>
				</div>
			</header>

			{#if streamError && streamStatus !== 'live'}
				<div class="stream-note" data-status={streamStatus} role="status">
					<span aria-hidden="true"></span>{streamError}
				</div>
			{/if}

			{#if errorMessage && !snapshot}
				<div class="state-card error-state" role="alert">
					<div class="state-icon">!</div>
					<div><strong>Telemetry unavailable</strong><span>{errorMessage}</span></div>
					<button type="button" onclick={() => void refresh()}>Retry</button>
				</div>
			{:else if loading || (tasks.length > 0 && !snapshot)}
				<div
					class="spatial-skeleton"
					role="status"
					aria-label="Loading Capability OS live telemetry"
				>
					<div class="skeleton-orbit"></div>
					<div class="skeleton-core"></div>
				</div>
			{:else if tasks.length === 0}
				<div class="state-card empty-state">
					<div class="state-icon">○</div>
					<div>
						<strong>No active Capability OS task</strong><span
							>Open a Workbench, Factory, or Control task to populate this operator view.</span
						>
					</div>
				</div>
			{:else if views && snapshot}
				<div class="spatial-stage" aria-label="Capability OS live task map">
					<div class="stage-grid" aria-hidden="true"></div>
					<div class="orbit orbit-outer" aria-hidden="true"></div>
					<div class="orbit orbit-inner" aria-hidden="true"></div>
					<div class="orbit-sweep" aria-hidden="true"></div>

					<button
						class="orbit-node node-runs"
						data-live={views.taskCausality.activeRuns > 0}
						type="button"
						onclick={() => scrollToSection('task-causality')}
						aria-label={`Runs: ${views.taskCausality.activeRuns} active`}
						><span class="node-icon" aria-hidden="true">↗</span><strong
							>{views.taskCausality.activeRuns}</strong
						><small>Active runs</small></button
					>
					<button
						class="orbit-node node-forge"
						data-live={views.forge.runs > 0}
						type="button"
						onclick={() => scrollToSection('forge')}
						aria-label={`Forge: ${views.forge.builds} builds, ${views.forge.failures} failures`}
						><span class="node-icon" aria-hidden="true">◇</span><strong>{views.forge.builds}</strong
						><small>Forge builds</small></button
					>
					<button
						class="orbit-node node-mcp"
						data-live={views.mcpFabric.activeMounts > 0}
						type="button"
						onclick={() => scrollToSection('mcp-fabric')}
						aria-label={`MCP Fabric: ${views.mcpFabric.activeMounts} active mounts`}
						><span class="node-icon" aria-hidden="true">⌁</span><strong
							>{views.mcpFabric.activeMounts}</strong
						><small>MCP mounts</small></button
					>
					<button
						class="orbit-node node-authority"
						data-live={views.authority.activeLeases > 0}
						type="button"
						onclick={() => scrollToSection('authority')}
						aria-label={`Authority: ${views.authority.activeLeases} active leases`}
						><span class="node-icon" aria-hidden="true">⌾</span><strong
							>{views.authority.activeLeases}</strong
						><small>Leases</small></button
					>
					<button
						class="orbit-node node-runtime"
						data-live={availableRuntimeCount > 0}
						type="button"
						onclick={() => scrollToSection('sandbox')}
						aria-label={`Sandbox: ${availableRuntimeCount} runtime flags available`}
						><span class="node-icon" aria-hidden="true">⬡</span><strong
							>{availableRuntimeCount}</strong
						><small>Runtime flags</small></button
					>
					<button
						class="orbit-node node-evidence"
						data-live={views.taskCausality.evidenceRecords > 0}
						type="button"
						onclick={() => scrollToSection('task-causality')}
						aria-label={`Evidence: ${views.taskCausality.evidenceRecords} records`}
						><span class="node-icon" aria-hidden="true">◈</span><strong
							>{views.taskCausality.evidenceRecords}</strong
						><small>Evidence</small></button
					>

					<div
						class="task-core"
						data-state={taskCoreState}
						data-running={views.taskCausality.activeRuns > 0}
					>
						<div class="core-halo core-halo-one" aria-hidden="true"></div>
						<div class="core-halo core-halo-two" aria-hidden="true"></div>
						<span class="core-label">Task core</span><strong
							>{titleCase(snapshot.task.status)}</strong
						><code title={snapshot.task.taskId}>{shortId(snapshot.task.taskId, 16)}</code>
						<div class="core-state-row">
							<span class="core-state-dot" aria-hidden="true"></span><span
								>{titleCase(taskCoreState)}</span
							><span class="core-separator">·</span><span
								>{snapshot.task.executionAllowed ? 'Execution allowed' : 'Execution blocked'}</span
							>
						</div>
					</div>
				</div>

				<div class="signal-strip" aria-label="Capability OS task summary">
					<div>
						<span>Artifacts</span><strong>{views.capabilityHealth.artifacts}</strong><small
							>{views.capabilityHealth.capabilities} capabilities</small
						>
					</div>
					<div data-good={chainHealthy}>
						<span>Evidence chain</span><strong>{chainHealthy ? 'Intact' : 'Review'}</strong><small
							>{views.taskCausality.evidenceRecords} records</small
						>
					</div>
					<div>
						<span>Authority</span><strong>{views.authority.activeLeases}</strong><small
							>{views.authority.policyDecisions} decisions</small
						>
					</div>
					<div>
						<span>Last snapshot</span><strong>{relativeTime(lastUpdatedAt)}</strong><small
							>{streamStatusLabel}</small
						>
					</div>
				</div>

				<details class="task-details">
					<summary>Task identity</summary>
					<div class="task-detail-grid">
						<div><span>Source</span><strong>{titleCase(snapshot.task.source)}</strong></div>
						<div><span>Task</span><code>{snapshot.task.taskId}</code></div>
						<div><span>Workspace</span><code>{snapshot.task.workspaceId ?? '—'}</code></div>
						<div><span>Updated</span><strong>{relativeTime(lastUpdatedAt)}</strong></div>
					</div>
				</details>
			{/if}
		</section>

		<nav class="section-rail" aria-label="Capability OS operator sections">
			{#each sections as section}<button type="button" onclick={() => scrollToSection(section[0])}
					>{section[1]}</button
				>{/each}
		</nav>

		{#if views && snapshot}
			<div class="operator-grid">
				<section
					id="capability-health"
					class="operator-card health-card card-span-5 section-anchor"
				>
					<header class="card-heading">
						<div>
							<span class="card-kicker">Inventory</span>
							<h3>Capability health</h3>
						</div>
						<span class="card-badge">{views.capabilityHealth.artifacts} artifacts</span>
					</header>
					<div class="capability-visual">
						<div class="capability-ring" style={`--progress:${qualificationPercent}%`}>
							<div><strong>{qualificationPercent}%</strong><span>qualified</span></div>
						</div>
						<div class="health-list">
							{#if artifactEntries.length === 0}<div class="visual-empty">
									No artifact state
								</div>{/if}{#each artifactEntries.slice(0, 5) as [state, count]}<div
									class="health-row"
								>
									<div><span>{titleCase(state)}</span><strong>{count}</strong></div>
									<div class="health-track" aria-hidden="true">
										<span style={`width:${statePercent(count)}%`}></span>
									</div>
								</div>{/each}
						</div>
					</div>
					<details class="micro-details">
						<summary>Artifact kinds</summary>
						<div class="chip-grid">
							{#each Object.entries(views.capabilityHealth.byKind).sort((a, b) => b[1] - a[1]) as [kind, count]}<span
									><em>{titleCase(kind)}</em><strong>{count}</strong></span
								>{/each}
						</div>
					</details>
				</section>

				<section
					id="task-causality"
					class="operator-card causality-card card-span-7 section-anchor"
				>
					<header class="card-heading">
						<div>
							<span class="card-kicker">Traceability</span>
							<h3>Task causality</h3>
						</div>
						<span class="health-pill" data-good={chainHealthy}
							>{chainHealthy ? 'Chain intact' : 'Review chain'}</span
						>
					</header>
					<div class="causality-line" aria-label="Task causality flow">
						<div class="causality-stop primary">
							<span>Task</span><strong>{titleCase(snapshot.task.source)}</strong><small
								>{shortId(snapshot.task.taskId)}</small
							>
						</div>
						<span class="causality-link" aria-hidden="true"></span>
						<div class="causality-stop" data-live={views.taskCausality.activeRuns > 0}>
							<span>Runs</span><strong>{views.taskCausality.runs}</strong><small
								>{views.taskCausality.activeRuns} active</small
							>
						</div>
						<span class="causality-link" aria-hidden="true"></span>
						<div class="causality-stop">
							<span>Evidence</span><strong>{views.taskCausality.evidenceRecords}</strong><small
								>records</small
							>
						</div>
						<span class="causality-link" aria-hidden="true"></span>
						<div class="causality-stop">
							<span>Observed</span><strong>{views.taskCausality.observations}</strong><small
								>relations</small
							>
						</div>
					</div>
					{#if evidenceEntries.length}<details class="micro-details evidence-details">
							<summary>Evidence mix</summary>
							<div class="chip-grid">
								{#each evidenceEntries as [kind, count]}<span
										><em>{titleCase(kind)}</em><strong>{count}</strong></span
									>{/each}
							</div>
						</details>{/if}
				</section>

				<section id="forge" class="operator-card forge-card card-span-4 section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Build system</span>
							<h3>Forge</h3>
						</div>
						<span class="forge-glyph" data-live={views.forge.runs > 0} aria-hidden="true">◇</span>
					</header>
					<div class="forge-visual">
						<div class="forge-core"><strong>{views.forge.tools}</strong><span>tools</span></div>
						<div class="metric-quadrant">
							<div><span>Builds</span><strong>{views.forge.builds}</strong></div>
							<div><span>Runs</span><strong>{views.forge.runs}</strong></div>
							<div data-danger={views.forge.failures > 0}>
								<span>Failures</span><strong>{views.forge.failures}</strong>
							</div>
							<div>
								<span>Supply chain</span><strong>{views.releases.supplyChainBuilds}</strong>
							</div>
						</div>
					</div>
				</section>

				<section id="mcp-fabric" class="operator-card card-span-4 section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Acquisition</span>
							<h3>MCP fabric</h3>
						</div>
						<span class="health-pill" data-good={views.mcpFabric.activeMounts > 0}
							>{views.mcpFabric.activeMounts} mounted</span
						>
					</header>
					<div class="live-list">
						{#if snapshot.activeMounts.length === 0}<div class="visual-empty">
								<span class="empty-orb" aria-hidden="true"></span>No active mount
							</div>{:else}{#each snapshot.activeMounts.slice(0, 4) as mount (mount.mountId)}<div
									class="live-row"
								>
									<span class="live-beacon" aria-hidden="true"></span>
									<div>
										<strong>{mount.serverId}</strong><small
											>{mount.projectedTools.length} tools</small
										>
									</div>
									<code>{shortId(mount.mountId, 9)}</code>
								</div>{/each}{/if}
					</div>
					<div class="card-footer">
						<span>{views.mcpFabric.adapters} adapters</span><span
							>{views.mcpFabric.events} events</span
						>
					</div>
				</section>

				<section id="authority" class="operator-card card-span-4 section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Least privilege</span>
							<h3>Authority</h3>
						</div>
						<span class="card-badge">{views.authority.policyDecisions} decisions</span>
					</header>
					<div class="live-list">
						{#if snapshot.activeLeases.length === 0}<div class="visual-empty">
								<span class="empty-orb" aria-hidden="true"></span>No active lease
							</div>{:else}{#each snapshot.activeLeases.slice(0, 4) as lease (lease.leaseId)}<div
									class="live-row"
								>
									<span class="live-beacon" aria-hidden="true"></span>
									<div>
										<strong>{titleCase(lease.runtimeProfile)}</strong><small
											>{lease.permissions.length} permissions</small
										>
									</div>
									<span class="expiry">{leaseRemaining(lease.expiresAtMs)}</span>
								</div>{/each}{/if}
					</div>
					<div class="card-footer">
						<span>Server authoritative</span><span>{views.authority.activeLeases} active</span>
					</div>
				</section>

				<section id="sandbox" class="operator-card sandbox-card card-span-6 section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Isolation</span>
							<h3>Sandbox runtime</h3>
						</div>
						<span class="health-pill" data-good={availableRuntimeCount > 0}
							>{availableRuntimeCount}/{runtimeEntries.length} flags</span
						>
					</header>
					<div class="runtime-map">
						{#each runtimeEntries as [runtime, available]}<div
								class="runtime-node"
								data-available={available}
							>
								<span class="runtime-glyph" aria-hidden="true">{available ? '◆' : '◇'}</span>
								<div>
									<strong>{titleCase(runtime)}</strong><small
										>{available ? 'Available' : 'Unavailable'}</small
									>
								</div>
							</div>{/each}
					</div>
				</section>

				<section
					id="skill-evolution"
					class="operator-card evolution-card card-span-3 section-anchor"
				>
					<header class="card-heading compact-heading">
						<div>
							<span class="card-kicker">Reusable intelligence</span>
							<h3>Skills</h3>
						</div>
						<strong class="heading-number">{views.skillEvolution.skills}</strong>
					</header>
					<div class="vertical-metrics">
						<div><span>Evaluations</span><strong>{views.skillEvolution.evaluations}</strong></div>
						<div><span>Promotions</span><strong>{views.skillEvolution.promotions}</strong></div>
					</div>
				</section>

				<section id="evolution" class="operator-card evolution-card card-span-3 section-anchor">
					<header class="card-heading compact-heading">
						<div>
							<span class="card-kicker">Matched evaluation</span>
							<h3>Evolution</h3>
						</div>
						<strong class="heading-number">{views.evolution.experiments}</strong>
					</header>
					<div class="evolution-visual">
						<div class="evolution-ring" style={`--progress:${evolutionPercent}%`}>
							<div><strong>{views.evolution.activeExperiments}</strong><span>active</span></div>
						</div>
						<div class="evolution-mini">
							<span>{views.evolution.events} events</span><span
								>{views.evolution.promotions} promotions</span
							>
						</div>
					</div>
				</section>

				<section id="releases" class="operator-card release-card card-span-12 section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Promotion state</span>
							<h3>Release ladder</h3>
						</div>
						<span class="card-badge">{views.releases.supplyChainBuilds} supply-chain builds</span>
					</header>
					<div class="release-ladder">
						<div class="release-tier learned">
							<span>Learned+</span><strong>{views.releases.learnedOrHigher}</strong><i
								aria-hidden="true"
							></i>
						</div>
						<span class="ladder-link" aria-hidden="true"></span>
						<div class="release-tier certified">
							<span>Certified</span><strong>{views.releases.certified}</strong><i aria-hidden="true"
							></i>
						</div>
						<span class="ladder-link" aria-hidden="true"></span>
						<div class="release-tier core">
							<span>Core</span><strong>{views.releases.core}</strong><i aria-hidden="true"></i>
						</div>
					</div>
				</section>
			</div>
		{/if}
	</div>
</div>

<style>
	.operator-root {
		--cos-success: color-mix(in oklab, var(--app-accent) 34%, #22c881);
		--cos-danger: color-mix(in oklab, var(--app-fg) 12%, #df5965);
		--cos-orbit: color-mix(in oklab, var(--app-accent) 28%, var(--app-border));
		height: 100%;
		min-height: 0;
		overflow: auto;
		overscroll-behavior: contain;
		-webkit-overflow-scrolling: touch;
		background:
			radial-gradient(
				circle at 18% -6rem,
				color-mix(in oklab, var(--app-accent) 13%, transparent),
				transparent 32rem
			),
			radial-gradient(
				circle at 88% 24%,
				color-mix(in oklab, var(--app-accent) 7%, transparent),
				transparent 28rem
			),
			var(--app-bg);
	}
	.operator-shell {
		width: min(100%, 108rem);
		margin: 0 auto;
		padding: clamp(0.7rem, 1.7vw, 1.35rem);
		padding-left: max(clamp(0.7rem, 1.7vw, 1.35rem), env(safe-area-inset-left, 0px));
		padding-right: max(clamp(0.7rem, 1.7vw, 1.35rem), env(safe-area-inset-right, 0px));
		padding-bottom: max(1.6rem, calc(env(safe-area-inset-bottom, 0px) + 0.9rem));
	}
	.operator-hero {
		position: relative;
		overflow: hidden;
		border: 1px solid color-mix(in oklab, var(--app-accent) 24%, var(--app-border));
		border-radius: 1.35rem;
		background:
			linear-gradient(
				145deg,
				color-mix(in oklab, var(--app-surface) 92%, var(--app-accent) 3%),
				color-mix(in oklab, var(--app-surface) 97%, transparent)
			),
			var(--app-surface);
		box-shadow: 0 1.25rem 4rem color-mix(in oklab, var(--app-bg) 67%, transparent);
	}
	.ambient-grid,
	.stage-grid {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background-image:
			linear-gradient(color-mix(in oklab, var(--app-border) 38%, transparent) 1px, transparent 1px),
			linear-gradient(
				90deg,
				color-mix(in oklab, var(--app-border) 38%, transparent) 1px,
				transparent 1px
			);
		background-size: 2.6rem 2.6rem;
		mask-image: linear-gradient(to bottom, rgba(0, 0, 0, 0.58), transparent 88%);
		opacity: 0.28;
	}
	.ambient-glow {
		position: absolute;
		border-radius: 999px;
		pointer-events: none;
	}
	.ambient-glow-one {
		top: -10rem;
		right: 4%;
		width: 28rem;
		height: 28rem;
		background: radial-gradient(
			circle,
			color-mix(in oklab, var(--app-accent) 18%, transparent),
			transparent 70%
		);
	}
	.ambient-glow-two {
		bottom: -12rem;
		left: 10%;
		width: 24rem;
		height: 24rem;
		background: radial-gradient(
			circle,
			color-mix(in oklab, var(--cos-success) 9%, transparent),
			transparent 68%
		);
	}
	.hero-toolbar {
		position: relative;
		z-index: 4;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(19rem, 32rem);
		gap: 1.25rem;
		align-items: end;
		padding: clamp(1rem, 2vw, 1.45rem);
	}
	.hero-title h2 {
		margin-top: 0.48rem;
		font-size: clamp(1.55rem, 3.3vw, 2.75rem);
		font-weight: 790;
		line-height: 0.98;
		letter-spacing: -0.048em;
	}
	.hero-title p {
		max-width: 47rem;
		margin-top: 0.52rem;
		font-size: 0.76rem;
		line-height: 1.55;
		color: var(--app-fg-muted);
	}
	.eyebrow-row {
		display: flex;
		align-items: center;
		gap: 0.46rem;
		min-height: 1.6rem;
		font-size: 0.59rem;
		font-weight: 760;
		letter-spacing: 0.11em;
		text-transform: uppercase;
		color: var(--app-fg-muted);
	}
	.eyebrow-mark {
		width: 0.45rem;
		height: 0.45rem;
		border-radius: 999px;
		background: var(--app-accent);
		box-shadow: 0 0 0 0.24rem var(--app-accent-soft);
	}
	.stream-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.34rem;
		margin-left: 0.2rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.23rem 0.48rem;
		letter-spacing: 0.05em;
		color: var(--app-fg-muted);
		background: color-mix(in oklab, var(--app-surface-raised) 78%, transparent);
	}
	.stream-dot {
		width: 0.38rem;
		height: 0.38rem;
		border-radius: 999px;
		background: var(--app-fg-subtle);
	}
	.stream-pill[data-status='live'] {
		border-color: color-mix(in oklab, var(--cos-success) 38%, var(--app-border));
		color: var(--cos-success);
	}
	.stream-pill[data-status='live'] .stream-dot {
		background: var(--cos-success);
		animation: live-pulse 1.8s ease-out infinite;
	}
	.stream-pill[data-status='reconnecting'],
	.stream-pill[data-status='connecting'] {
		border-color: color-mix(in oklab, var(--app-accent) 42%, var(--app-border));
		color: var(--app-accent);
	}
	.stream-pill[data-status='reconnecting'] .stream-dot,
	.stream-pill[data-status='connecting'] .stream-dot {
		background: var(--app-accent);
		animation: connect-blink 1s ease-in-out infinite;
	}
	.stream-pill[data-status='error'] {
		border-color: color-mix(in oklab, var(--cos-danger) 42%, var(--app-border));
		color: var(--cos-danger);
	}
	.stream-pill[data-status='error'] .stream-dot {
		background: var(--cos-danger);
	}
	.hero-controls {
		display: flex;
		align-items: end;
		gap: 0.62rem;
	}
	.task-picker {
		display: grid;
		min-width: 0;
		flex: 1;
		gap: 0.34rem;
	}
	.task-picker > span {
		font-size: 0.56rem;
		font-weight: 730;
		letter-spacing: 0.09em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.task-picker select,
	.refresh-button,
	.section-rail button,
	.state-card button,
	.orbit-node {
		min-height: 44px;
		font: inherit;
	}
	.task-picker select {
		width: 100%;
		min-width: 0;
		border: 1px solid var(--app-border);
		border-radius: 0.78rem;
		padding: 0 2.25rem 0 0.72rem;
		background: color-mix(in oklab, var(--app-surface-raised) 92%, transparent);
		color: var(--app-fg);
		font-size: 0.69rem;
		font-weight: 650;
	}
	.refresh-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.42rem;
		border: 1px solid color-mix(in oklab, var(--app-accent) 45%, var(--app-border));
		border-radius: 0.78rem;
		padding: 0 0.78rem;
		background: var(--app-accent);
		color: var(--app-accent-contrast, white);
		font-size: 0.66rem;
		font-weight: 760;
	}
	.refresh-button svg {
		width: 0.95rem;
		height: 0.95rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.8;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.refresh-button:disabled,
	.task-picker select:disabled {
		opacity: 0.52;
	}
	.stream-note {
		position: relative;
		z-index: 4;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin: 0 1rem;
		border-top: 1px solid var(--app-divider);
		padding: 0.55rem 0.15rem;
		font-size: 0.61rem;
		color: var(--app-fg-muted);
	}
	.stream-note > span {
		width: 0.4rem;
		height: 0.4rem;
		border-radius: 999px;
		background: var(--app-accent);
	}
	.stream-note[data-status='error'] > span {
		background: var(--cos-danger);
	}
	.spatial-stage {
		position: relative;
		z-index: 2;
		min-height: clamp(27rem, 49vw, 39rem);
		margin: 0 clamp(0.45rem, 1vw, 0.9rem);
		overflow: hidden;
		border-top: 1px solid var(--app-divider);
	}
	.stage-grid {
		inset: 0 7%;
		mask-image: radial-gradient(circle at center, black, transparent 76%);
		opacity: 0.2;
	}
	.orbit {
		position: absolute;
		top: 50%;
		left: 50%;
		border: 1px solid var(--cos-orbit);
		border-radius: 50%;
		pointer-events: none;
		transform: translate(-50%, -50%) rotate(-12deg);
	}
	.orbit::after {
		position: absolute;
		top: -0.22rem;
		left: 52%;
		width: 0.42rem;
		height: 0.42rem;
		border-radius: 50%;
		content: '';
		background: var(--app-accent);
		box-shadow: 0 0 1rem color-mix(in oklab, var(--app-accent) 70%, transparent);
	}
	.orbit-outer {
		width: min(72vw, 46rem);
		aspect-ratio: 1.85;
		animation: orbit-drift 22s ease-in-out infinite alternate;
	}
	.orbit-inner {
		width: min(52vw, 31rem);
		aspect-ratio: 1.6;
		border-style: dashed;
		opacity: 0.68;
		transform: translate(-50%, -50%) rotate(18deg);
		animation: orbit-drift-inverse 18s ease-in-out infinite alternate;
	}
	.orbit-sweep {
		position: absolute;
		top: 50%;
		left: 50%;
		width: min(65vw, 40rem);
		aspect-ratio: 1;
		border-radius: 50%;
		pointer-events: none;
		background: conic-gradient(
			from 230deg,
			transparent 0 77%,
			color-mix(in oklab, var(--app-accent) 11%, transparent) 83%,
			transparent 90%
		);
		transform: translate(-50%, -50%);
		animation: sweep-rotate 26s linear infinite;
	}
	.task-core {
		position: absolute;
		top: 50%;
		left: 50%;
		display: grid;
		width: clamp(10.8rem, 18vw, 14.5rem);
		aspect-ratio: 1;
		place-content: center;
		place-items: center;
		border: 1px solid color-mix(in oklab, var(--app-accent) 48%, var(--app-border));
		border-radius: 50%;
		padding: 1.1rem;
		background:
			radial-gradient(
				circle at 40% 35%,
				color-mix(in oklab, var(--app-accent) 14%, transparent),
				transparent 48%
			),
			color-mix(in oklab, var(--app-surface-raised) 94%, transparent);
		box-shadow:
			0 0 0 0.6rem color-mix(in oklab, var(--app-accent) 4%, transparent),
			0 0 3.5rem color-mix(in oklab, var(--app-accent) 17%, transparent),
			inset 0 0 2.5rem color-mix(in oklab, var(--app-accent) 7%, transparent);
		text-align: center;
		transform: translate(-50%, -50%);
	}
	.task-core[data-running='true'] {
		animation: core-breathe 2.4s ease-in-out infinite;
	}
	.core-halo {
		position: absolute;
		inset: -0.75rem;
		border: 1px solid color-mix(in oklab, var(--app-accent) 22%, transparent);
		border-radius: 50%;
		pointer-events: none;
	}
	.core-halo-two {
		inset: -1.55rem;
		border-style: dashed;
		opacity: 0.55;
		animation: halo-spin 20s linear infinite;
	}
	.core-label {
		font-size: 0.55rem;
		font-weight: 760;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.task-core > strong {
		margin-top: 0.36rem;
		font-size: clamp(1.15rem, 2.5vw, 1.62rem);
		font-weight: 790;
		line-height: 1;
		letter-spacing: -0.04em;
	}
	.task-core > code {
		margin-top: 0.44rem;
		font-size: 0.55rem;
		color: var(--app-fg-muted);
	}
	.core-state-row {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.28rem;
		margin-top: 0.58rem;
		font-size: 0.5rem;
		color: var(--app-fg-muted);
	}
	.core-state-dot {
		width: 0.38rem;
		height: 0.38rem;
		border-radius: 50%;
		background: var(--app-fg-subtle);
	}
	.task-core[data-state='ready'] .core-state-dot,
	.task-core[data-state='running'] .core-state-dot {
		background: var(--cos-success);
	}
	.task-core[data-state='blocked'] .core-state-dot {
		background: var(--cos-danger);
	}
	.core-separator {
		color: var(--app-fg-subtle);
	}
	.orbit-node {
		position: absolute;
		z-index: 3;
		display: grid;
		min-width: 7.1rem;
		min-height: 4.3rem;
		place-content: center;
		justify-items: center;
		gap: 0.08rem;
		border: 1px solid color-mix(in oklab, var(--app-border) 88%, var(--app-accent) 12%);
		border-radius: 0.86rem;
		padding: 0.48rem 0.65rem;
		background: color-mix(in oklab, var(--app-surface-raised) 91%, transparent);
		box-shadow: 0 0.65rem 1.8rem color-mix(in oklab, var(--app-bg) 50%, transparent);
		color: var(--app-fg);
		cursor: pointer;
		backdrop-filter: blur(10px);
		transition:
			transform 180ms ease,
			border-color 180ms ease,
			background 180ms ease;
	}
	.orbit-node::before {
		position: absolute;
		inset: -0.35rem;
		border: 1px solid transparent;
		border-radius: 1rem;
		content: '';
		pointer-events: none;
	}
	.orbit-node[data-live='true']::before {
		border-color: color-mix(in oklab, var(--cos-success) 26%, transparent);
		animation: node-pulse 2s ease-out infinite;
	}
	.orbit-node strong {
		font-size: 1.05rem;
		font-weight: 780;
		line-height: 1;
	}
	.orbit-node small {
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.node-icon {
		display: grid;
		width: 1.4rem;
		height: 1.4rem;
		place-items: center;
		border-radius: 0.46rem;
		background: var(--app-accent-soft);
		color: var(--app-accent);
		font-size: 0.65rem;
		font-weight: 760;
	}
	.node-runs {
		top: 13%;
		left: 18%;
	}
	.node-forge {
		top: 12%;
		right: 18%;
	}
	.node-mcp {
		top: 43%;
		right: 7%;
	}
	.node-authority {
		bottom: 11%;
		right: 20%;
	}
	.node-runtime {
		bottom: 11%;
		left: 20%;
	}
	.node-evidence {
		top: 43%;
		left: 7%;
	}
	.signal-strip {
		position: absolute;
		z-index: 4;
		left: 50%;
		bottom: 0.6rem;
		display: grid;
		width: min(92%, 56rem);
		grid-template-columns: repeat(4, minmax(0, 1fr));
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
		background: color-mix(in oklab, var(--app-surface-raised) 91%, transparent);
		box-shadow: 0 0.8rem 2rem color-mix(in oklab, var(--app-bg) 52%, transparent);
		transform: translateX(-50%);
		backdrop-filter: blur(12px);
	}
	.signal-strip > div {
		display: grid;
		min-width: 0;
		gap: 0.16rem;
		border-right: 1px solid var(--app-divider);
		padding: 0.7rem 0.78rem;
	}
	.signal-strip > div:last-child {
		border-right: 0;
	}
	.signal-strip span,
	.signal-strip small {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.signal-strip > div > span {
		font-weight: 720;
		letter-spacing: 0.07em;
		text-transform: uppercase;
	}
	.signal-strip strong {
		font-size: 0.88rem;
		font-weight: 760;
	}
	.signal-strip [data-good='true'] strong {
		color: var(--cos-success);
	}
	.signal-strip [data-good='false'] strong {
		color: var(--cos-danger);
	}
	.task-details {
		position: relative;
		z-index: 5;
		border-top: 1px solid var(--app-divider);
		background: color-mix(in oklab, var(--app-surface) 96%, transparent);
	}
	.task-details summary,
	.micro-details summary {
		min-height: 2.5rem;
		cursor: pointer;
		list-style: none;
		font-size: 0.58rem;
		font-weight: 700;
		color: var(--app-fg-muted);
	}
	.task-details summary {
		display: flex;
		align-items: center;
		padding: 0 1rem;
	}
	.task-details summary::-webkit-details-marker,
	.micro-details summary::-webkit-details-marker {
		display: none;
	}
	.task-details summary::after,
	.micro-details summary::after {
		margin-left: auto;
		content: '+';
		color: var(--app-fg-subtle);
	}
	.task-details[open] summary::after,
	.micro-details[open] summary::after {
		content: '–';
	}
	.task-detail-grid {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.6rem;
		border-top: 1px solid var(--app-divider);
		padding: 0.7rem 1rem 0.85rem;
	}
	.task-detail-grid > div {
		display: grid;
		min-width: 0;
		gap: 0.14rem;
	}
	.task-detail-grid span {
		font-size: 0.5rem;
		font-weight: 700;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.task-detail-grid strong,
	.task-detail-grid code {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.6rem;
	}
	.section-rail {
		display: flex;
		gap: 0.38rem;
		margin: 0.78rem 0;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.section-rail::-webkit-scrollbar {
		display: none;
	}
	.section-rail button {
		flex: 0 0 auto;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0 0.78rem;
		background: color-mix(in oklab, var(--app-surface) 88%, transparent);
		color: var(--app-fg-muted);
		font-size: 0.61rem;
		font-weight: 670;
	}
	.operator-grid {
		display: grid;
		grid-template-columns: repeat(12, minmax(0, 1fr));
		gap: 0.72rem;
	}
	.operator-card {
		min-width: 0;
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 1rem;
		background: color-mix(in oklab, var(--app-surface) 93%, transparent);
		box-shadow: 0 0.55rem 1.6rem color-mix(in oklab, var(--app-bg) 61%, transparent);
		content-visibility: auto;
		contain-intrinsic-size: 18rem;
		animation: card-enter 320ms ease both;
	}
	.card-span-3 {
		grid-column: span 3;
	}
	.card-span-4 {
		grid-column: span 4;
	}
	.card-span-5 {
		grid-column: span 5;
	}
	.card-span-6 {
		grid-column: span 6;
	}
	.card-span-7 {
		grid-column: span 7;
	}
	.card-span-12 {
		grid-column: span 12;
	}
	.section-anchor {
		scroll-margin-top: 0.8rem;
	}
	.card-heading {
		display: flex;
		min-height: 4rem;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.68rem 0.82rem;
	}
	.compact-heading {
		min-height: 3.75rem;
	}
	.card-kicker {
		display: block;
		font-size: 0.51rem;
		font-weight: 760;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.card-heading h3 {
		margin-top: 0.12rem;
		font-size: 0.81rem;
		font-weight: 730;
	}
	.heading-number {
		font-size: 1.22rem;
		font-weight: 780;
		letter-spacing: -0.04em;
	}
	.card-badge,
	.health-pill {
		display: inline-flex;
		min-height: 1.65rem;
		align-items: center;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0 0.5rem;
		font-size: 0.55rem;
		font-weight: 720;
		color: var(--app-fg-muted);
	}
	.health-pill[data-good='true'] {
		border-color: color-mix(in oklab, var(--cos-success) 30%, var(--app-border));
		background: color-mix(in oklab, var(--cos-success) 7%, transparent);
		color: var(--cos-success);
	}
	.capability-visual {
		display: grid;
		grid-template-columns: minmax(7rem, 0.72fr) minmax(0, 1.28fr);
		align-items: center;
		gap: 0.8rem;
		padding: 0.86rem;
	}
	.capability-ring,
	.evolution-ring {
		display: grid;
		place-items: center;
		border-radius: 50%;
		background: conic-gradient(var(--app-accent) var(--progress), var(--app-border) 0);
	}
	.capability-ring {
		width: min(100%, 8.4rem);
		aspect-ratio: 1;
		justify-self: center;
	}
	.capability-ring::before,
	.evolution-ring::before {
		grid-area: 1 / 1;
		border-radius: 50%;
		content: '';
		background: var(--app-surface);
	}
	.capability-ring::before {
		width: 6.65rem;
		aspect-ratio: 1;
	}
	.capability-ring > div,
	.evolution-ring > div {
		z-index: 1;
		display: grid;
		grid-area: 1 / 1;
		text-align: center;
	}
	.capability-ring strong {
		font-size: 1.35rem;
		font-weight: 790;
	}
	.capability-ring span,
	.evolution-ring span {
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.health-list {
		display: grid;
		gap: 0.64rem;
	}
	.health-row > div:first-child {
		display: flex;
		justify-content: space-between;
		gap: 0.7rem;
		font-size: 0.59rem;
	}
	.health-row span {
		color: var(--app-fg-muted);
	}
	.health-track {
		height: 0.28rem;
		margin-top: 0.3rem;
		overflow: hidden;
		border-radius: 999px;
		background: var(--app-border);
	}
	.health-track span {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: linear-gradient(
			90deg,
			var(--app-accent),
			color-mix(in oklab, var(--app-accent) 54%, var(--cos-success))
		);
		transition: width 260ms ease;
	}
	.micro-details {
		border-top: 1px solid var(--app-divider);
	}
	.micro-details summary {
		display: flex;
		align-items: center;
		padding: 0 0.82rem;
	}
	.chip-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.4rem;
		border-top: 1px solid var(--app-divider);
		padding: 0.7rem 0.82rem 0.82rem;
	}
	.chip-grid > span {
		display: flex;
		min-width: 0;
		align-items: center;
		justify-content: space-between;
		gap: 0.55rem;
		border: 1px solid var(--app-border);
		border-radius: 0.58rem;
		padding: 0.46rem 0.55rem;
		background: var(--app-surface-subtle);
	}
	.chip-grid em {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.53rem;
		font-style: normal;
		color: var(--app-fg-muted);
	}
	.chip-grid strong {
		font-size: 0.62rem;
	}
	.causality-line {
		display: grid;
		grid-template-columns:
			minmax(5.5rem, 1fr) minmax(1.2rem, auto) minmax(5.5rem, 0.8fr) minmax(1.2rem, auto)
			minmax(5.5rem, 0.8fr) minmax(1.2rem, auto) minmax(5.5rem, 0.8fr);
		align-items: center;
		gap: 0.35rem;
		padding: 1rem 0.84rem;
	}
	.causality-stop {
		position: relative;
		display: grid;
		min-width: 0;
		min-height: 5rem;
		align-content: center;
		gap: 0.13rem;
		border: 1px solid var(--app-border);
		border-radius: 0.72rem;
		padding: 0.55rem;
		background: var(--app-surface-subtle);
	}
	.causality-stop.primary {
		border-color: color-mix(in oklab, var(--app-accent) 34%, var(--app-border));
		background: color-mix(in oklab, var(--app-accent-soft) 44%, var(--app-surface));
	}
	.causality-stop[data-live='true']::after {
		position: absolute;
		top: 0.5rem;
		right: 0.5rem;
		width: 0.38rem;
		height: 0.38rem;
		border-radius: 50%;
		content: '';
		background: var(--cos-success);
		animation: live-pulse 1.8s ease-out infinite;
	}
	.causality-stop span,
	.causality-stop small {
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.causality-stop strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.78rem;
	}
	.causality-link {
		height: 1px;
		background: linear-gradient(90deg, var(--app-border), var(--app-accent), var(--app-border));
	}
	.forge-glyph {
		display: grid;
		width: 2rem;
		height: 2rem;
		place-items: center;
		border: 1px solid color-mix(in oklab, var(--app-accent) 40%, var(--app-border));
		border-radius: 0.6rem;
		background: var(--app-accent-soft);
		color: var(--app-accent);
	}
	.forge-glyph[data-live='true'] {
		animation: forge-spin 4s linear infinite;
	}
	.forge-visual {
		display: grid;
		grid-template-columns: 6.8rem minmax(0, 1fr);
		align-items: stretch;
		min-height: 10.6rem;
	}
	.forge-core {
		display: grid;
		place-content: center;
		text-align: center;
		border-right: 1px solid var(--app-divider);
		background: radial-gradient(
			circle,
			color-mix(in oklab, var(--app-accent) 12%, transparent),
			transparent 64%
		);
	}
	.forge-core strong {
		font-size: 1.55rem;
		font-weight: 790;
	}
	.forge-core span {
		font-size: 0.54rem;
		color: var(--app-fg-muted);
	}
	.metric-quadrant {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	.metric-quadrant > div {
		display: grid;
		align-content: center;
		gap: 0.12rem;
		border-right: 1px solid var(--app-divider);
		border-bottom: 1px solid var(--app-divider);
		padding: 0.55rem 0.62rem;
	}
	.metric-quadrant > div:nth-child(2n) {
		border-right: 0;
	}
	.metric-quadrant > div:nth-last-child(-n + 2) {
		border-bottom: 0;
	}
	.metric-quadrant span {
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.metric-quadrant strong {
		font-size: 0.92rem;
	}
	.metric-quadrant [data-danger='true'] strong {
		color: var(--cos-danger);
	}
	.live-list {
		min-height: 9.5rem;
	}
	.live-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 0.58rem;
		min-height: 3.05rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.5rem 0.78rem;
	}
	.live-row:last-child {
		border-bottom: 0;
	}
	.live-row > div {
		display: grid;
		min-width: 0;
		gap: 0.08rem;
	}
	.live-row strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.64rem;
	}
	.live-row small,
	.live-row code,
	.expiry {
		font-size: 0.53rem;
		color: var(--app-fg-muted);
	}
	.live-beacon {
		width: 0.44rem;
		height: 0.44rem;
		border-radius: 50%;
		background: var(--cos-success);
		box-shadow: 0 0 0 0.16rem color-mix(in oklab, var(--cos-success) 11%, transparent);
		animation: live-pulse 2s ease-out infinite;
	}
	.card-footer {
		display: flex;
		min-height: 2.35rem;
		align-items: center;
		justify-content: space-between;
		gap: 0.6rem;
		border-top: 1px solid var(--app-divider);
		padding: 0 0.78rem;
		font-size: 0.53rem;
		color: var(--app-fg-subtle);
	}
	.runtime-map {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 0.55rem;
		padding: 0.82rem;
	}
	.runtime-node {
		display: flex;
		min-width: 0;
		min-height: 4.4rem;
		align-items: center;
		gap: 0.58rem;
		border: 1px solid var(--app-border);
		border-radius: 0.72rem;
		padding: 0.58rem;
		background: var(--app-surface-subtle);
	}
	.runtime-node[data-available='true'] {
		border-color: color-mix(in oklab, var(--cos-success) 26%, var(--app-border));
		background: color-mix(in oklab, var(--cos-success) 4%, var(--app-surface-subtle));
	}
	.runtime-glyph {
		display: grid;
		width: 1.55rem;
		height: 1.55rem;
		flex: 0 0 auto;
		place-items: center;
		border-radius: 0.48rem;
		background: var(--app-hover);
		font-size: 0.65rem;
		color: var(--app-fg-subtle);
	}
	.runtime-node[data-available='true'] .runtime-glyph {
		background: color-mix(in oklab, var(--cos-success) 11%, transparent);
		color: var(--cos-success);
	}
	.runtime-node > div {
		display: grid;
		min-width: 0;
		gap: 0.08rem;
	}
	.runtime-node strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.61rem;
	}
	.runtime-node small {
		font-size: 0.5rem;
		color: var(--app-fg-muted);
	}
	.vertical-metrics {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		min-height: 8.4rem;
	}
	.vertical-metrics > div {
		display: grid;
		place-content: center;
		gap: 0.22rem;
		border-right: 1px solid var(--app-divider);
		text-align: center;
	}
	.vertical-metrics > div:last-child {
		border-right: 0;
	}
	.vertical-metrics span {
		font-size: 0.52rem;
		color: var(--app-fg-muted);
	}
	.vertical-metrics strong {
		font-size: 1.3rem;
		font-weight: 780;
	}
	.evolution-visual {
		display: grid;
		min-height: 8.4rem;
		grid-template-columns: 6.8rem minmax(0, 1fr);
		align-items: center;
		gap: 0.55rem;
		padding: 0.65rem;
	}
	.evolution-ring {
		width: 5.7rem;
		aspect-ratio: 1;
		justify-self: center;
	}
	.evolution-ring::before {
		width: 4.45rem;
		aspect-ratio: 1;
	}
	.evolution-ring strong {
		font-size: 1.2rem;
		font-weight: 780;
	}
	.evolution-mini {
		display: grid;
		gap: 0.4rem;
	}
	.evolution-mini span {
		border-bottom: 1px solid var(--app-divider);
		padding: 0.42rem 0;
		font-size: 0.54rem;
		color: var(--app-fg-muted);
	}
	.release-ladder {
		display: grid;
		grid-template-columns:
			minmax(8rem, 1fr) minmax(2rem, 0.22fr) minmax(8rem, 1fr) minmax(2rem, 0.22fr)
			minmax(8rem, 1fr);
		align-items: center;
		gap: 0.7rem;
		padding: 1.15rem;
	}
	.release-tier {
		position: relative;
		display: grid;
		min-height: 6rem;
		place-content: center;
		justify-items: center;
		gap: 0.22rem;
		border: 1px solid var(--app-border);
		border-radius: 0.82rem;
		background: var(--app-surface-subtle);
	}
	.release-tier span {
		font-size: 0.55rem;
		color: var(--app-fg-muted);
	}
	.release-tier strong {
		font-size: 1.35rem;
		font-weight: 790;
	}
	.release-tier i {
		position: absolute;
		bottom: -1px;
		left: 16%;
		width: 68%;
		height: 2px;
		border-radius: 999px;
		background: var(--app-accent);
	}
	.release-tier.certified {
		border-color: color-mix(in oklab, var(--app-accent) 30%, var(--app-border));
		background: color-mix(in oklab, var(--app-accent-soft) 35%, var(--app-surface));
	}
	.release-tier.core {
		border-color: color-mix(in oklab, var(--cos-success) 30%, var(--app-border));
		background: color-mix(in oklab, var(--cos-success) 4%, var(--app-surface));
	}
	.release-tier.core i {
		background: var(--cos-success);
	}
	.ladder-link {
		height: 1px;
		background: linear-gradient(90deg, var(--app-border), var(--app-accent), var(--app-border));
	}
	.visual-empty {
		display: flex;
		min-height: 7rem;
		align-items: center;
		justify-content: center;
		gap: 0.48rem;
		padding: 1rem;
		text-align: center;
		font-size: 0.58rem;
		color: var(--app-fg-muted);
	}
	.empty-orb {
		width: 0.48rem;
		height: 0.48rem;
		border: 1px solid var(--app-fg-subtle);
		border-radius: 50%;
	}
	.state-card {
		position: relative;
		z-index: 4;
		display: flex;
		min-height: 12rem;
		align-items: center;
		gap: 0.8rem;
		border-top: 1px solid var(--app-divider);
		padding: 1rem 1.15rem;
		background: color-mix(in oklab, var(--app-surface) 92%, transparent);
	}
	.state-icon {
		display: grid;
		width: 2.4rem;
		height: 2.4rem;
		flex: 0 0 auto;
		place-items: center;
		border-radius: 0.72rem;
		background: var(--app-accent-soft);
		color: var(--app-accent);
		font-weight: 800;
	}
	.state-card > div:nth-child(2) {
		display: grid;
		min-width: 0;
		flex: 1;
		gap: 0.2rem;
	}
	.state-card strong {
		font-size: 0.75rem;
	}
	.state-card span {
		font-size: 0.62rem;
		line-height: 1.45;
		color: var(--app-fg-muted);
	}
	.state-card button {
		border: 1px solid var(--app-border);
		border-radius: 0.72rem;
		padding: 0 0.85rem;
		background: var(--app-interactive);
		color: var(--app-fg);
		font-size: 0.62rem;
		font-weight: 720;
	}
	.error-state .state-icon {
		background: color-mix(in oklab, var(--cos-danger) 10%, transparent);
		color: var(--cos-danger);
	}
	.spatial-skeleton {
		position: relative;
		min-height: 31rem;
		border-top: 1px solid var(--app-divider);
		background: linear-gradient(
			100deg,
			var(--app-surface-subtle),
			var(--app-surface-raised),
			var(--app-surface-subtle)
		);
		background-size: 180% 100%;
		animation: operator-shimmer 1.5s linear infinite;
	}
	.skeleton-orbit,
	.skeleton-core {
		position: absolute;
		top: 50%;
		left: 50%;
		border: 1px solid var(--app-border);
		border-radius: 50%;
		transform: translate(-50%, -50%);
	}
	.skeleton-orbit {
		width: min(70vw, 44rem);
		aspect-ratio: 1.8;
	}
	.skeleton-core {
		width: 12rem;
		aspect-ratio: 1;
		background: var(--app-surface-subtle);
	}
	button:focus-visible,
	select:focus-visible,
	summary:focus-visible {
		outline: 2px solid var(--app-accent);
		outline-offset: 2px;
	}
	@media (hover: hover) and (pointer: fine) {
		.refresh-button:hover {
			filter: brightness(1.04);
		}
		.section-rail button:hover,
		.state-card button:hover {
			border-color: color-mix(in oklab, var(--app-accent) 38%, var(--app-border));
			background: var(--app-hover);
			color: var(--app-fg);
		}
		.orbit-node:hover {
			z-index: 5;
			border-color: color-mix(in oklab, var(--app-accent) 42%, var(--app-border));
			background: color-mix(in oklab, var(--app-surface-raised) 96%, var(--app-accent) 2%);
			transform: translateY(-3px);
		}
		.operator-card:hover {
			border-color: color-mix(in oklab, var(--app-accent) 20%, var(--app-border));
		}
	}
	@media (max-width: 1180px) {
		.card-span-3,
		.card-span-4 {
			grid-column: span 6;
		}
		.card-span-5,
		.card-span-7 {
			grid-column: span 12;
		}
		.node-runs {
			left: 12%;
		}
		.node-forge {
			right: 12%;
		}
		.node-mcp {
			right: 3%;
		}
		.node-evidence {
			left: 3%;
		}
		.signal-strip {
			width: 96%;
		}
	}
	@media (max-width: 860px) {
		.hero-toolbar {
			grid-template-columns: 1fr;
			align-items: stretch;
		}
		.spatial-stage {
			min-height: 36rem;
		}
		.orbit-outer {
			width: 42rem;
		}
		.orbit-inner {
			width: 28rem;
		}
		.node-runs {
			top: 9%;
			left: 8%;
		}
		.node-forge {
			top: 9%;
			right: 8%;
		}
		.node-mcp {
			top: 44%;
			right: 1%;
		}
		.node-evidence {
			top: 44%;
			left: 1%;
		}
		.node-authority {
			bottom: 17%;
			right: 9%;
		}
		.node-runtime {
			bottom: 17%;
			left: 9%;
		}
		.signal-strip {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.signal-strip > div:nth-child(2) {
			border-right: 0;
		}
		.signal-strip > div:nth-child(-n + 2) {
			border-bottom: 1px solid var(--app-divider);
		}
		.task-detail-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.runtime-map {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.causality-line {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
		.causality-link {
			display: none;
		}
		.card-span-6 {
			grid-column: span 12;
		}
	}
	@media (max-width: 639px) {
		.operator-shell {
			padding-top: 0.55rem;
		}
		.operator-hero {
			border-radius: 1rem;
			box-shadow: none;
		}
		.hero-toolbar {
			gap: 0.85rem;
			padding: 0.85rem;
		}
		.hero-title h2 {
			font-size: 1.72rem;
		}
		.hero-title p {
			font-size: 0.7rem;
		}
		.eyebrow-row {
			flex-wrap: wrap;
		}
		.hero-controls {
			align-items: stretch;
			flex-direction: column;
		}
		.refresh-button {
			width: 100%;
		}
		.stream-note {
			margin: 0 0.85rem;
		}
		.spatial-stage {
			min-height: 42rem;
			margin-inline: 0;
		}
		.task-core {
			top: 44%;
			width: 10rem;
		}
		.core-state-row {
			flex-wrap: wrap;
			max-width: 8.5rem;
		}
		.orbit-outer {
			top: 44%;
			width: 31rem;
			aspect-ratio: 1.2;
		}
		.orbit-inner {
			top: 44%;
			width: 22rem;
			aspect-ratio: 1.25;
		}
		.orbit-sweep {
			top: 44%;
			width: 26rem;
		}
		.orbit-node {
			min-width: 6.3rem;
			min-height: 4rem;
			border-radius: 0.74rem;
		}
		.node-runs {
			top: 7%;
			left: 4%;
		}
		.node-forge {
			top: 7%;
			right: 4%;
		}
		.node-evidence {
			top: 29%;
			left: 0;
		}
		.node-mcp {
			top: 29%;
			right: 0;
		}
		.node-runtime {
			bottom: 24%;
			left: 4%;
		}
		.node-authority {
			bottom: 24%;
			right: 4%;
		}
		.signal-strip {
			bottom: 0.45rem;
			width: 98%;
			border-radius: 0.76rem;
		}
		.signal-strip > div {
			padding: 0.55rem 0.58rem;
		}
		.task-details summary {
			min-height: 44px;
		}
		.task-detail-grid {
			grid-template-columns: 1fr;
		}
		.section-rail {
			margin-inline: calc(-1 * max(0.7rem, env(safe-area-inset-left, 0px)));
			padding-inline: max(0.7rem, env(safe-area-inset-left, 0px));
		}
		.section-rail button {
			min-height: 44px;
		}
		.operator-grid {
			grid-template-columns: 1fr;
			gap: 0.6rem;
		}
		.card-span-3,
		.card-span-4,
		.card-span-5,
		.card-span-6,
		.card-span-7,
		.card-span-12 {
			grid-column: 1;
		}
		.operator-card {
			border-radius: 0.86rem;
			box-shadow: none;
		}
		.capability-visual {
			grid-template-columns: 6.6rem minmax(0, 1fr);
			padding: 0.7rem;
		}
		.capability-ring {
			width: 6.4rem;
		}
		.capability-ring::before {
			width: 5rem;
		}
		.causality-line {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			padding: 0.72rem;
		}
		.causality-stop {
			min-height: 4.5rem;
		}
		.runtime-map {
			grid-template-columns: 1fr;
			padding: 0.7rem;
		}
		.forge-visual {
			grid-template-columns: 5.7rem minmax(0, 1fr);
		}
		.release-ladder {
			grid-template-columns: 1fr;
			padding: 0.75rem;
		}
		.ladder-link {
			width: 1px;
			height: 1.4rem;
			justify-self: center;
			background: linear-gradient(var(--app-border), var(--app-accent), var(--app-border));
		}
		.release-tier {
			width: 100%;
			min-height: 4.8rem;
		}
		.state-card {
			align-items: flex-start;
			flex-wrap: wrap;
		}
		.state-card button {
			width: 100%;
		}
		.spatial-skeleton {
			min-height: 25rem;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		*,
		*::before,
		*::after {
			scroll-behavior: auto !important;
			animation-duration: 0.001ms !important;
			animation-iteration-count: 1 !important;
			transition-duration: 0.001ms !important;
		}
		.orbit-sweep {
			display: none;
		}
	}
	@keyframes live-pulse {
		0% {
			box-shadow: 0 0 0 0 color-mix(in oklab, var(--cos-success) 34%, transparent);
		}
		75%,
		100% {
			box-shadow: 0 0 0 0.45rem transparent;
		}
	}
	@keyframes connect-blink {
		50% {
			opacity: 0.35;
		}
	}
	@keyframes orbit-drift {
		to {
			transform: translate(-50%, -50%) rotate(-5deg) scale(1.015);
		}
	}
	@keyframes orbit-drift-inverse {
		to {
			transform: translate(-50%, -50%) rotate(10deg) scale(0.985);
		}
	}
	@keyframes sweep-rotate {
		to {
			transform: translate(-50%, -50%) rotate(360deg);
		}
	}
	@keyframes halo-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@keyframes core-breathe {
		50% {
			box-shadow:
				0 0 0 0.8rem color-mix(in oklab, var(--app-accent) 5%, transparent),
				0 0 4.2rem color-mix(in oklab, var(--app-accent) 23%, transparent),
				inset 0 0 2.5rem color-mix(in oklab, var(--app-accent) 9%, transparent);
		}
	}
	@keyframes node-pulse {
		0% {
			opacity: 0.9;
			transform: scale(0.98);
		}
		75%,
		100% {
			opacity: 0;
			transform: scale(1.12);
		}
	}
	@keyframes forge-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@keyframes card-enter {
		from {
			opacity: 0;
			transform: translateY(6px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
	@keyframes operator-shimmer {
		to {
			background-position: -180% 0;
		}
	}
</style>
