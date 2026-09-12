<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		getMemory,
		searchMemory,
		updateMemory,
		type MemorySnippet,
		type MemoryState
	} from '$lib/apis/memory';
	import { getWorkspaceContext, type WorkspaceContextView } from '$lib/apis/workspace-os';

	interface Props {
		workspaceId: string;
		workspacePath: string;
		initialContext: WorkspaceContextView | null;
		onrefresh: () => void | Promise<void>;
	}

	let { workspaceId, workspacePath, initialContext, onrefresh }: Props = $props();

	let context = $state<WorkspaceContextView | null>(null);
	let memory = $state<MemoryState | null>(null);
	let memoryDraft = $state('');
	let searchQuery = $state('');
	let searchResults = $state<MemorySnippet[]>([]);
	let loading = $state(false);
	let writing = $state(false);
	let searching = $state(false);
	let error = $state('');
	let loadGeneration = 0;
	let searchGeneration = 0;

	$effect(() => {
		if (initialContext) context = initialContext;
	});

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Workspace context operation failed';
	}

	function pretty(value: unknown): string {
		return JSON.stringify(value, null, 2);
	}

	async function load() {
		const generation = ++loadGeneration;
		loading = true;
		error = '';
		try {
			const [contextResult, memoryResult] = await Promise.all([
				getWorkspaceContext(workspaceId),
				getMemory(workspacePath)
			]);
			if (generation !== loadGeneration) return;
			context = contextResult;
			memory = memoryResult;
		} catch (cause) {
			if (generation !== loadGeneration) return;
			error = message(cause);
		} finally {
			if (generation === loadGeneration) loading = false;
		}
	}

	async function addMemory() {
		const content = memoryDraft.trim();
		if (!content) return;
		writing = true;
		try {
			await updateMemory('workspace', workspacePath, [{ action: 'add', content }]);
			memoryDraft = '';
			await Promise.all([load(), onrefresh()]);
			toast.success('Workspace memory added');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			writing = false;
		}
	}

	async function runSearch() {
		const generation = ++searchGeneration;
		const query = searchQuery.trim();
		if (!query) {
			searchResults = [];
			return;
		}
		searching = true;
		try {
			const result = await searchMemory({
				query,
				scope: 'workspace',
				workspace: workspacePath,
				limit: 12,
				expand_links: true
			});
			if (generation !== searchGeneration || searchQuery.trim() !== query) return;
			searchResults = result.results;
		} catch (cause) {
			if (generation !== searchGeneration || searchQuery.trim() !== query) return;
			toast.error(message(cause));
		} finally {
			searching = false;
		}
	}

	onMount(() => {
		void load();
	});

	onDestroy(() => {
		loadGeneration += 1;
		searchGeneration += 1;
	});
</script>

