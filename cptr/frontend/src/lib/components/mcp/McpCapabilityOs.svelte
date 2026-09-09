<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getMcpCapabilityOsOperatorSnapshot,
		getMcpCapabilityOsTasks,
		type McpCapabilityOsOperatorSnapshot,
		type McpCapabilityOsTask
	} from '$lib/apis/mcp';

	const sections = [
		['task-causality', 'Task Causality'],
		['capability-health', 'Capability Health'],
		['forge', 'Forge'],
		['skill-evolution', 'Skill Evolution'],
		['mcp-fabric', 'MCP Fabric'],
		['authority', 'Authority'],
		['sandbox', 'Sandbox'],
		['evolution', 'Evolution'],
		['releases', 'Releases']
	] as const;

	let tasks = $state<McpCapabilityOsTask[]>([]);
	let selectedTaskId = $state('');
	let snapshot = $state<McpCapabilityOsOperatorSnapshot | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let errorMessage = $state<string | null>(null);
	let lastUpdatedAt = $state<number | null>(null);
	let nowMs = $state(Date.now());
	let refreshGeneration = 0;

	const selectedTask = $derived(tasks.find((task) => task.taskId === selectedTaskId) ?? null);
	const views = $derived(snapshot?.views ?? null);
	const artifactEntries = $derived(
		Object.entries(snapshot?.artifactStates ?? {}).sort((a, b) => b[1] - a[1])
	);
	const evidenceEntries = $derived(
		Object.entries(snapshot?.evidenceKinds ?? {})
			.sort((a, b) => b[1] - a[1])
			.slice(0, 6)
	);
	const runtimeEntries = $derived(
		Object.entries(views?.sandbox.runtime ?? {}).filter(
			([, value]) => typeof value === 'boolean'
		) as Array<[string, boolean]>
	);
	const chainHealthy = $derived(isEvidenceChainHealthy(views?.taskCausality.evidenceChain));

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
		if (seconds < 10) return 'just now';
		if (seconds < 60) return `${seconds}s ago`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m ago`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `${hours}h ago`;
		return `${Math.floor(hours / 24)}d ago`;
	}

	function leaseRemaining(expiresAtMs: number): string {
		const remaining = Math.max(0, expiresAtMs - nowMs);
		if (remaining <= 0) return 'expired';
		const minutes = Math.ceil(remaining / 60_000);
		if (minutes < 60) return `${minutes}m left`;
		const hours = Math.ceil(minutes / 60);
		return `${hours}h left`;
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
		return total > 0 ? Math.max(4, Math.round((value / total) * 100)) : 0;
	}

	function scrollToSection(id: string) {
		const target = document.getElementById(id);
		if (!target) return;
		const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		target.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
	}

	async function loadTasks(): Promise<void> {
		const response = await getMcpCapabilityOsTasks(30);
		tasks = response.tasks;
		if (!selectedTaskId || !tasks.some((task) => task.taskId === selectedTaskId)) {
			selectedTaskId = tasks[0]?.taskId ?? '';
		}
	}

	async function loadSnapshot(taskId: string, quiet = false): Promise<void> {
		if (!taskId) {
			snapshot = null;
			return;
		}
		const generation = ++refreshGeneration;
		if (!quiet) refreshing = true;
		try {
			const next = await getMcpCapabilityOsOperatorSnapshot(taskId);
			if (generation !== refreshGeneration) return;
			snapshot = next;
			lastUpdatedAt = Date.now();
			errorMessage = null;
		} catch (error) {
			if (generation !== refreshGeneration) return;
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
		} finally {
			if (generation === refreshGeneration && !quiet) refreshing = false;
		}
	}

	async function bootstrap(): Promise<void> {
		loading = true;
		try {
			await loadTasks();
			if (selectedTaskId) await loadSnapshot(selectedTaskId);
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	async function selectTask(taskId: string): Promise<void> {
		if (!taskId || taskId === selectedTaskId) return;
		selectedTaskId = taskId;
		snapshot = null;
		errorMessage = null;
		await loadSnapshot(taskId);
	}

	async function refresh(): Promise<void> {
		try {
			await loadTasks();
			if (selectedTaskId) await loadSnapshot(selectedTaskId);
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Capability OS telemetry unavailable';
		}
	}

	onMount(() => {
		void bootstrap();
		const interval = window.setInterval(() => {
			nowMs = Date.now();
			if (document.visibilityState === 'visible' && selectedTaskId && !refreshing) {
				void loadSnapshot(selectedTaskId, true);
			}
		}, 5000);
		return () => {
			window.clearInterval(interval);
			refreshGeneration += 1;
		};
	});
</script>

<div class="operator-root app-theme">
	<div class="operator-shell">
		<section class="operator-hero" aria-labelledby="capability-os-heading">
			<div class="hero-glow" aria-hidden="true"></div>
			<div class="hero-copy">
				<div class="eyebrow-row">
					<span class="eyebrow-mark" aria-hidden="true"></span>
					<span>Capability OS operator</span>
					{#if selectedTask}
						<span class="hero-status" data-active={snapshot?.task.active ?? false}>
							<span></span>{snapshot?.task.status ?? selectedTask.status}
						</span>
					{/if}
				</div>
				<h2 id="capability-os-heading">Observe. Evaluate. Evolve.</h2>
				<p>
					A task-scoped operations surface for capability execution, authority, sandboxing, MCP
					acquisition, evidence, and evolution.
				</p>
			</div>

			<div class="hero-controls">
				<label class="task-picker">
					<span>Active task</span>
					<select
						value={selectedTaskId}
						disabled={loading || tasks.length === 0}
						onchange={(event) => void selectTask((event.currentTarget as HTMLSelectElement).value)}
					>
						{#if tasks.length === 0}
							<option value="">No active Capability OS task</option>
						{:else}
							{#each tasks as task (task.taskId)}
								<option value={task.taskId}>
									{task.source.toUpperCase()} · {task.label.slice(0, 74)}
								</option>
							{/each}
						{/if}
					</select>
				</label>
				<button
					class="refresh-button"
					type="button"
					disabled={refreshing}
					onclick={() => void refresh()}
				>
					<svg viewBox="0 0 24 24" aria-hidden="true">
						<path d="M20 11a8 8 0 1 0-2.34 5.66M20 4v7h-7" />
					</svg>
					<span>{refreshing ? 'Refreshing' : 'Refresh'}</span>
				</button>
			</div>

			{#if selectedTask}
				<div class="task-meta" aria-label="Selected Capability OS task">
					<div><span>Source</span><strong>{titleCase(selectedTask.source)}</strong></div>
					<div>
						<span>Task</span><code title={selectedTask.taskId}
							>{shortId(selectedTask.taskId, 18)}</code
						>
					</div>
					<div>
						<span>Workspace</span><code title={selectedTask.workspaceId ?? ''}
							>{shortId(selectedTask.workspaceId, 18)}</code
						>
					</div>
					<div>
						<span>Updated</span><strong
							>{relativeTime(lastUpdatedAt ?? selectedTask.updatedAtMs)}</strong
						>
					</div>
				</div>
			{/if}
		</section>

		<nav class="section-nav" aria-label="Capability OS operator sections">
			{#each sections as section}
				<button type="button" onclick={() => scrollToSection(section[0])}>{section[1]}</button>
			{/each}
		</nav>

		{#if errorMessage && !snapshot}
			<section class="state-card error-state" role="alert">
				<div class="state-icon">!</div>
				<div>
					<h3>Capability OS telemetry unavailable</h3>
					<p>{errorMessage}</p>
				</div>
				<button type="button" onclick={() => void refresh()}>Retry</button>
			</section>
		{:else if loading}
			<div class="skeleton-grid" aria-label="Loading Capability OS operator data" role="status">
				{#each Array(8) as _}<div class="skeleton-card"></div>{/each}
			</div>
		{:else if tasks.length === 0}
			<section class="state-card empty-state">
				<div class="state-icon">○</div>
				<div>
					<h3>No active Capability OS task</h3>
					<p>
						Start or resume a Workbench, Factory, or Control task and its operator telemetry will
						appear here.
					</p>
				</div>
			</section>
		{:else if views && snapshot}
			<section class="kpi-grid" aria-label="Capability OS summary">
				<article class="kpi-card">
					<span>Artifacts</span><strong>{views.capabilityHealth.artifacts}</strong><small
						>{views.capabilityHealth.capabilities} capabilities</small
					>
				</article>
				<article class="kpi-card">
					<span>Evidence</span><strong>{views.taskCausality.evidenceRecords}</strong><small
						>{chainHealthy ? 'chain verified' : 'chain issue'}</small
					>
				</article>
				<article class="kpi-card">
					<span>Authority</span><strong>{views.authority.activeLeases}</strong><small
						>{views.authority.policyDecisions} decisions</small
					>
				</article>
				<article class="kpi-card">
					<span>MCP mounts</span><strong>{views.mcpFabric.activeMounts}</strong><small
						>{views.mcpFabric.adapters} adapters</small
					>
				</article>
				<article class="kpi-card">
					<span>Experiments</span><strong>{views.evolution.experiments}</strong><small
						>{views.evolution.activeExperiments} active</small
					>
				</article>
			</section>

			<div class="operator-grid">
				<section id="task-causality" class="operator-card card-wide section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Traceability</span>
							<h3>Task Causality</h3>
						</div>
						<span class="health-pill" data-good={chainHealthy}
							>{chainHealthy ? 'Chain intact' : 'Review chain'}</span
						>
					</header>
					<div class="causality-flow" aria-label="Task causality flow">
						<div class="causality-node primary">
							<span>Task</span><strong>{titleCase(snapshot.task.source)}</strong><code
								>{shortId(snapshot.task.taskId)}</code
							>
						</div>
						<div class="flow-arrow" aria-hidden="true">→</div>
						<div class="causality-node">
							<span>Runs</span><strong>{views.taskCausality.runs}</strong><small
								>{views.taskCausality.activeRuns} active</small
							>
						</div>
						<div class="flow-arrow" aria-hidden="true">→</div>
						<div class="causality-node">
							<span>Evidence</span><strong>{views.taskCausality.evidenceRecords}</strong><small
								>tamper-evident</small
							>
						</div>
						<div class="flow-arrow" aria-hidden="true">→</div>
						<div class="causality-node">
							<span>Observations</span><strong>{views.taskCausality.observations}</strong><small
								>relational projection</small
							>
						</div>
					</div>
					{#if evidenceEntries.length}
						<div class="evidence-strip">
							{#each evidenceEntries as [kind, count]}
								<span><code>{kind}</code><strong>{count}</strong></span>
							{/each}
						</div>
					{/if}
				</section>

				<section id="capability-health" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Inventory</span>
							<h3>Capability Health</h3>
						</div>
						<strong class="heading-number">{views.capabilityHealth.artifacts}</strong>
					</header>
					<div class="health-list">
						{#if artifactEntries.length === 0}<p class="muted-empty">
								No artifact state recorded.
							</p>{/if}
						{#each artifactEntries as [state, count]}
							<div class="health-row">
								<div><span>{titleCase(state)}</span><strong>{count}</strong></div>
								<div class="health-track" aria-hidden="true">
									<span style={`width:${statePercent(count)}%`}></span>
								</div>
							</div>
						{/each}
					</div>
					<div class="compact-stats">
						{#each Object.entries(views.capabilityHealth.byKind)
							.sort((a, b) => b[1] - a[1])
							.slice(0, 4) as [kind, count]}
							<div><span>{titleCase(kind)}</span><strong>{count}</strong></div>
						{/each}
					</div>
				</section>

				<section id="forge" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Build system</span>
							<h3>Forge</h3>
						</div>
						<span class="card-orbit" aria-hidden="true"></span>
					</header>
					<div class="quad-metrics">
						<div><span>Tools</span><strong>{views.forge.tools}</strong></div>
						<div><span>Builds</span><strong>{views.forge.builds}</strong></div>
						<div><span>Runs</span><strong>{views.forge.runs}</strong></div>
						<div data-danger={views.forge.failures > 0}>
							<span>Failures</span><strong>{views.forge.failures}</strong>
						</div>
					</div>
					<p class="card-note">
						Immutable source bundles, sandboxed builds, supply-chain attestations, and bounded
						runtime execution.
					</p>
				</section>

				<section id="skill-evolution" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Reusable intelligence</span>
							<h3>Skill Evolution</h3>
						</div>
						<strong class="heading-number">{views.skillEvolution.skills}</strong>
					</header>
					<div class="evolution-bars">
						<div>
							<span><b>Evaluations</b><em>{views.skillEvolution.evaluations}</em></span><i
								style={`--fill:${Math.min(100, views.skillEvolution.evaluations * 12 + 8)}%`}
							></i>
						</div>
						<div>
							<span><b>Promotions</b><em>{views.skillEvolution.promotions}</em></span><i
								style={`--fill:${Math.min(100, views.skillEvolution.promotions * 18 + 6)}%`}
							></i>
						</div>
					</div>
					<p class="card-note">
						Promotion remains evidence-gated; caller-supplied aggregate claims cannot mutate learned
						state.
					</p>
				</section>

				<section id="mcp-fabric" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Acquisition</span>
							<h3>MCP Fabric</h3>
						</div>
						<span class="health-pill" data-good={views.mcpFabric.activeMounts >= 0}
							>{views.mcpFabric.activeMounts} mounted</span
						>
					</header>
					<div class="mount-list">
						{#if snapshot.activeMounts.length === 0}<p class="muted-empty">
								No active MCP mount for this task.
							</p>{/if}
						{#each snapshot.activeMounts.slice(0, 5) as mount (mount.mountId)}
							<div class="list-row">
								<span class="live-dot"></span>
								<div>
									<strong>{mount.serverId}</strong><small
										>{mount.projectedTools.length} projected tools</small
									>
								</div>
								<code>{shortId(mount.mountId, 10)}</code>
							</div>
						{/each}
					</div>
					<div class="card-footer">
						<span>{views.mcpFabric.adapters} adapters</span><span
							>{views.mcpFabric.events} evidence events</span
						>
					</div>
				</section>

				<section id="authority" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Least privilege</span>
							<h3>Authority</h3>
						</div>
						<strong class="heading-number">{views.authority.activeLeases}</strong>
					</header>
					<div class="lease-list">
						{#if snapshot.activeLeases.length === 0}<p class="muted-empty">
								No active authority lease.
							</p>{/if}
						{#each snapshot.activeLeases.slice(0, 5) as lease (lease.leaseId)}
							<div class="list-row lease-row">
								<span class="live-dot"></span>
								<div>
									<strong>{lease.runtimeProfile}</strong><small
										>{lease.permissions.length} permissions</small
									>
								</div>
								<span class="expiry">{leaseRemaining(lease.expiresAtMs)}</span>
							</div>
						{/each}
					</div>
					<div class="card-footer">
						<span>{views.authority.policyDecisions} policy decisions</span><span
							>server authoritative</span
						>
					</div>
				</section>

				<section id="sandbox" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Isolation</span>
							<h3>Sandbox</h3>
						</div>
						<span class="health-pill" data-good={true}>Fail closed</span>
					</header>
					<div class="runtime-grid">
						{#each runtimeEntries as [runtime, available]}
							<div data-available={available}>
								<span class="runtime-icon">{available ? '✓' : '–'}</span>
								<div>
									<strong>{titleCase(runtime)}</strong><small
										>{available ? 'Available' : 'Unavailable'}</small
									>
								</div>
							</div>
						{/each}
					</div>
					<p class="card-note">
						Production rejects namespace-dev. WASM and native runtimes remain explicit, bounded
						isolation classes.
					</p>
				</section>

				<section id="evolution" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Matched evaluation</span>
							<h3>Evolution</h3>
						</div>
						<strong class="heading-number">{views.evolution.experiments}</strong>
					</header>
					<div class="experiment-visual">
						<div
							class="experiment-ring"
							style={`--progress:${views.evolution.experiments ? Math.max(12, Math.min(100, (views.evolution.activeExperiments / views.evolution.experiments) * 100)) : 0}%`}
						>
							<span><strong>{views.evolution.activeExperiments}</strong><small>active</small></span>
						</div>
						<div class="experiment-copy">
							<div><span>Experiment events</span><strong>{views.evolution.events}</strong></div>
							<div><span>Promotions</span><strong>{views.evolution.promotions}</strong></div>
						</div>
					</div>
					<p class="card-note">
						Control and candidate arms are matched on task distribution, model, reasoning effort,
						permissions, and resource budget.
					</p>
				</section>

				<section id="releases" class="operator-card section-anchor">
					<header class="card-heading">
						<div>
							<span class="card-kicker">Promotion state</span>
							<h3>Releases</h3>
						</div>
						<span class="card-orbit release-orbit" aria-hidden="true"></span>
					</header>
					<div class="release-stack">
						<div>
							<span>Learned+</span><strong>{views.releases.learnedOrHigher}</strong><small
								>reusable candidates</small
							>
						</div>
						<div>
							<span>Certified</span><strong>{views.releases.certified}</strong><small
								>evidence qualified</small
							>
						</div>
						<div>
							<span>Core</span><strong>{views.releases.core}</strong><small
								>highest trust state</small
							>
						</div>
					</div>
					<div class="card-footer">
						<span>{views.releases.supplyChainBuilds} supply-chain builds</span><span
							>content addressed</span
						>
					</div>
				</section>
			</div>
		{/if}
	</div>
</div>

<style>
	.operator-root {
		height: 100%;
		min-height: 0;
		overflow: auto;
		overscroll-behavior: contain;
		-webkit-overflow-scrolling: touch;
		background:
			radial-gradient(
				circle at 18% -8rem,
				color-mix(in oklab, var(--app-accent) 12%, transparent),
				transparent 31rem
			),
			radial-gradient(
				circle at 92% 10%,
				color-mix(in oklab, var(--app-accent) 6%, transparent),
				transparent 26rem
			),
			var(--app-bg);
	}
	.operator-shell {
		width: min(100%, 104rem);
		margin: 0 auto;
		padding: clamp(0.7rem, 2vw, 1.4rem);
		padding-left: max(clamp(0.7rem, 2vw, 1.4rem), env(safe-area-inset-left, 0px));
		padding-right: max(clamp(0.7rem, 2vw, 1.4rem), env(safe-area-inset-right, 0px));
		padding-bottom: max(1.5rem, calc(env(safe-area-inset-bottom, 0px) + 0.85rem));
	}
	.operator-hero {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(18rem, 30rem);
		gap: 1.25rem;
		overflow: hidden;
		border: 1px solid color-mix(in oklab, var(--app-accent) 18%, var(--app-border));
		border-radius: 1.15rem;
		padding: clamp(1rem, 2.5vw, 1.55rem);
		background: color-mix(in oklab, var(--app-surface) 88%, var(--app-accent) 2%);
		box-shadow: 0 1rem 3rem color-mix(in oklab, var(--app-bg) 65%, transparent);
	}
	.hero-glow {
		position: absolute;
		top: -6rem;
		right: -4rem;
		width: 18rem;
		height: 18rem;
		border-radius: 999px;
		background: radial-gradient(
			circle,
			color-mix(in oklab, var(--app-accent) 18%, transparent),
			transparent 67%
		);
		pointer-events: none;
	}
	.hero-copy,
	.hero-controls,
	.task-meta {
		position: relative;
		z-index: 1;
	}
	.eyebrow-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-height: 1.6rem;
		font-size: 0.62rem;
		font-weight: 750;
		letter-spacing: 0.105em;
		text-transform: uppercase;
		color: var(--app-fg-muted);
	}
	.eyebrow-mark {
		width: 0.48rem;
		height: 0.48rem;
		border-radius: 999px;
		background: var(--app-accent);
		box-shadow: 0 0 0 0.22rem var(--app-accent-soft);
	}
	.hero-status {
		display: inline-flex;
		align-items: center;
		gap: 0.32rem;
		margin-left: 0.25rem;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.22rem 0.42rem;
		letter-spacing: 0.04em;
		color: var(--app-fg-muted);
	}
	.hero-status > span {
		width: 0.35rem;
		height: 0.35rem;
		border-radius: 999px;
		background: var(--app-fg-subtle);
	}
	.hero-status[data-active='true'] > span {
		background: #22c881;
		box-shadow: 0 0 0 0.15rem color-mix(in oklab, #22c881 14%, transparent);
	}
	.operator-hero h2 {
		margin-top: 0.5rem;
		font-size: clamp(1.5rem, 3.4vw, 2.55rem);
		font-weight: 780;
		line-height: 1.02;
		letter-spacing: -0.045em;
	}
	.operator-hero p {
		max-width: 50rem;
		margin-top: 0.62rem;
		font-size: 0.82rem;
		line-height: 1.6;
		color: var(--app-fg-muted);
	}
	.hero-controls {
		display: flex;
		align-items: end;
		gap: 0.65rem;
		align-self: center;
	}
	.task-picker {
		display: grid;
		min-width: 0;
		flex: 1;
		gap: 0.36rem;
	}
	.task-picker > span {
		font-size: 0.59rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.task-picker select,
	.refresh-button,
	.section-nav button,
	.state-card button {
		min-height: 44px;
		border: 1px solid var(--app-border);
		border-radius: 0.72rem;
		font: inherit;
	}
	.task-picker select {
		width: 100%;
		min-width: 0;
		padding: 0 2.25rem 0 0.72rem;
		background: var(--app-surface-raised);
		color: var(--app-fg);
		font-size: 0.72rem;
		font-weight: 650;
	}
	.refresh-button {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.45rem;
		padding: 0 0.8rem;
		background: var(--app-accent);
		color: var(--app-accent-contrast, white);
		font-size: 0.69rem;
		font-weight: 750;
	}
	.refresh-button svg {
		width: 1rem;
		height: 1rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.8;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.refresh-button:disabled,
	.task-picker select:disabled {
		opacity: 0.55;
	}
	.task-meta {
		grid-column: 1 / -1;
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.5rem;
		border-top: 1px solid var(--app-divider);
		padding-top: 0.8rem;
	}
	.task-meta > div {
		display: grid;
		min-width: 0;
		gap: 0.12rem;
	}
	.task-meta span {
		font-size: 0.56rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.task-meta strong,
	.task-meta code {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.68rem;
		font-weight: 650;
	}
	.section-nav {
		display: flex;
		gap: 0.4rem;
		margin: 0.85rem 0;
		overflow-x: auto;
		overflow-y: hidden;
		scroll-snap-type: x proximity;
		overscroll-behavior-inline: contain;
		scrollbar-width: none;
	}
	.section-nav::-webkit-scrollbar {
		display: none;
	}
	.section-nav button {
		flex: 0 0 auto;
		scroll-snap-align: start;
		padding: 0 0.78rem;
		background: color-mix(in oklab, var(--app-surface) 84%, transparent);
		color: var(--app-fg-muted);
		font-size: 0.65rem;
		font-weight: 650;
	}
	.kpi-grid {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 0.62rem;
		margin-bottom: 0.7rem;
	}
	.kpi-card {
		position: relative;
		overflow: hidden;
		min-height: 6.4rem;
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
		padding: 0.8rem;
		background: linear-gradient(
			145deg,
			color-mix(in oklab, var(--app-surface) 92%, var(--app-accent) 2%),
			var(--app-surface)
		);
	}
	.kpi-card::after {
		position: absolute;
		right: -1.2rem;
		bottom: -1.5rem;
		width: 5.4rem;
		height: 5.4rem;
		border-radius: 999px;
		content: '';
		background: radial-gradient(
			circle,
			color-mix(in oklab, var(--app-accent) 12%, transparent),
			transparent 68%
		);
	}
	.kpi-card span,
	.kpi-card small {
		display: block;
		font-size: 0.6rem;
		color: var(--app-fg-muted);
	}
	.kpi-card > span {
		font-weight: 700;
		letter-spacing: 0.07em;
		text-transform: uppercase;
	}
	.kpi-card strong {
		display: block;
		margin-top: 0.55rem;
		font-size: 1.45rem;
		font-weight: 760;
		line-height: 1;
		letter-spacing: -0.035em;
	}
	.kpi-card small {
		margin-top: 0.4rem;
	}
	.operator-grid {
		display: grid;
		grid-template-columns: repeat(12, minmax(0, 1fr));
		gap: 0.7rem;
	}
	.operator-card {
		grid-column: span 4;
		min-width: 0;
		overflow: hidden;
		border: 1px solid var(--app-border);
		border-radius: 0.95rem;
		background: color-mix(in oklab, var(--app-surface) 91%, transparent);
		box-shadow: 0 0.55rem 1.6rem color-mix(in oklab, var(--app-bg) 64%, transparent);
	}
	.operator-card.card-wide {
		grid-column: span 8;
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
		padding: 0.7rem 0.82rem;
	}
	.card-kicker {
		display: block;
		font-size: 0.55rem;
		font-weight: 750;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--app-fg-subtle);
	}
	.card-heading h3 {
		margin-top: 0.13rem;
		font-size: 0.83rem;
		font-weight: 720;
	}
	.heading-number {
		font-size: 1.2rem;
		font-weight: 760;
		letter-spacing: -0.04em;
	}
	.health-pill {
		display: inline-flex;
		min-height: 1.65rem;
		align-items: center;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0 0.5rem;
		font-size: 0.57rem;
		font-weight: 720;
		color: var(--app-fg-muted);
	}
	.health-pill[data-good='true'] {
		border-color: color-mix(in oklab, #22c881 28%, var(--app-border));
		background: color-mix(in oklab, #22c881 7%, transparent);
		color: #22a971;
	}
	.card-orbit {
		width: 1.6rem;
		height: 1.6rem;
		border: 1px solid color-mix(in oklab, var(--app-accent) 55%, var(--app-border));
		border-radius: 50%;
		box-shadow: inset 0 0 0 0.32rem var(--app-accent-soft);
	}
	.release-orbit {
		border-radius: 0.4rem;
		transform: rotate(45deg);
	}
	.causality-flow {
		display: grid;
		grid-template-columns:
			minmax(7rem, 1fr) auto minmax(6rem, 0.7fr) auto minmax(6rem, 0.7fr)
			auto minmax(7rem, 0.8fr);
		align-items: center;
		gap: 0.45rem;
		padding: 1rem 0.82rem;
	}
	.causality-node {
		display: grid;
		min-width: 0;
		min-height: 5.2rem;
		align-content: center;
		gap: 0.2rem;
		border: 1px solid var(--app-border);
		border-radius: 0.72rem;
		padding: 0.62rem;
		background: var(--app-surface-subtle);
	}
	.causality-node.primary {
		border-color: color-mix(in oklab, var(--app-accent) 28%, var(--app-border));
		background: color-mix(in oklab, var(--app-accent-soft) 55%, var(--app-surface));
	}
	.causality-node span,
	.causality-node small {
		font-size: 0.56rem;
		color: var(--app-fg-muted);
	}
	.causality-node strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.78rem;
	}
	.causality-node code {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.58rem;
		color: var(--app-fg-muted);
	}
	.flow-arrow {
		font-size: 0.9rem;
		color: var(--app-accent);
	}
	.evidence-strip {
		display: flex;
		gap: 0.35rem;
		border-top: 1px solid var(--app-divider);
		padding: 0.65rem 0.82rem;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.evidence-strip::-webkit-scrollbar {
		display: none;
	}
	.evidence-strip > span {
		display: inline-flex;
		min-height: 1.75rem;
		flex: 0 0 auto;
		align-items: center;
		gap: 0.45rem;
		border: 1px solid var(--app-border);
		border-radius: 0.5rem;
		padding: 0 0.48rem;
		font-size: 0.55rem;
		color: var(--app-fg-muted);
	}
	.evidence-strip strong {
		color: var(--app-fg);
	}
	.health-list {
		display: grid;
		gap: 0.72rem;
		padding: 0.85rem;
	}
	.health-row > div:first-child {
		display: flex;
		justify-content: space-between;
		gap: 0.7rem;
		font-size: 0.62rem;
	}
	.health-row span {
		color: var(--app-fg-muted);
	}
	.health-track {
		height: 0.28rem;
		margin-top: 0.32rem;
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
			color-mix(in oklab, var(--app-accent) 55%, #22c881)
		);
	}
	.compact-stats,
	.quad-metrics,
	.release-stack {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		border-top: 1px solid var(--app-divider);
	}
	.compact-stats > div,
	.quad-metrics > div,
	.release-stack > div {
		display: grid;
		gap: 0.22rem;
		min-height: 4.8rem;
		align-content: center;
		border-right: 1px solid var(--app-divider);
		border-bottom: 1px solid var(--app-divider);
		padding: 0.65rem 0.78rem;
	}
	.compact-stats > div:nth-child(2n),
	.quad-metrics > div:nth-child(2n),
	.release-stack > div:nth-child(2n) {
		border-right: 0;
	}
	.compact-stats span,
	.quad-metrics span,
	.release-stack span,
	.release-stack small {
		font-size: 0.57rem;
		color: var(--app-fg-muted);
	}
	.compact-stats strong,
	.quad-metrics strong,
	.release-stack strong {
		font-size: 1.05rem;
		font-weight: 750;
	}
	.quad-metrics > div[data-danger='true'] strong {
		color: #df5965;
	}
	.card-note {
		margin: 0;
		border-top: 1px solid var(--app-divider);
		padding: 0.72rem 0.82rem;
		font-size: 0.62rem;
		line-height: 1.55;
		color: var(--app-fg-muted);
	}
	.evolution-bars {
		display: grid;
		gap: 0.88rem;
		padding: 1rem 0.85rem;
	}
	.evolution-bars > div > span {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		font-size: 0.61rem;
	}
	.evolution-bars b {
		font-weight: 650;
		color: var(--app-fg-muted);
	}
	.evolution-bars em {
		font-style: normal;
		font-weight: 750;
	}
	.evolution-bars i {
		display: block;
		height: 0.38rem;
		margin-top: 0.38rem;
		overflow: hidden;
		border-radius: 999px;
		background: var(--app-border);
	}
	.evolution-bars i::after {
		display: block;
		width: var(--fill);
		height: 100%;
		border-radius: inherit;
		content: '';
		background: linear-gradient(90deg, var(--app-accent), #8a67e8);
	}
	.mount-list,
	.lease-list {
		min-height: 8rem;
	}
	.list-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 0.58rem;
		min-height: 3.1rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.52rem 0.8rem;
	}
	.list-row:last-child {
		border-bottom: 0;
	}
	.list-row > div {
		display: grid;
		min-width: 0;
		gap: 0.1rem;
	}
	.list-row strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.66rem;
	}
	.list-row small,
	.list-row code,
	.expiry {
		font-size: 0.56rem;
		color: var(--app-fg-muted);
	}
	.live-dot {
		width: 0.42rem;
		height: 0.42rem;
		border-radius: 999px;
		background: #22c881;
		box-shadow: 0 0 0 0.16rem color-mix(in oklab, #22c881 12%, transparent);
	}
	.card-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.6rem;
		min-height: 2.4rem;
		border-top: 1px solid var(--app-divider);
		padding: 0 0.8rem;
		font-size: 0.56rem;
		color: var(--app-fg-subtle);
	}
	.runtime-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.5rem;
		padding: 0.82rem;
	}
	.runtime-grid > div {
		display: flex;
		min-width: 0;
		align-items: center;
		gap: 0.55rem;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		padding: 0.6rem;
		background: var(--app-surface-subtle);
	}
	.runtime-grid > div[data-available='true'] {
		border-color: color-mix(in oklab, #22c881 24%, var(--app-border));
	}
	.runtime-icon {
		display: grid;
		width: 1.55rem;
		height: 1.55rem;
		flex: 0 0 auto;
		place-items: center;
		border-radius: 0.48rem;
		background: var(--app-hover);
		font-size: 0.7rem;
		font-weight: 800;
	}
	.runtime-grid > div[data-available='true'] .runtime-icon {
		background: color-mix(in oklab, #22c881 12%, transparent);
		color: #22a971;
	}
	.runtime-grid > div > div {
		display: grid;
		min-width: 0;
		gap: 0.1rem;
	}
	.runtime-grid strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.63rem;
	}
	.runtime-grid small {
		font-size: 0.54rem;
		color: var(--app-fg-muted);
	}
	.experiment-visual {
		display: grid;
		grid-template-columns: 7rem minmax(0, 1fr);
		align-items: center;
		gap: 1rem;
		padding: 0.9rem;
	}
	.experiment-ring {
		display: grid;
		width: 6.25rem;
		aspect-ratio: 1;
		place-items: center;
		border-radius: 50%;
		background: conic-gradient(var(--app-accent) var(--progress), var(--app-border) 0);
	}
	.experiment-ring::before {
		grid-area: 1 / 1;
		width: 4.9rem;
		aspect-ratio: 1;
		border-radius: 50%;
		content: '';
		background: var(--app-surface);
	}
	.experiment-ring span {
		z-index: 1;
		display: grid;
		grid-area: 1 / 1;
		text-align: center;
	}
	.experiment-ring strong {
		font-size: 1.25rem;
	}
	.experiment-ring small {
		font-size: 0.55rem;
		color: var(--app-fg-muted);
	}
	.experiment-copy {
		display: grid;
		gap: 0.55rem;
	}
	.experiment-copy > div {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		border-bottom: 1px solid var(--app-divider);
		padding: 0.45rem 0;
		font-size: 0.61rem;
	}
	.experiment-copy span {
		color: var(--app-fg-muted);
	}
	.release-stack {
		grid-template-columns: repeat(3, minmax(0, 1fr));
		border-top: 0;
	}
	.release-stack > div {
		border-top: 0;
		border-bottom: 0;
	}
	.release-stack > div:nth-child(2n) {
		border-right: 1px solid var(--app-divider);
	}
	.release-stack > div:last-child {
		border-right: 0;
	}
	.muted-empty {
		margin: 0;
		padding: 1.4rem 0.85rem;
		text-align: center;
		font-size: 0.64rem;
		color: var(--app-fg-muted);
	}
	.state-card {
		display: flex;
		min-height: 9rem;
		align-items: center;
		gap: 0.9rem;
		border: 1px solid var(--app-border);
		border-radius: 0.95rem;
		padding: 1rem;
		background: var(--app-surface);
	}
	.state-icon {
		display: grid;
		width: 2.4rem;
		height: 2.4rem;
		flex: 0 0 auto;
		place-items: center;
		border-radius: 0.7rem;
		background: var(--app-accent-soft);
		color: var(--app-accent);
		font-weight: 800;
	}
	.state-card > div:nth-child(2) {
		min-width: 0;
		flex: 1;
	}
	.state-card h3 {
		font-size: 0.82rem;
		font-weight: 700;
	}
	.state-card p {
		margin-top: 0.25rem;
		font-size: 0.68rem;
		line-height: 1.5;
		color: var(--app-fg-muted);
	}
	.state-card button {
		padding: 0 0.85rem;
		background: var(--app-interactive);
		font-size: 0.66rem;
		font-weight: 700;
	}
	.error-state {
		border-color: color-mix(in oklab, #df5965 28%, var(--app-border));
	}
	.error-state .state-icon {
		background: color-mix(in oklab, #df5965 10%, transparent);
		color: #df5965;
	}
	.skeleton-grid {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.7rem;
	}
	.skeleton-card {
		height: 11rem;
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
		background: linear-gradient(
			100deg,
			var(--app-surface-subtle),
			var(--app-surface-raised),
			var(--app-surface-subtle)
		);
		background-size: 180% 100%;
		animation: operator-shimmer 1.5s linear infinite;
	}
	button:focus-visible,
	select:focus-visible {
		outline: 2px solid var(--app-accent);
		outline-offset: 2px;
	}
	@media (hover: hover) and (pointer: fine) {
		.section-nav button:hover,
		.state-card button:hover {
			border-color: color-mix(in oklab, var(--app-accent) 35%, var(--app-border));
			background: var(--app-hover);
			color: var(--app-fg);
		}
		.operator-card:hover {
			border-color: color-mix(in oklab, var(--app-accent) 22%, var(--app-border));
		}
	}
	@media (max-width: 1180px) {
		.operator-card,
		.operator-card.card-wide {
			grid-column: span 6;
		}
		.causality-flow {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
		.flow-arrow {
			display: none;
		}
	}
	@media (max-width: 820px) {
		.operator-hero {
			grid-template-columns: 1fr;
		}
		.hero-controls {
			align-self: stretch;
		}
		.task-meta {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.kpi-grid {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
		.skeleton-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
	@media (max-width: 639px) {
		.operator-shell {
			padding-top: 0.6rem;
		}
		.operator-hero {
			gap: 0.9rem;
			border-radius: 0.95rem;
			padding: 0.9rem;
			box-shadow: none;
		}
		.eyebrow-row {
			flex-wrap: wrap;
		}
		.operator-hero h2 {
			font-size: 1.65rem;
		}
		.operator-hero p {
			font-size: 0.75rem;
		}
		.hero-controls {
			align-items: stretch;
			flex-direction: column;
		}
		.refresh-button {
			width: 100%;
		}
		.task-meta {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.65rem;
		}
		.section-nav {
			margin-inline: calc(-1 * max(0.7rem, env(safe-area-inset-left, 0px)));
			padding-inline: max(0.7rem, env(safe-area-inset-left, 0px));
		}
		.section-nav button {
			min-height: 46px;
		}
		.kpi-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.5rem;
		}
		.kpi-card {
			min-height: 5.7rem;
			padding: 0.72rem;
		}
		.kpi-card:last-child {
			grid-column: 1 / -1;
		}
		.operator-grid {
			grid-template-columns: 1fr;
			gap: 0.6rem;
		}
		.operator-card,
		.operator-card.card-wide {
			grid-column: 1;
			border-radius: 0.84rem;
			box-shadow: none;
		}
		.card-heading {
			min-height: 3.75rem;
		}
		.causality-flow {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 0.45rem;
			padding: 0.72rem;
		}
		.causality-node {
			min-height: 4.6rem;
		}
		.runtime-grid {
			grid-template-columns: 1fr;
		}
		.experiment-visual {
			grid-template-columns: 5.8rem minmax(0, 1fr);
			gap: 0.75rem;
			padding: 0.75rem;
		}
		.experiment-ring {
			width: 5.4rem;
		}
		.experiment-ring::before {
			width: 4.2rem;
		}
		.release-stack {
			grid-template-columns: 1fr;
		}
		.release-stack > div,
		.release-stack > div:nth-child(2n) {
			min-height: 4.15rem;
			border-right: 0;
			border-bottom: 1px solid var(--app-divider);
		}
		.release-stack > div:last-child {
			border-bottom: 0;
		}
		.state-card {
			align-items: flex-start;
			flex-wrap: wrap;
		}
		.state-card button {
			width: 100%;
		}
		.skeleton-grid {
			grid-template-columns: 1fr;
		}
		.skeleton-card {
			height: 8rem;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.skeleton-card {
			animation: none;
		}
		* {
			scroll-behavior: auto !important;
		}
	}
	@keyframes operator-shimmer {
		to {
			background-position: -180% 0;
		}
	}
</style>
