<script lang="ts">
	import { toast } from 'svelte-sonner';
	import Modal from '$lib/components/Modal.svelte';
	import WorkspaceActivity from '$lib/components/WorkspaceActivity.svelte';
	import WorkspaceHealth from '$lib/components/WorkspaceHealth.svelte';
	import WorkspaceOverview from '$lib/components/WorkspaceOverview.svelte';
	import {
		addWorkspaceRepository,
		createWorkspaceEnvironment,
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
		saveWorkspaceInstructions,
		updateWorkspaceIdentity,
		updateWorkspaceRepository,
		workspaceOsAction,
		type EnvironmentProfileView,
		type RepositoryCatalogItem,
		type WorkspaceCheckpointView,
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
		| 'instructions'
		| 'environment'
		| 'memory'
		| 'checkpoints'
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
		onclose: () => void;
	}

	let { workspace, onclose }: Props = $props();

	const tabs: Array<{ id: TabId; label: string }> = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'general', label: 'General' },
		{ id: 'repositories', label: 'Repositories' },
		{ id: 'instructions', label: 'Instructions' },
		{ id: 'environment', label: 'Environment' },
		{ id: 'memory', label: 'Memory' },
		{ id: 'checkpoints', label: 'Checkpoints' },
		{ id: 'activity', label: 'Activity' },
		{ id: 'health', label: 'Health' }
	];

	let activeTab = $state<TabId>('overview');
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
	let instructionText = $state('');

	let environmentProfiles = $state<EnvironmentProfileView[]>([]);
	let newEnvironmentName = $state('');
	let newRuntimeProfile = $state('default');
	let targetDrafts = $state<Record<string, string>>({});

	let contextData = $state<Record<string, unknown> | null>(null);
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
			case 'instructions':
				return ['instructions'];
			case 'environment':
				return ['environment'];
			case 'memory':
				return ['memory', 'context'];
			case 'checkpoints':
				return ['checkpoints'];
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

	function markLoaded(tab: TabId) {
		loadedTabs = new Set([...loadedTabs, tab]);
		if (isLiveTarget) workspaceOsStore.markFresh(domainsForTab(tab));
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
					instructionText = instruction?.content ?? '';
					break;
				}
				case 'environment': {
					const envResult = await getWorkspaceEnvironmentProfiles(workspace.workspace_id);
					environmentProfiles = envResult.profiles ?? [];
					targetDrafts = Object.fromEntries(
						environmentProfiles.map((profile) => [profile.profile_id, profile.target_name ?? ''])
					);
					break;
				}
				case 'memory':
					contextData = (await getWorkspaceContext(workspace.workspace_id)) as unknown as Record<
						string,
						unknown
					>;
					break;
				case 'checkpoints': {
					const checkpointResult = await getWorkspaceCheckpoints(workspace.workspace_id);
					checkpoints = checkpointResult.checkpoints ?? [];
					checkpointMemoryVersion = checkpointResult.memory_version ?? 0;
					break;
				}
				case 'activity':
					await refreshProjectionSummary();
					break;
				case 'health':
					health = await getWorkspaceHealth(workspace.workspace_id);
					break;
			}
			markLoaded(tab);
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

	async function saveInstructions() {
		busy = true;
		try {
			const result = await saveWorkspaceInstructions(
				workspace.workspace_id,
				instructionText,
				instruction?.version ?? null,
				'Updated from native Workspace settings'
			);
			instruction = result.instruction;
			instructionText = result.instruction.content;
			const historyResult = await getWorkspaceInstructionHistory(workspace.workspace_id);
			instructionHistory = historyResult.instructions ?? [];
			refreshOverviewAfterMutation();
			toast.success('Workspace instructions saved');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function createEnvironment() {
		if (!newEnvironmentName.trim()) return;
		busy = true;
		try {
			await createWorkspaceEnvironment(workspace.workspace_id, {
				name: newEnvironmentName.trim(),
				initial_spec: {
					runtime_profile: newRuntimeProfile.trim() || 'default',
					environment_variables: {},
					packages: {},
					settings: {},
					credential_refs: []
				}
			});
			const result = await getWorkspaceEnvironmentProfiles(workspace.workspace_id);
			environmentProfiles = result.profiles ?? [];
			targetDrafts = Object.fromEntries(
				environmentProfiles.map((profile) => [profile.profile_id, profile.target_name ?? ''])
			);
			newEnvironmentName = '';
			refreshOverviewAfterMutation();
			toast.success('Environment profile created');
		} catch (e) {
			toast.error(message(e));
		} finally {
			busy = false;
		}
	}

	async function saveEnvironmentTarget(profile: EnvironmentProfileView) {
		busy = true;
		try {
			await workspaceOsAction('environment_set_target', {
				workspace_id: workspace.workspace_id,
				profile_id: profile.profile_id,
				target_name: targetDrafts[profile.profile_id]?.trim() || null
			});
			const result = await getWorkspaceEnvironmentProfiles(workspace.workspace_id);
			environmentProfiles = result.profiles ?? [];
			refreshOverviewAfterMutation();
			toast.success('Environment target updated');
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

	function pretty(value: unknown): string {
		return JSON.stringify(value, null, 2);
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
			<button class="rounded-lg px-3 py-1.5 text-sm hover:bg-[var(--app-hover)]" onclick={onclose}>
				Close
			</button>
		</header>

		<div class="flex min-h-0 flex-1 flex-col sm:flex-row">
			<nav
				class="flex shrink-0 gap-1 overflow-x-auto border-b border-[var(--app-border)] p-2 sm:w-44 sm:flex-col sm:border-b-0 sm:border-r"
			>
				{#each tabs as tab}
					<button
						class="whitespace-nowrap rounded-lg px-3 py-2 text-left text-sm transition-colors {activeTab ===
						tab.id
							? 'bg-[var(--app-hover)] font-medium'
							: 'text-[var(--app-muted-fg)] hover:bg-[var(--app-hover)]'}"
						onclick={() => (activeTab = tab.id)}
					>
						{tab.label}
					</button>
				{/each}
			</nav>

			<main class="min-h-0 flex-1 overflow-y-auto p-5">
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
				{:else if activeTab === 'instructions'}
					<section class="space-y-4">
						<div>
							<h3 class="text-base font-semibold">Workspace instructions</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Authoritative project instructions, loaded before Workspace memory.
							</p>
						</div>
						<textarea
							class="workspace-field min-h-64 resize-y font-mono text-xs"
							bind:value={instructionText}
						></textarea>
						<div class="flex items-center justify-between gap-3">
							<span class="text-xs text-[var(--app-muted-fg)]"
								>Current version: {instruction?.version ?? 0}</span
							>
							<button class="workspace-primary" disabled={busy} onclick={saveInstructions}
								>Save new version</button
							>
						</div>
						<div>
							<h4 class="mb-2 text-sm font-medium">History</h4>
							<div class="space-y-2">
								{#each instructionHistory.slice(0, 10) as item}
									<div class="rounded-lg border border-[var(--app-border)] p-3 text-xs">
										<div class="font-medium">
											v{item.version}
											{item.is_current ? '· current' : ''}
										</div>
										<div class="mt-1 text-[var(--app-muted-fg)]">
											{item.change_summary || item.content_hash}
										</div>
									</div>
								{/each}
							</div>
						</div>
					</section>
				{:else if activeTab === 'environment'}
					<section class="space-y-4">
						<div>
							<h3 class="text-base font-semibold">Environment</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Versioned runtime configuration. Secret material is never returned here.
							</p>
						</div>
						<div class="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
							<input
								class="workspace-field"
								placeholder="Profile name"
								bind:value={newEnvironmentName}
							/>
							<input
								class="workspace-field"
								placeholder="Runtime profile"
								bind:value={newRuntimeProfile}
							/>
							<button
								class="workspace-primary"
								disabled={busy || !newEnvironmentName.trim()}
								onclick={createEnvironment}>Create</button
							>
						</div>
						{#each environmentProfiles as profile}
							<div class="rounded-xl border border-[var(--app-border)] p-4">
								<div class="font-medium">{profile.name}</div>
								<div class="mt-1 text-xs text-[var(--app-muted-fg)]">
									{profile.active_version
										? `v${profile.active_version.version_number} · ${profile.active_version.runtime_profile}`
										: 'No active version'}
								</div>
								{#if profile.active_version}
									<div class="mt-2 text-xs text-[var(--app-muted-fg)]">
										{profile.active_version.environment_variable_names.length} non-sensitive env var(s)
										·
										{profile.active_version.credential_refs.length} credential reference(s)
									</div>
								{/if}
								<div class="mt-3 flex gap-2">
									<input
										class="workspace-field flex-1"
										placeholder="Target name (for example local or aws)"
										value={targetDrafts[profile.profile_id] ?? ''}
										oninput={(event) =>
											(targetDrafts = {
												...targetDrafts,
												[profile.profile_id]: event.currentTarget.value
											})}
									/>
									<button
										class="workspace-secondary"
										disabled={busy}
										onclick={() => saveEnvironmentTarget(profile)}>Save target</button
									>
								</div>
							</div>
						{/each}
					</section>
				{:else if activeTab === 'memory'}
					<section class="space-y-4">
						<div>
							<h3 class="text-base font-semibold">Workspace memory</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Stable UUID-scoped Memory Core context automatically loaded for this Workspace.
							</p>
						</div>
						<div class="grid gap-3 sm:grid-cols-3">
							<div class="workspace-stat">
								<span>Memory version</span>
								<strong>{String(contextData?.memory_version ?? 0)}</strong>
							</div>
							<div class="workspace-stat">
								<span>Instruction version</span>
								<strong>{String(contextData?.instruction_version ?? 0)}</strong>
							</div>
							<div class="workspace-stat">
								<span>Context snapshot</span>
								<strong class="truncate text-xs"
									>{String(
										(contextData?.context as Record<string, unknown> | undefined)?.snapshot_id ??
											'ready'
									)}</strong
								>
							</div>
						</div>
						<pre class="workspace-code">{pretty(
								(contextData?.context as Record<string, unknown> | undefined)?.diagnostics ?? []
							)}</pre>
					</section>
				{:else if activeTab === 'checkpoints'}
					<section class="space-y-4">
						<div>
							<h3 class="text-base font-semibold">Checkpoints</h3>
							<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
								Memory/execution recovery checkpoints. Restoring one never performs Git rollback.
							</p>
						</div>
						<div class="text-xs text-[var(--app-muted-fg)]">
							Memory namespace version: {checkpointMemoryVersion}
						</div>
						{#if checkpoints.length === 0}
							<p
								class="rounded-xl border border-dashed border-[var(--app-border)] p-5 text-sm text-[var(--app-muted-fg)]"
							>
								No checkpoints yet.
							</p>
						{/if}
						{#each checkpoints as checkpoint}
							<div class="rounded-xl border border-[var(--app-border)] p-4">
								<div class="flex items-center justify-between gap-3">
									<div class="font-medium">#{checkpoint.version} · {checkpoint.stage}</div>
									<div class="text-xs text-[var(--app-muted-fg)]">
										{new Date(checkpoint.created_at_ms).toLocaleString()}
									</div>
								</div>
								<div class="mt-1 text-xs text-[var(--app-muted-fg)]">
									{checkpoint.task_key || 'workspace'} · memory v{checkpoint.memory_version}
								</div>
							</div>
						{/each}
					</section>
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
			</main>
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
	.workspace-stat {
		display: flex;
		min-height: 5rem;
		flex-direction: column;
		justify-content: space-between;
		border: 1px solid var(--app-border);
		border-radius: 0.75rem;
		padding: 0.8rem;
	}
	.workspace-stat span {
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}
	.workspace-stat strong {
		margin-top: 0.5rem;
		font-size: 1rem;
	}
	.workspace-code {
		max-height: 18rem;
		overflow: auto;
		border: 1px solid var(--app-border);
		border-radius: 0.75rem;
		background: color-mix(in srgb, var(--app-bg) 94%, black);
		padding: 0.8rem;
		font-size: 0.72rem;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
