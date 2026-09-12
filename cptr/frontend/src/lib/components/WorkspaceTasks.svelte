<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		addWorkspaceTaskEvidence,
		createWorkspaceTask,
		getWorkspaceTaskSummary,
		listWorkspaceTasks,
		pinWorkspaceTaskRepository,
		updateWorkspaceTaskStatus,
		type WorkspaceTaskSummaryView,
		type WorkspaceTaskView
	} from '$lib/apis/workspace-os';

	interface Props {
		workspaceId: string;
		stale?: boolean;
		onfresh?: () => void;
	}

	let { workspaceId, stale = false, onfresh = () => {} }: Props = $props();

	let tasks = $state<WorkspaceTaskView[]>([]);
	let selectedTaskId = $state('');
	let summary = $state<WorkspaceTaskSummaryView | null>(null);
	let loading = $state(false);
	let busy = $state('');
	let error = $state('');
	let newTitle = $state('');
	let newDescription = $state('');
	let statusFilter = $state('all');
	let repoPath = $state('.');
	let evidenceKind = $state('test');
	let evidenceStatus = $state('PASSED');
	let evidenceSummary = $state('');
	let evidenceCommand = $state('');
	let evidenceRepoPath = $state('');

	const filteredTasks = $derived(
		statusFilter === 'all' ? tasks : tasks.filter((task) => task.status === statusFilter)
	);
	const selectedTask = $derived(tasks.find((task) => task.id === selectedTaskId) ?? null);

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Workspace Task operation failed';
	}

	async function loadTasks(preserveSelection = true) {
		loading = true;
		error = '';
		try {
			const result = await listWorkspaceTasks(workspaceId);
			tasks = result.tasks;
			const current = preserveSelection ? selectedTaskId : '';
			selectedTaskId =
				(current && tasks.some((task) => task.id === current) ? current : tasks[0]?.id) ?? '';
			if (selectedTaskId) {
				await loadSummary(selectedTaskId);
			} else {
				summary = null;
			}
			onfresh();
		} catch (cause) {
			error = message(cause);
		} finally {
			loading = false;
		}
	}

	async function loadSummary(taskId: string) {
		if (!taskId) {
			summary = null;
			return;
		}
		try {
			const result = await getWorkspaceTaskSummary(workspaceId, taskId);
			summary = result.summary;
		} catch (cause) {
			error = message(cause);
		}
	}

	async function selectTask(taskId: string) {
		selectedTaskId = taskId;
		await loadSummary(taskId);
	}

	async function createTask() {
		const title = newTitle.trim();
		if (!title) return;
		busy = 'create';
		try {
			const result = await createWorkspaceTask(workspaceId, {
				title,
				description: newDescription.trim()
			});
			newTitle = '';
			newDescription = '';
			selectedTaskId = result.task.id;
			await loadTasks(true);
			toast.success('Workspace Task created');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	async function setStatus(status: string) {
		if (!selectedTask) return;
		busy = 'status';
		try {
			await updateWorkspaceTaskStatus(workspaceId, selectedTask.id, status);
			await loadTasks(true);
			toast.success('Task status updated');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	async function pinRepository() {
		if (!selectedTask) return;
		busy = 'pin';
		try {
			await pinWorkspaceTaskRepository(workspaceId, selectedTask.id, {
				repo_path: repoPath.trim() || '.'
			});
			await loadSummary(selectedTask.id);
			toast.success('Repository revision pinned');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	async function addEvidence() {
		if (!selectedTask || !evidenceSummary.trim()) return;
		busy = 'evidence';
		try {
			await addWorkspaceTaskEvidence(workspaceId, selectedTask.id, {
				kind: evidenceKind,
				status: evidenceStatus,
				summary: evidenceSummary.trim(),
				command: evidenceCommand.trim() || undefined,
				repo_path: evidenceRepoPath.trim() || undefined
			});
			evidenceSummary = '';
			evidenceCommand = '';
			evidenceRepoPath = '';
			await loadSummary(selectedTask.id);
			toast.success('Verification evidence recorded');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busy = '';
		}
	}

	$effect(() => {
		if (stale && !loading && !busy) {
			void loadTasks(true);
		}
	});

	onMount(() => {
		void loadTasks(false);
	});
</script>

<section class="space-y-5" aria-label="Workspace Tasks">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<h3 class="text-base font-semibold">Workspace Tasks</h3>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Durable coordination across repository revisions, Direct Coding Workers and verification
				evidence. Execution remains owned by the existing worker subsystem.
			</p>
		</div>
		<button
			class="secondary"
			disabled={loading || Boolean(busy)}
			onclick={() => void loadTasks(true)}
		>
			{loading ? 'Refreshing…' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="task-error">{error}</div>
	{/if}

	<div class="task-create">
		<div class="grid gap-2 md:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)_auto]">
			<input class="field" placeholder="Task title" bind:value={newTitle} />
			<input class="field" placeholder="Description" bind:value={newDescription} />
			<button
				class="primary"
				disabled={busy === 'create' || !newTitle.trim()}
				onclick={() => void createTask()}
			>
				Create task
			</button>
		</div>
	</div>

	<div class="task-layout">
		<aside class="task-list">
			<div class="task-list-toolbar">
				<select bind:value={statusFilter} aria-label="Filter tasks by status">
					<option value="all">All statuses</option>
					<option value="OPEN">Open</option>
					<option value="IN_PROGRESS">In progress</option>
					<option value="VERIFIED">Verified</option>
					<option value="INTEGRATED">Integrated</option>
					<option value="CLOSED">Closed</option>
					<option value="FAILED">Failed</option>
				</select>
				<span>{filteredTasks.length}</span>
			</div>
			{#if filteredTasks.length === 0}
				<div class="task-empty">No Workspace Tasks match this filter.</div>
			{:else}
				{#each filteredTasks as task (task.id)}
					<button
						class="task-list-item"
						class:selected={task.id === selectedTaskId}
						onclick={() => void selectTask(task.id)}
					>
						<div class="flex items-center justify-between gap-2">
							<strong>{task.title}</strong>
							<span class="task-chip" data-status={task.status}>{task.status}</span>
						</div>
						<p>{task.description || 'No description'}</p>
						<small>{new Date(task.updated_at).toLocaleString()}</small>
					</button>
				{/each}
			{/if}
		</aside>

		<div class="task-detail">
			{#if !selectedTask}
				<div class="task-empty">Select or create a Workspace Task.</div>
			{:else}
				<div class="task-detail-heading">
					<div>
						<div class="flex flex-wrap items-center gap-2">
							<h4>{selectedTask.title}</h4>
							<span class="task-chip" data-status={selectedTask.status}>{selectedTask.status}</span>
						</div>
						<p>{selectedTask.description || selectedTask.id}</p>
					</div>
				</div>

				<div class="task-stats">
					<div>
						<span>Evidence</span>
						<strong>{summary?.verification.total ?? 0}</strong>
					</div>
					<div>
						<span>Passed</span>
						<strong>{summary?.verification.passed ?? 0}</strong>
					</div>
					<div>
						<span>Failed</span>
						<strong>{summary?.verification.failed ?? 0}</strong>
					</div>
					<div>
						<span>Repositories</span>
						<strong>{summary?.repositories.length ?? 0}</strong>
					</div>
					<div>
						<span>Workers</span>
						<strong>{summary?.workers.length ?? 0}</strong>
					</div>
					<div>
						<span>Verified</span>
						<strong>{summary?.verification.verified ? 'Yes' : 'No'}</strong>
					</div>
				</div>

				<div class="task-section">
					<div class="section-heading">
						<div>
							<h5>Status transition</h5>
							<p>Backend state-machine rules remain authoritative.</p>
						</div>
					</div>
					<div class="status-actions">
						{#each ['OPEN', 'IN_PROGRESS', 'VERIFIED', 'INTEGRATED', 'CLOSED', 'FAILED'] as status}
							<button
								class="secondary compact"
								class:active-status={selectedTask.status === status}
								disabled={Boolean(busy) ||
									selectedTask.status === status ||
									(status === 'INTEGRATED' &&
										!(selectedTask.status === 'VERIFIED' && summary?.verification.verified))}
								onclick={() => void setStatus(status)}
							>
								{status.replaceAll('_', ' ')}
							</button>
						{/each}
					</div>
				</div>

				<div class="task-section">
					<div class="section-heading">
						<div>
							<h5>Repository revision pins</h5>
							<p>Pin a workspace-relative Git repository to its current revision.</p>
						</div>
					</div>
					<div class="inline-form">
						<input class="field" placeholder="Repo path" bind:value={repoPath} />
						<button class="secondary" disabled={Boolean(busy)} onclick={() => void pinRepository()}>
							Pin current revision
						</button>
					</div>
					{#if (summary?.repositories.length ?? 0) > 0}
						<div class="repository-list">
							{#each summary?.repositories ?? [] as repository}
								<div class="repository-row">
									<div>
										<strong>{repository.repo_path}</strong>
										<small>{repository.branch || 'detached'}</small>
									</div>
									<div class="repo-revisions">
										<code>{repository.pinned_revision?.slice(0, 9) || 'unknown'}</code>
										<span>→</span>
										<code
											>{(repository.current_revision ?? repository.head_revision)?.slice(0, 9) ||
												'unknown'}</code
										>
										{#if repository.is_diverged}<span class="danger-text">diverged</span>{/if}
									</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<div class="task-section">
					<div class="section-heading">
						<div>
							<h5>Add verification evidence</h5>
							<p>
								Evidence is durable task state; record observed facts, not unsupported success
								claims.
							</p>
						</div>
					</div>
					<div class="evidence-grid">
						<select class="field" bind:value={evidenceKind}>
							<option value="test">Test</option>
							<option value="lint">Lint</option>
							<option value="typecheck">Typecheck</option>
							<option value="diff">Diff</option>
							<option value="review">Review</option>
							<option value="command">Command</option>
							<option value="manual">Manual</option>
						</select>
						<select class="field" bind:value={evidenceStatus}>
							<option value="PASSED">Passed</option>
							<option value="FAILED">Failed</option>
							<option value="PENDING">Pending</option>
							<option value="OBSERVED">Observed</option>
						</select>
						<input class="field" placeholder="Repo path (optional)" bind:value={evidenceRepoPath} />
						<input class="field" placeholder="Command (optional)" bind:value={evidenceCommand} />
						<textarea
							class="field evidence-summary"
							placeholder="Evidence summary"
							bind:value={evidenceSummary}
						></textarea>
						<button
							class="primary"
							disabled={Boolean(busy) || !evidenceSummary.trim()}
							onclick={() => void addEvidence()}
						>
							Record evidence
						</button>
					</div>
				</div>

				{#if summary?.verification.failures.length}
					<div class="task-section failure-section">
						<div class="section-heading">
							<div>
								<h5>Verification failures</h5>
								<p>These failures prevent the verification summary from becoming green.</p>
							</div>
						</div>
						<ul>
							{#each summary.verification.failures as failure}
								<li>{failure}</li>
							{/each}
						</ul>
					</div>
				{/if}
			{/if}
		</div>
	</div>
</section>

<style>
	.primary,
	.secondary {
		border-radius: 0.62rem;
		padding: 0.48rem 0.72rem;
		font-size: 0.71rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}

	.secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.primary:disabled,
	.secondary:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.compact {
		padding: 0.34rem 0.5rem;
		font-size: 0.63rem;
	}

	.active-status {
		border-color: color-mix(in srgb, var(--app-fg) 35%, var(--app-border));
		background: color-mix(in srgb, var(--app-hover) 70%, var(--app-fg) 5%);
	}

	.field {
		width: 100%;
		min-width: 0;
		border: 1px solid var(--app-border);
		border-radius: 0.6rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		padding: 0.48rem 0.62rem;
		font-size: 0.74rem;
		outline: none;
	}

	.task-error {
		border: 1px solid rgb(239 68 68 / 0.28);
		border-radius: 0.75rem;
		padding: 0.75rem;
		font-size: 0.72rem;
		color: rgb(239 68 68);
	}

	.task-create,
	.task-list,
	.task-detail {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
	}

	.task-create {
		padding: 0.75rem;
	}

	.task-layout {
		display: grid;
		grid-template-columns: minmax(13rem, 0.36fr) minmax(0, 1fr);
		gap: 0.8rem;
		align-items: start;
	}

	.task-list {
		overflow: hidden;
	}

	.task-list-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		border-bottom: 1px solid var(--app-border);
		padding: 0.6rem;
	}

	.task-list-toolbar select {
		min-width: 0;
		flex: 1;
		border: 1px solid var(--app-border);
		border-radius: 0.55rem;
		background: var(--app-bg);
		padding: 0.4rem 0.5rem;
		font-size: 0.68rem;
	}

	.task-list-toolbar span {
		font-size: 0.65rem;
		color: var(--app-muted-fg);
	}

	.task-list-item {
		display: block;
		width: 100%;
		padding: 0.7rem;
		text-align: left;
	}

	.task-list-item + .task-list-item {
		border-top: 1px solid var(--app-border);
	}

	.task-list-item:hover,
	.task-list-item.selected {
		background: var(--app-hover);
	}

	.task-list-item strong,
	.task-detail-heading h4 {
		font-size: 0.75rem;
		font-weight: 650;
	}

	.task-list-item p,
	.task-list-item small,
	.task-detail-heading p,
	.section-heading p,
	.repository-row small {
		display: block;
		margin-top: 0.2rem;
		font-size: 0.63rem;
		color: var(--app-muted-fg);
	}

	.task-chip {
		display: inline-flex;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.08rem 0.38rem;
		font-size: 0.57rem;
		color: var(--app-muted-fg);
	}

	.task-chip[data-status='VERIFIED'],
	.task-chip[data-status='INTEGRATED'],
	.task-chip[data-status='CLOSED'] {
		color: rgb(34 197 94);
		border-color: rgb(34 197 94 / 0.25);
	}

	.task-chip[data-status='FAILED'] {
		color: rgb(239 68 68);
		border-color: rgb(239 68 68 / 0.25);
	}

	.task-detail {
		overflow: hidden;
	}

	.task-detail-heading,
	.section-heading {
		padding: 0.75rem 0.85rem;
	}

	.task-stats {
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		border-top: 1px solid var(--app-border);
	}

	.task-stats > div {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		padding: 0.65rem;
	}

	.task-stats > div + div {
		border-left: 1px solid var(--app-border);
	}

	.task-stats span {
		font-size: 0.59rem;
		color: var(--app-muted-fg);
	}

	.task-stats strong {
		font-size: 0.76rem;
	}

	.task-section {
		border-top: 1px solid var(--app-border);
	}

	.section-heading h5 {
		font-size: 0.72rem;
		font-weight: 650;
	}

	.status-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		padding: 0 0.85rem 0.8rem;
	}

	.inline-form {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.5rem;
		padding: 0 0.85rem 0.8rem;
	}

	.repository-list {
		border-top: 1px solid var(--app-border);
	}

	.repository-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.7rem;
		padding: 0.65rem 0.85rem;
	}

	.repository-row + .repository-row {
		border-top: 1px solid var(--app-border);
	}

	.repository-row strong {
		font-size: 0.68rem;
	}

	.repo-revisions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: flex-end;
		gap: 0.35rem;
		font-size: 0.61rem;
		color: var(--app-muted-fg);
	}

	.repo-revisions code {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	}

	.danger-text {
		color: rgb(239 68 68);
	}

	.evidence-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.5rem;
		padding: 0 0.85rem 0.85rem;
	}

	.evidence-summary {
		min-height: 4.5rem;
		resize: vertical;
	}

	.failure-section ul {
		border-top: 1px solid rgb(239 68 68 / 0.2);
	}

	.failure-section li {
		padding: 0.6rem 0.85rem;
		font-size: 0.68rem;
		color: rgb(239 68 68);
	}

	.task-empty {
		padding: 1.25rem;
		text-align: center;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 900px) {
		.task-layout {
			grid-template-columns: 1fr;
		}

		.task-stats {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}

		.task-stats > div:nth-child(4) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.task-stats > div:nth-child(5),
		.task-stats > div:nth-child(6) {
			border-top: 1px solid var(--app-border);
		}
	}

	@media (max-width: 640px) {
		.inline-form,
		.evidence-grid {
			grid-template-columns: 1fr;
		}

		.task-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.task-stats > div:nth-child(3),
		.task-stats > div:nth-child(5) {
			border-left: 0;
		}

		.task-stats > div:nth-child(n + 3) {
			border-top: 1px solid var(--app-border);
		}

		.repository-row {
			align-items: flex-start;
			flex-direction: column;
		}

		.repo-revisions {
			justify-content: flex-start;
		}
	}
</style>
