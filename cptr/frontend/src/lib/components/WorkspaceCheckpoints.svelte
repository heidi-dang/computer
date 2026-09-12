<script lang="ts">
	import type { WorkspaceCheckpointView } from '$lib/apis/workspace-os';

	interface Props {
		checkpoints: WorkspaceCheckpointView[];
		memoryVersion: number;
		onrefresh: () => void | Promise<void>;
	}

	let { checkpoints, memoryVersion, onrefresh }: Props = $props();

	let taskFilter = $state('all');
	let stageFilter = $state('all');

	const taskKeys = $derived(
		Array.from(new Set(checkpoints.map((checkpoint) => checkpoint.task_key || 'workspace'))).sort()
	);
	const stages = $derived(
		Array.from(new Set(checkpoints.map((checkpoint) => checkpoint.stage))).sort()
	);
	const visible = $derived(
		checkpoints.filter(
			(checkpoint) =>
				(taskFilter === 'all' || (checkpoint.task_key || 'workspace') === taskFilter) &&
				(stageFilter === 'all' || checkpoint.stage === stageFilter)
		)
	);

	function pretty(value: unknown): string {
		return JSON.stringify(value, null, 2);
	}

	function time(value: number): string {
		return new Date(value).toLocaleString();
	}
</script>

<section class="space-y-4" aria-label="Workspace checkpoints">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h3 class="text-base font-semibold">Checkpoints</h3>
				<span class="checkpoint-chip">Memory v{memoryVersion}</span>
			</div>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Read-only execution and memory recovery anchors. A checkpoint is not a Git rollback and no
				restore operation is exposed by the current Workspace OS contract.
			</p>
		</div>
		<button class="secondary" onclick={() => void onrefresh()}>Refresh</button>
	</div>

	<div class="checkpoint-toolbar">
		<label>
			<span>Task</span>
			<select bind:value={taskFilter}>
				<option value="all">All task keys</option>
				{#each taskKeys as key}
					<option value={key}>{key}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>Stage</span>
			<select bind:value={stageFilter}>
				<option value="all">All stages</option>
				{#each stages as stage}
					<option value={stage}>{stage}</option>
				{/each}
			</select>
		</label>
		<div class="checkpoint-count">
			<strong>{visible.length}</strong>
			<span>shown</span>
		</div>
	</div>

	{#if visible.length === 0}
		<div class="checkpoint-empty">No checkpoints match the selected filters.</div>
	{:else}
		<div class="checkpoint-list">
			{#each visible as checkpoint (checkpoint.checkpoint_id)}
				<article class="checkpoint-row">
					<div class="checkpoint-version">
						<span>v{checkpoint.version}</span>
						<small>mem {checkpoint.memory_version}</small>
					</div>
					<div class="min-w-0 flex-1">
						<div class="flex flex-wrap items-center gap-2">
							<strong>{checkpoint.stage}</strong>
							<span class="checkpoint-chip">{checkpoint.task_key || 'workspace'}</span>
							{#if checkpoint.parent_checkpoint_id}
								<span class="checkpoint-chip">has parent</span>
							{/if}
						</div>
						<p>{time(checkpoint.created_at_ms)}</p>
						<details>
							<summary>State snapshot</summary>
							<pre>{pretty(checkpoint.state)}</pre>
						</details>
					</div>
					<code>{checkpoint.checkpoint_id.slice(0, 10)}</code>
				</article>
			{/each}
		</div>
	{/if}
</section>

<style>
	.secondary {
		border: 1px solid var(--app-border);
		border-radius: 0.62rem;
		background: var(--app-hover);
		padding: 0.48rem 0.72rem;
		font-size: 0.72rem;
		font-weight: 600;
	}

	.checkpoint-chip {
		display: inline-flex;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.1rem 0.42rem;
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.checkpoint-toolbar {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
		gap: 0.65rem;
		align-items: end;
		border: 1px solid var(--app-border);
		border-radius: 0.82rem;
		padding: 0.75rem;
	}

	.checkpoint-toolbar label {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	.checkpoint-toolbar label > span,
	.checkpoint-count span {
		font-size: 0.63rem;
		color: var(--app-muted-fg);
	}

	.checkpoint-toolbar select {
		border: 1px solid var(--app-border);
		border-radius: 0.58rem;
		background: var(--app-bg);
		padding: 0.46rem 0.55rem;
		font-size: 0.74rem;
	}

	.checkpoint-count {
		display: flex;
		min-width: 5.5rem;
		flex-direction: column;
		border-left: 1px solid var(--app-border);
		padding-left: 0.7rem;
	}

	.checkpoint-count strong {
		font-size: 0.86rem;
	}

	.checkpoint-list {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		overflow: hidden;
	}

	.checkpoint-row {
		display: flex;
		align-items: flex-start;
		gap: 0.8rem;
		padding: 0.8rem;
	}

	.checkpoint-row + .checkpoint-row {
		border-top: 1px solid var(--app-border);
	}

	.checkpoint-version {
		display: flex;
		min-width: 3.7rem;
		flex-direction: column;
		gap: 0.15rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	}

	.checkpoint-version span {
		font-size: 0.78rem;
		font-weight: 650;
	}

	.checkpoint-version small,
	.checkpoint-row p {
		font-size: 0.63rem;
		color: var(--app-muted-fg);
	}

	.checkpoint-row strong {
		font-size: 0.76rem;
	}

	.checkpoint-row p {
		margin-top: 0.25rem;
	}

	.checkpoint-row > code {
		font-size: 0.62rem;
		color: var(--app-muted-fg);
	}

	.checkpoint-row details {
		margin-top: 0.55rem;
	}

	.checkpoint-row summary {
		cursor: pointer;
		font-size: 0.66rem;
		font-weight: 600;
		color: var(--app-muted-fg);
	}

	.checkpoint-row pre {
		max-height: 14rem;
		overflow: auto;
		margin-top: 0.45rem;
		border: 1px solid var(--app-border);
		border-radius: 0.6rem;
		padding: 0.65rem;
		font-size: 0.64rem;
		line-height: 1.4;
		white-space: pre-wrap;
		word-break: break-word;
	}

	.checkpoint-empty {
		border: 1px dashed var(--app-border);
		border-radius: 0.82rem;
		padding: 1.25rem;
		text-align: center;
		font-size: 0.74rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 640px) {
		.checkpoint-toolbar {
			grid-template-columns: 1fr 1fr;
		}

		.checkpoint-count {
			grid-column: 1 / -1;
			border-left: 0;
			border-top: 1px solid var(--app-border);
			padding: 0.55rem 0 0;
		}

		.checkpoint-row {
			flex-wrap: wrap;
		}

		.checkpoint-row > code {
			width: 100%;
			padding-left: 4.5rem;
		}
	}
</style>