<section class="space-y-5" aria-label="Workspace context and memory">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<h3 class="text-base font-semibold">Context & memory</h3>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Compiled Workspace context combines versioned instructions, managed memory, environment
				state and repository context.
			</p>
		</div>
		<button class="secondary" disabled={loading} onclick={() => void load()}>
			{loading ? 'Refreshing…' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="context-error">{error}</div>
	{/if}

	<div class="context-stats">
		<div>
			<span>Memory version</span>
			<strong>{context?.memory_version ?? 0}</strong>
		</div>
		<div>
			<span>Instruction version</span>
			<strong>{context?.instruction_version ?? 0}</strong>
		</div>
		<div>
			<span>Snapshot</span>
			<strong class="mono">{context?.context.snapshot_id?.slice(0, 12) ?? 'none'}</strong>
		</div>
		<div>
			<span>Content digest</span>
			<strong class="mono">{context?.context.content_digest?.slice(0, 12) ?? 'none'}</strong>
		</div>
	</div>

	<div class="grid gap-3 lg:grid-cols-2">
		<div class="context-panel">
			<div class="panel-heading">
				<div>
					<h4>Context diagnostics</h4>
					<p>Bounded diagnostics from the canonical context compiler.</p>
				</div>
			</div>
			{#if (context?.context.diagnostics?.length ?? 0) === 0}
				<div class="context-empty">No context diagnostics reported.</div>
			{:else}
				<ul class="diagnostics">
					{#each context?.context.diagnostics ?? [] as diagnostic}
						<li>{diagnostic}</li>
					{/each}
				</ul>
			{/if}
		</div>

		<div class="context-panel">
			<div class="panel-heading">
				<div>
					<h4>Managed memory</h4>
					<p>Filesystem-backed managed memory for this Workspace.</p>
				</div>
			</div>
			<div class="memory-summary">
				<div>
					<span>Usage</span>
					<strong>{memory?.workspace.usage ?? 'unavailable'}</strong>
				</div>
				<div>
					<span>Entries</span>
					<strong>{memory?.workspace.entries.length ?? 0}</strong>
				</div>
				<div>
					<span>Memory tool</span>
					<strong>{memory?.settings.tool_enabled ? 'Enabled' : 'Disabled'}</strong>
				</div>
			</div>
		</div>
	</div>

	<div class="context-panel">
		<div class="panel-heading">
			<div>
				<h4>Add Workspace memory</h4>
				<p>Persist a fact or procedure scoped only to this Workspace.</p>
			</div>
		</div>
		<div class="memory-compose">
			<textarea
				class="memory-editor"
				placeholder="Add a durable Workspace-specific fact, convention or procedure…"
				bind:value={memoryDraft}
			></textarea>
			<div class="compose-footer">
				<span>{memoryDraft.trim().length} characters</span>
				<button
					class="primary"
					disabled={writing || !memoryDraft.trim()}
					onclick={() => void addMemory()}
				>
					{writing ? 'Saving…' : 'Add memory'}
				</button>
			</div>
		</div>
	</div>

	<div class="context-panel">
		<div class="panel-heading">
			<div>
				<h4>Search Workspace memory</h4>
				<p>Search canonical memory snippets without changing recall state.</p>
			</div>
		</div>
		<div class="search-row">
			<input
				class="field"
				placeholder="Search memory…"
				bind:value={searchQuery}
				onkeydown={(event) => event.key === 'Enter' && void runSearch()}
			/>
			<button class="secondary" disabled={searching} onclick={() => void runSearch()}>
				{searching ? 'Searching…' : 'Search'}
			</button>
		</div>
		{#if searchResults.length > 0}
			<div class="search-results">
				{#each searchResults as result (result.memory_id)}
					<article>
						<div class="flex flex-wrap items-center gap-2">
							<strong>{result.heading || result.path}</strong>
							<span class="context-chip">{result.scope}</span>
						</div>
						<p>{result.snippet}</p>
						<small>{result.reason}</small>
					</article>
				{/each}
			</div>
		{/if}
	</div>

	<details class="context-advanced">
		<summary>Compiled context details</summary>
		<div class="grid gap-3 pt-3 lg:grid-cols-2">
			<div>
				<h5>Environment profile</h5>
				<pre>{pretty(context?.environment_profile ?? {})}</pre>
			</div>
			<div>
				<h5>Context snapshot</h5>
				<pre>{pretty(context?.context ?? {})}</pre>
			</div>
		</div>
	</details>
</section>

<style>
	.primary,
	.secondary {
		border-radius: 0.62rem;
		padding: 0.48rem 0.72rem;
		font-size: 0.72rem;
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

	.context-error {
		border: 1px solid rgb(239 68 68 / 0.28);
		border-radius: 0.75rem;
		padding: 0.75rem;
		font-size: 0.74rem;
		color: rgb(239 68 68);
	}

	.context-stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		overflow: hidden;
	}

	.context-stats > div,
	.memory-summary > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.75rem;
	}

	.context-stats > div + div,
	.memory-summary > div + div {
		border-left: 1px solid var(--app-border);
	}

	.context-stats span,
	.memory-summary span,
	.panel-heading p,
	.compose-footer,
	.search-results small {
		font-size: 0.64rem;
		color: var(--app-muted-fg);
	}

	.context-stats strong,
	.memory-summary strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.78rem;
		white-space: nowrap;
	}

	.context-panel,
	.context-advanced {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		overflow: hidden;
	}

	.panel-heading {
		padding: 0.75rem 0.85rem;
		border-bottom: 1px solid var(--app-border);
	}

	.panel-heading h4 {
		font-size: 0.78rem;
		font-weight: 650;
	}

	.memory-summary {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}

	.context-empty {
		padding: 1rem;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	.diagnostics li {
		padding: 0.65rem 0.85rem;
		font-size: 0.72rem;
	}

	.diagnostics li + li {
		border-top: 1px solid var(--app-border);
	}

	.memory-compose {
		padding: 0.8rem;
	}

	.memory-editor {
		width: 100%;
		min-height: 7rem;
		resize: vertical;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		padding: 0.7rem;
		font-size: 0.74rem;
		line-height: 1.45;
		outline: none;
	}

	.compose-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding-top: 0.6rem;
	}

	.search-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.55rem;
		padding: 0.8rem;
	}

	.field {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.62rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		padding: 0.5rem 0.65rem;
		font-size: 0.76rem;
		outline: none;
	}

	.search-results {
		border-top: 1px solid var(--app-border);
	}

	.search-results article {
		padding: 0.75rem 0.85rem;
	}

	.search-results article + article {
		border-top: 1px solid var(--app-border);
	}

	.search-results strong {
		font-size: 0.74rem;
	}

	.search-results p {
		margin-top: 0.3rem;
		font-size: 0.71rem;
		line-height: 1.45;
	}

	.search-results small {
		display: block;
		margin-top: 0.3rem;
	}

	.context-chip {
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.08rem 0.38rem;
		font-size: 0.59rem;
		color: var(--app-muted-fg);
	}

	.context-advanced {
		padding: 0.75rem 0.85rem;
	}

	.context-advanced summary {
		cursor: pointer;
		font-size: 0.74rem;
		font-weight: 650;
	}

	.context-advanced h5 {
		margin-bottom: 0.4rem;
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.context-advanced pre {
		max-height: 18rem;
		overflow: auto;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		padding: 0.7rem;
		font-size: 0.66rem;
		line-height: 1.4;
		white-space: pre-wrap;
		word-break: break-word;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	}

	@media (max-width: 720px) {
		.context-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.context-stats > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.context-stats > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}

		.memory-summary {
			grid-template-columns: 1fr;
		}

		.memory-summary > div + div {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.search-row {
			grid-template-columns: 1fr;
		}
	}
</style>
