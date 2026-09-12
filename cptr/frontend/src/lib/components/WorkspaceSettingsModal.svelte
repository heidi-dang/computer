<script lang="ts">
	import { toast } from 'svelte-sonner';
	import Modal from '$lib/components/Modal.svelte';
	import WorkspaceActivity from '$lib/components/WorkspaceActivity.svelte';
	import WorkspaceAdminPrivilege from '$lib/components/WorkspaceAdminPrivilege.svelte';
	import WorkspaceCheckpoints from '$lib/components/WorkspaceCheckpoints.svelte';
	import WorkspaceContextMemory from '$lib/components/WorkspaceContextMemory.svelte';
	import WorkspaceEnvironment from '$lib/components/WorkspaceEnvironment.svelte';
	import WorkspaceGroups from '$lib/components/WorkspaceGroups.svelte';
	import WorkspaceHealth from '$lib/components/WorkspaceHealth.svelte';
	import WorkspaceInstructions from '$lib/components/WorkspaceInstructions.svelte';
	import WorkspaceOverview from '$lib/components/WorkspaceOverview.svelte';
	import WorkspaceRepositoryTopology from '$lib/components/WorkspaceRepositoryTopology.svelte';
	import WorkspaceTasks from '$lib/components/WorkspaceTasks.svelte';
	import WorkspaceWorkbench from '$lib/components/WorkspaceWorkbench.svelte';
	import {
		addWorkspaceRepository,
		getRepositoryCatalog,
		getWorkspaceCheckpoints,
		getWorkspaceContext,
		getWorkspaceEnvironmentProfiles,
		getWorkspaceHealth,
		getWorkspaceInstructionHistory,
		getWorkspaceInstructions,
		getWorkspaceProjection,
		getWorkspaceRepositories,
		reconcileWorkspace,
		removeWorkspaceRepository,
		updateWorkspaceIdentity,
		updateWorkspaceRepository,
		type EnvironmentProfileView,
		type RepositoryCatalogItem,
		type WorkspaceCheckpointView,
		type WorkspaceContextView,
		type WorkspaceInstruction,
		type WorkspaceProjection,
		type WorkspaceRepositorySummary
	} from '$lib/apis/workspace-os';
	import { currentWorkspace, loadWorkspaceList } from '$lib/stores';
	import { workspaceOsStore } from '$lib/stores/workspace-os.svelte';
	import type { WorkspaceOsDomain } from '$lib/stores/workspace-os-state';

	type TabId =
		| 'overview'
		| 'general'
		| 'repositories'
		| 'groups'
		| 'instructions'
		| 'environment'
		| 'memory'
		| 'checkpoints'
		| 'tasks'
		| 'workbench'
		| 'admin'
		| 'activity'
		| 'health';

	interface WorkspaceTarget {
		workspace_id: string;
		path: string;
		name: string;
		slug: string | null;
		workspace_type: string;
	}

	interface Props {
		workspace: WorkspaceTarget;
		initialTab?: string;
		onclose: () => void;
		ontabchange?: (tab: string) => void;
	}

	let { workspace, initialTab = 'overview', onclose, ontabchange = () => {} }: Props = $props();

	const tabs: Array<{ id: TabId; label: string }> = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'general', label: 'General' },
		{ id: 'repositories', label: 'Repositories' },
		{ id: 'groups', label: 'Groups' },
		{ id: 'instructions', label: 'Instructions' },
		{ id: 'environment', label: 'Environment' },
		{ id: 'memory', label: 'Context & Memory' },
		{ id: 'checkpoints', label: 'Checkpoints' },
		{ id: 'tasks', label: 'Tasks' },
		{ id: 'workbench', label: 'Workbench' },
		{ id: 'admin', label: 'Admin' },
		{ id: 'activity', label: 'Activity' },
		{ id: 'health', label: 'Health' }
	];

	function isTabId(value: string): value is TabId {
		return tabs.some((tab) => tab.id === value);
	}

	let activeTab = $state<TabId>('overview');
	let appliedInitialTab = $state<string | null>(null);

	$effect(() => {
		if (initialTab === appliedInitialTab) return;
		appliedInitialTab = initialTab;
		activeTab = isTabId(initialTab) ? initialTab : 'overview';
	});
	let busy = $state(false);
	let tabLoading = $state<Partial<Record<TabId, boolean>>>({});
	let tabErrors = $state<Partial<Record<TabId, string>>>({});
	let loadedTabs = $state<Set<TabId>>(new Set(['general']));
	let snapshotProjection = $state<WorkspaceProjection | null>(null);

	let name = $state('');
	let slug = $state('');
	let workspaceType = $state('project');

	$effect(() => {
		name = workspace.name;
		slug = workspace.slug ?? '';
		workspaceType = workspace.workspace_type || 'project';
	});

	let repositories = $state<WorkspaceRepositorySummary[]>([]);
	let repositoryCatalog = $state<RepositoryCatalogItem[]>([]);
	let selectedRepository = $state('');

	let instruction = $state<WorkspaceInstruction | null>(null);
	let instructionHistory = $state<WorkspaceInstruction[]>([]);

	let environmentProfiles = $state<EnvironmentProfileView[]>([]);

	let contextData = $state<WorkspaceContextView | null>(null);
	let checkpoints = $state<WorkspaceCheckpointView[]>([]);
	let checkpointMemoryVersion = $state(0);
	let health = $state<Record<string, unknown> | null>(null);

	let isLiveTarget = $derived(workspaceOsStore.workspaceId === workspace.workspace_id);
	let projection = $derived(isLiveTarget ? workspaceOsStore.projection : snapshotProjection);
	let connectionStatus = $derived(
		isLiveTarget
			? workspaceOsStore.connection.status
			: snapshotProjection
				? 'snapshot'
				: tabLoading.overview
					? 'loading'
					: 'idle'
	);
	let liveStaleDomains = $derived(
		isLiveTarget ? (workspaceOsStore.staleDomains as readonly WorkspaceOsDomain[]) : []
	);
	let activeTabLoading = $derived(Boolean(tabLoading[activeTab]));
	let activeTabError = $derived(tabErrors[activeTab] ?? '');

	const availableRepositories = $derived(
		repositoryCatalog.filter(
			(item) => !repositories.some((repo) => repo.repository_id === item.repository_id)
		)
	);

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		if (typeof value === 'string') return value;
		return 'Workspace operation failed';
	}

	function domainsForTab(tab: TabId): WorkspaceOsDomain[] {
		switch (tab) {
			case 'repositories':
				return ['repositories'];
			case 'groups':
				return [];
			case 'instructions':
				return ['instructions'];
			case 'environment':
				return ['environment'];
			case 'memory':
				return ['memory', 'context'];
			case 'checkpoints':
				return ['checkpoints'];
			case 'tasks':
				return ['tasks'];
			case 'workbench':
				return ['workbench'];
			case 'admin':
				return ['privilege'];
			case 'activity':
				return ['projection'];
			case 'health':
				return ['health'];
			case 'overview':
				return ['projection'];
			default:
				return [];
		}
	}

	function isTabStale(tab: TabId): boolean {
		if (!isLiveTarget) return false;
		const stale = new Set(liveStaleDomains);
		return domainsForTab(tab).some((domain) => stale.has(domain));
	}

	function selectTab(tab: TabId) {
		if (activeTab === tab) return;
		activeTab = tab;
		ontabchange(tab);
	}

	function handleTabKeydown(event: KeyboardEvent, index: number) {
		let next = index;
		if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % tabs.length;
		else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft')
			next = (index - 1 + tabs.length) % tabs.length;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = tabs.length - 1;
		else return;
		event.preventDefault();
		selectTab(tabs[next].id);
		const currentTarget = event.currentTarget as HTMLElement;
		const tabButtons = currentTarget.parentElement?.querySelectorAll<HTMLElement>('[role="tab"]');
		queueMicrotask(() => tabButtons?.[next]?.focus());
	}

	function markLoaded(tab: TabId, clearFresh = true) {
		loadedTabs = new Set([...loadedTabs, tab]);
		if (clearFresh && isLiveTarget) workspaceOsStore.markFresh(domainsForTab(tab));
	}

	async function refreshProjectionSummary() {
		if (isLiveTarget) {
			await workspaceOsStore.refresh();
		} else {
			snapshotProjection = await getWorkspaceProjection(workspace.workspace_id);
		}
	}

	function refreshOverviewAfterMutation() {
		void refreshProjectionSummary()
			.then(() => markLoaded('overview'))
			.catch(() => {
				const next = new Set(loadedTabs);
				next.delete('overview');
				loadedTabs = next;
			});
	}

	async function loadTab(tab: TabId, force = false) {
		if (tabLoading[tab]) return;
		if (!force && loadedTabs.has(tab) && !isTabStale(tab)) return;
		tabLoading = { ...tabLoading, [tab]: true };
		tabErrors = { ...tabErrors, [tab]: '' };
		try {
			switch (tab) {
				case 'overview':
					await refreshProjectionSummary();
					break;
				case 'general':
					break;
				case 'groups':
					break;
				case 'repositories': {
					const [repoResult, catalogResult] = await Promise.all([
						getWorkspaceRepositories(workspace.workspace_id),
						getRepositoryCatalog()
					]);
					repositories = repoResult.repositories ?? [];
					repositoryCatalog = catalogResult.repositories ?? [];
					break;
				}
				case 'instructions': {
					const [instructionResult, historyResult] = await Promise.all([
						getWorkspaceInstructions(workspace.workspace_id),
						getWorkspaceInstructionHistory(workspace.workspace_id)
					]);
					instruction = instructionResult.instruction;
					instructionHistory = historyResult.instructions ?? [];
					break;
				}
				case 'environment': {
					const envResult = await getWorkspaceEnvironmentProfiles(workspace.workspace_id);
					environmentProfiles = envResult.profiles ?? [];
					break;
				}
				case 'memory':
					contextData = await getWorkspaceContext(workspace.workspace_id);
					break;
				case 'checkpoints': {
					const checkpointResult = await getWorkspaceCheckpoints(workspace.workspace_id);
					checkpoints = checkpointResult.checkpoints ?? [];
					checkpointMemoryVersion = checkpointResult.memory_version ?? 0;
					break;
				}
				case 'tasks':
				case 'workbench':
				case 'admin':
					break;
				case 'activity':
					await refreshProjectionSummary();
					break;
				case 'health':
					health = await getWorkspaceHealth(workspace.workspace_id);
					break;
			}
			markLoaded(tab, !['tasks', 'workbench', 'admin'].includes(tab));
		} catch (e) {
			tabErrors = { ...tabErrors, [tab]: message(e) };
		} finally {
			tabLoading = { ...tabLoading, [tab]: false };
		}
	}

	$effect(() => {
		const tab = activeTab;
		const stale = isTabStale(tab);
		if (!tabLoading[tab] && (!loadedTabs.has(tab) || stale)) {
			void loadTab(tab, stale);
		}
	});

	async function saveGeneral() {
		busy = true;
		try {
			const result = await updateWorkspaceIdentity(workspace.workspace_id, {
				name: name.trim(),
				slug: slug.trim(),
				workspace_type: workspaceType
			});
			name = result.workspace.name;
			slug = result.workspace.slug ?? '';
			workspaceType = result.workspace.workspace_type;
			currentWorkspace.update((current) =>
				current?.workspace_id === workspace.workspace_id
					? {
							...current,
							name,
							slug,
							workspace_type: workspaceType
						}
					: current
			);
			await loadWorkspaceList();
			refreshOverviewAfterMutation();
			toast.success('Workspace updated');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function addRepository() {
		if (!selectedRepository) return;
		busy = true;
		try {
			const result = await addWorkspaceRepository(workspace.workspace_id, {
				repository_id: selectedRepository,
				role: 'repository',
				primary: repositories.length === 0,
				sort_order: repositories.length
			});
			repositories = result.repositories ?? [];
			selectedRepository = '';
			refreshOverviewAfterMutation();
			toast.success('Repository added');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function setPrimary(repo: WorkspaceRepositorySummary) {
		busy = true;
		try {
			const result = await updateWorkspaceRepository(workspace.workspace_id, {
				repository_id: repo.repository_id,
				primary: true
			});
			repositories = result.repositories ?? [];
			refreshOverviewAfterMutation();
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function saveRole(repo: WorkspaceRepositorySummary, role: string) {
		busy = true;
		try {
			const result = await updateWorkspaceRepository(workspace.workspace_id, {
				repository_id: repo.repository_id,
				role
			});
			repositories = result.repositories ?? [];
			refreshOverviewAfterMutation();
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function removeRepository(repo: WorkspaceRepositorySummary) {
		busy = true;
		try {
			const result = await removeWorkspaceRepository(workspace.workspace_id, repo.repository_id);
			repositories = result.repositories ?? [];
			refreshOverviewAfterMutation();
			toast.success('Repository removed');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function runReconcile() {
		busy = true;
		try {
			const result = await reconcileWorkspace(workspace.workspace_id);
			health = (result.health as Record<string, unknown> | undefined) ?? result;
			refreshOverviewAfterMutation();
			toast.success('Workspace state reconciled');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}
</script>

<Modal {onclose} class="w-full max-w-5xl mx-0 sm:mx-4 max-h-[92dvh]">
	<div class="flex min-h-[36rem] max-h-[92dvh] flex-col overflow-hidden">
		<header
			class="flex items-start justify-between gap-4 border-b border-[var(--app-border)] px-5 py-4"
		>
			<div class="min-w-0">
				<div class="flex flex-wrap items-center gap-2">
					<h2 class="truncate text-lg font-semibold">Workspace Center</h2>
					<span
						class="rounded-full border border-[var(--app-border)] px-2 py-0.5 text-[11px] font-medium"
					>
						{name}
					</span>
					{#if projection}
						<span
							class="rounded-full border border-[var(--app-border)] px-2 py-0.5 text-[11px] uppercase tracking-wide"
						>
							{projection.health.status}
						</span>
					{/if}
				</div>
				<p class="mt-1 truncate text-xs text-[var(--app-muted-fg)]">
					{workspace.slug || workspace.workspace_id} · {workspace.path}
				</p>
			</div>
			<button
				class="min-h-11 min-w-11 rounded-lg px-3 py-1.5 text-sm hover:bg-[var(--app-hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
				onclick={onclose}
				aria-label="Close Workspace Center"
			>
				Close
			</button>
		</header>

		<div class="flex min-h-0 flex-1 flex-col sm:flex-row">
			<div
				class="flex shrink-0 gap-1 overflow-x-auto overscroll-contain border-b border-[var(--app-border)] p-2 sm:w-44 sm:flex-col sm:border-b-0 sm:border-r"
				role="tablist"
				aria-label="Workspace Center sections"
			>
				{#each tabs as tab, index}
					<button
						id={'workspace-tab-' + tab.id}
						role="tab"
						aria-selected={activeTab === tab.id}
						aria-controls="workspace-center-panel"
						tabindex={activeTab === tab.id ? 0 : -1}
						class="min-h-11 whitespace-nowrap rounded-lg px-3 py-2 text-left text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 {activeTab ===
						tab.id
							? 'bg-[var(--app-hover)] font-medium'
							: 'text-[var(--app-muted-fg)] hover:bg-[var(--app-hover)]'}"
						onclick={() => selectTab(tab.id)}
						onkeydown={(event) => handleTabKeydown(event, index)}
					>
						{tab.label}
					</button>
				{/each}
			</div>

			<div
				id="workspace-center-panel"
				role="tabpanel"
				aria-labelledby={'workspace-tab-' + activeTab}
				tabindex="0"
				class="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3 sm:p-5 focus:outline-none"
			>
				{#if activeTabLoading && !loadedTabs.has(activeTab)}
					<div class="flex min-h-48 items-center justify-center text-sm text-[var(--app-muted-fg)]">
						Loading {tabs.find((tab) => tab.id === activeTab)?.label ?? 'Workspace'}…
					</div>
				{:else if activeTabError}
					<div class="rounded-xl border border-red-500/30 bg-red-500/5 p-4">
						<p class="text-sm text-red-500">{activeTabError}</p>
						<button
							class="mt-3 rounded-lg border border-[var(--app-border)] px-3 py-1.5 text-sm"
							onclick={() => void loadTab(activeTab, true)}
						>
							Retry
						</button>
					</div>
				{:else if activeTab === 'overview'}
					<WorkspaceOverview
						{projection}
						{connectionStatus}
						staleDomains={liveStaleDomains}
						onrefresh={() => loadTab('overview', true)}
					/>
				{:else if activeTab === 'general'}
					<section class="space-y-5">
						<div>
							<h3 class="text-base font-semibold">General</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Stable Workspace identity; filesystem path remains a compatibility execution root.
							</p>
						</div>
						<label class="block">
							<span class="mb-1 block text-xs font-medium">Name</span>
							<input class="workspace-field" bind:value={name} />
						</label>
						<label class="block">
							<span class="mb-1 block text-xs font-medium">Slug</span>
							<input class="workspace-field" bind:value={slug} />
						</label>
						<label class="block">
							<span class="mb-1 block text-xs font-medium">Type</span>
							<select class="workspace-field" bind:value={workspaceType}>
								<option value="project">Project</option>
								<option value="single_repo">Single repository</option>
								<option value="multi_repo">Multi repository</option>
								<option value="general">General</option>
							</select>
						</label>
						<div
							class="rounded-xl border border-[var(--app-border)] p-3 text-xs text-[var(--app-muted-fg)]"
						>
							<div><strong>Workspace ID:</strong> {workspace.workspace_id}</div>
							<div class="mt-1 break-all">
								<strong>Compatibility path:</strong>
								{workspace.path}
							</div>
						</div>
						<button class="workspace-primary" disabled={busy || !name.trim()} onclick={saveGeneral}
							>Save identity</button
						>
					</section>
				{:else if activeTab === 'repositories'}
					<section class="space-y-4">
						<div>
							<h3 class="text-base font-semibold">Repositories</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Explicit logical repository membership. Worker worktrees remain ephemeral.
							</p>
						</div>
						<WorkspaceRepositoryTopology workspaceName={name} {repositories} />
						<div class="flex flex-col gap-2 sm:flex-row">
							<select class="workspace-field flex-1" bind:value={selectedRepository}>
								<option value="">Add an existing repository…</option>
								{#each availableRepositories as repo}
									<option value={repo.repository_id}
										>{repo.name} · {repo.canonical_remote_identity}</option
									>
								{/each}
							</select>
							<button
								class="workspace-primary"
								disabled={busy || !selectedRepository}
								onclick={addRepository}>Add</button
							>
						</div>
						{#if repositories.length === 0}
							<p
								class="rounded-xl border border-dashed border-[var(--app-border)] p-5 text-sm text-[var(--app-muted-fg)]"
							>
								No explicit repository membership yet.
							</p>
						{/if}
						{#each repositories as repo}
							<div class="rounded-xl border border-[var(--app-border)] p-4">
								<div class="flex flex-wrap items-start justify-between gap-3">
									<div class="min-w-0">
										<div class="font-medium">{repo.name} {repo.primary ? '★' : ''}</div>
										<div class="mt-1 break-all text-xs text-[var(--app-muted-fg)]">
											{repo.canonical_remote_identity}
										</div>
										<div class="mt-1 text-xs text-[var(--app-muted-fg)]">
											{repo.default_branch || 'default branch unknown'} · {repo.checkouts.length} checkout(s)
										</div>
									</div>
									<div class="flex gap-2">
										{#if !repo.primary}
											<button
												class="workspace-secondary"
												disabled={busy}
												onclick={() => setPrimary(repo)}>Make primary</button
											>
										{/if}
										<button
											class="workspace-danger"
											disabled={busy}
											onclick={() => removeRepository(repo)}>Remove</button
										>
									</div>
								</div>
								<label class="mt-3 block">
									<span class="mb-1 block text-xs font-medium">Role</span>
									<input
										class="workspace-field"
										value={repo.role}
										onchange={(event) => saveRole(repo, event.currentTarget.value)}
									/>
								</label>
							</div>
						{/each}
					</section>
				{:else if activeTab === 'groups'}
					<WorkspaceGroups workspaceId={workspace.workspace_id} workspaceName={name} />
				{:else if activeTab === 'instructions'}
					<WorkspaceInstructions
						workspaceId={workspace.workspace_id}
						{instruction}
						history={instructionHistory}
						onrefresh={async () => {
							await loadTab('instructions', true);
							refreshOverviewAfterMutation();
						}}
					/>
				{:else if activeTab === 'environment'}
					<WorkspaceEnvironment
						workspaceId={workspace.workspace_id}
						profiles={environmentProfiles}
						onrefresh={async () => {
							await loadTab('environment', true);
							refreshOverviewAfterMutation();
						}}
					/>
				{:else if activeTab === 'memory'}
					<WorkspaceContextMemory
						workspaceId={workspace.workspace_id}
						workspacePath={workspace.path}
						initialContext={contextData}
						onrefresh={async () => {
							await loadTab('memory', true);
							refreshOverviewAfterMutation();
						}}
					/>
				{:else if activeTab === 'checkpoints'}
					<WorkspaceCheckpoints
						{checkpoints}
						memoryVersion={checkpointMemoryVersion}
						onrefresh={() => loadTab('checkpoints', true)}
					/>
				{:else if activeTab === 'tasks'}
					<WorkspaceTasks
						workspaceId={workspace.workspace_id}
						stale={isTabStale('tasks')}
						onfresh={() => {
							markLoaded('tasks', true);
							refreshOverviewAfterMutation();
						}}
					/>
				{:else if activeTab === 'workbench'}
					<WorkspaceWorkbench
						workspaceId={workspace.workspace_id}
						stale={isTabStale('workbench')}
						onfresh={() => markLoaded('workbench', true)}
					/>
				{:else if activeTab === 'admin'}
					<WorkspaceAdminPrivilege
						workspaceId={workspace.workspace_id}
						stale={isTabStale('admin')}
						onfresh={() => markLoaded('admin', true)}
					/>
				{:else if activeTab === 'activity'}
					<WorkspaceActivity
						{projection}
						{connectionStatus}
						onrefresh={() => loadTab('activity', true)}
					/>
				{:else if activeTab === 'health'}
					<WorkspaceHealth
						{health}
						{projection}
						{busy}
						onrefresh={() => loadTab('health', true)}
						onreconcile={runReconcile}
					/>
				{/if}
			</div>
		</div>
	</div>
</Modal>

<style>
	.workspace-field {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--app-bg) 92%, transparent);
		padding: 0.55rem 0.7rem;
		font-size: 0.875rem;
		outline: none;
	}
	.workspace-field:focus {
		border-color: color-mix(in srgb, var(--app-fg) 35%, var(--app-border));
	}
	.workspace-primary,
	.workspace-secondary,
	.workspace-danger {
		border-radius: 0.65rem;
		padding: 0.5rem 0.8rem;
		font-size: 0.8rem;
		font-weight: 600;
		transition:
			opacity 120ms ease,
			background 120ms ease;
	}
	.workspace-primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}
	.workspace-secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}
	.workspace-danger {
		border: 1px solid rgb(239 68 68 / 0.35);
		color: rgb(239 68 68);
	}
	.workspace-primary:disabled,
	.workspace-secondary:disabled,
	.workspace-danger:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	@media (prefers-reduced-motion: reduce) {
		.workspace-primary,
		.workspace-secondary,
		.workspace-danger {
			transition: none;
		}
	}
</style>
