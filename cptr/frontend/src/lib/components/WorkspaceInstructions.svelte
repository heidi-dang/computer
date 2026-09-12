<script lang="ts">
	import { toast } from 'svelte-sonner';
	import {
		getWorkspaceInstructionHistory,
		previewWorkspaceInstructions,
		saveWorkspaceInstructions,
		type WorkspaceInstruction,
		type WorkspaceInstructionPreview
	} from '$lib/apis/workspace-os';

	interface Props {
		workspaceId: string;
		instruction: WorkspaceInstruction | null;
		history: WorkspaceInstruction[];
		onrefresh: () => void | Promise<void>;
	}

	let { workspaceId, instruction, history, onrefresh }: Props = $props();

	let draft = $state('');
	let changeSummary = $state('');
	let preview = $state<WorkspaceInstructionPreview | null>(null);
	let previewing = $state(false);
	let saving = $state(false);
	let error = $state('');
	let loadedVersion = $state<number | null>(null);

	$effect(() => {
		const version = instruction?.version ?? 0;
		if (loadedVersion === null || loadedVersion === version) {
			draft = instruction?.content ?? '';
			loadedVersion = version;
		}
	});

	const dirty = $derived(draft !== (instruction?.content ?? ''));

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Workspace instruction operation failed';
	}

	async function runPreview() {
		previewing = true;
		error = '';
		try {
			const result = await previewWorkspaceInstructions(workspaceId, draft);
			preview = result.preview;
		} catch (cause) {
			error = message(cause);
		} finally {
			previewing = false;
		}
	}

	async function save() {
		saving = true;
		error = '';
		try {
			const result = await saveWorkspaceInstructions(
				workspaceId,
				draft,
				instruction?.version ?? null,
				changeSummary.trim() || undefined
			);
			loadedVersion = result.instruction.version;
			changeSummary = '';
			preview = null;
			toast.success('Workspace instructions saved');
			await onrefresh();
		} catch (cause) {
			error = message(cause);
			toast.error(error);
		} finally {
			saving = false;
		}
	}

	function loadHistorical(item: WorkspaceInstruction) {
		draft = item.content;
		loadedVersion = instruction?.version ?? 0;
		changeSummary =
			item.version === instruction?.version
				? ''
				: 'Restore content from instruction v' + item.version + ' as a new version';
		preview = null;
	}

	async function reloadHistory() {
		try {
			await getWorkspaceInstructionHistory(workspaceId, 50);
			await onrefresh();
		} catch (cause) {
			toast.error(message(cause));
		}
	}
</script>

<section class="space-y-4" aria-label="Workspace instructions">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<div class="flex flex-wrap items-center gap-2">
				<h3 class="text-base font-semibold">Workspace instructions</h3>
				<span class="instruction-chip">v{instruction?.version ?? 0}</span>
				{#if dirty}<span class="instruction-chip pending">Unsaved</span>{/if}
			</div>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Versioned user-authored steering compiled before Workspace memory. Historical recovery
				always creates a new version.
			</p>
		</div>
		<button class="secondary" disabled={previewing || saving} onclick={() => void reloadHistory()}>
			Refresh
		</button>
	</div>

	{#if error}
		<div class="instruction-error">{error}</div>
	{/if}

	<textarea
		class="instruction-editor"
		aria-label="Workspace instruction draft"
		bind:value={draft}
		oninput={() => (preview = null)}
	></textarea>

	<div class="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto_auto]">
		<input class="field" placeholder="Change summary (recommended)" bind:value={changeSummary} />
		<button class="secondary" disabled={previewing} onclick={() => void runPreview()}>
			{previewing ? 'Compiling…' : 'Preview compiled'}
		</button>
		<button class="primary" disabled={saving || !dirty} onclick={() => void save()}>
			{saving ? 'Saving…' : 'Save new version'}
		</button>
	</div>

	{#if preview}
		<div class="preview-card">
			<div class="preview-stats">
				<div><span>Source</span><strong>{preview.source}</strong></div>
				<div><span>Characters</span><strong>{preview.char_count}</strong></div>
				<div><span>Estimated tokens</span><strong>{preview.estimated_tokens}</strong></div>
				<div>
					<span>Digest</span>
					<strong class="mono">{preview.content_hash?.slice(0, 12) ?? 'none'}</strong>
				</div>
			</div>
			<details open>
				<summary>Compiled instruction block</summary>
				<pre>{preview.compiled_instructions}</pre>
			</details>
		</div>
	{/if}

	<div>
		<div class="history-heading">
			<div>
				<h4>Version history</h4>
				<p>{history.length} loaded version{history.length === 1 ? '' : 's'}</p>
			</div>
		</div>
		{#if history.length === 0}
			<div class="history-empty">No persisted instruction versions yet.</div>
		{:else}
			<div class="history-list">
				{#each history.slice(0, 50) as item (item.id)}
					<article class="history-row" class:current={item.is_current}>
						<div class="min-w-0">
							<div class="flex flex-wrap items-center gap-2">
								<strong>v{item.version}</strong>
								{#if item.is_current}<span class="instruction-chip">Current</span>{/if}
								<span class="mono digest">{item.content_hash.slice(0, 10)}</span>
							</div>
							<p>{item.change_summary || 'No change summary'}</p>
							<small>{new Date(item.created_at * 1000).toLocaleString()}</small>
						</div>
						<button class="secondary compact" onclick={() => loadHistorical(item)}>
							{item.is_current ? 'Load current' : 'Restore as draft'}
						</button>
					</article>
				{/each}
			</div>
		{/if}
	</div>
</section>

<style>
	.field,
	.instruction-editor {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.65rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		outline: none;
	}

	.field {
		padding: 0.52rem 0.65rem;
		font-size: 0.78rem;
	}

	.instruction-editor {
		min-height: 18rem;
		resize: vertical;
		padding: 0.8rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.74rem;
		line-height: 1.5;
	}

	.field:focus,
	.instruction-editor:focus {
		border-color: color-mix(in srgb, var(--app-fg) 35%, var(--app-border));
	}

	.primary,
	.secondary {
		border-radius: 0.65rem;
		padding: 0.5rem 0.75rem;
		font-size: 0.74rem;
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
		padding: 0.36rem 0.55rem;
		font-size: 0.66rem;
	}

	.instruction-chip {
		display: inline-flex;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.1rem 0.45rem;
		font-size: 0.62rem;
		color: var(--app-muted-fg);
	}

	.instruction-chip.pending {
		color: rgb(234 179 8);
		border-color: rgb(234 179 8 / 0.28);
	}

	.instruction-error {
		border: 1px solid rgb(239 68 68 / 0.28);
		border-radius: 0.7rem;
		background: rgb(239 68 68 / 0.05);
		padding: 0.7rem;
		font-size: 0.74rem;
		color: rgb(239 68 68);
	}

	.preview-card,
	.history-list {
		border: 1px solid var(--app-border);
		border-radius: 0.85rem;
		overflow: hidden;
	}

	.preview-stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
	}

	.preview-stats > div {
		display: flex;
		min-width: 0;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.7rem;
	}

	.preview-stats > div + div {
		border-left: 1px solid var(--app-border);
	}

	.preview-stats span,
	.history-heading p,
	.history-row p,
	.history-row small {
		font-size: 0.65rem;
		color: var(--app-muted-fg);
	}

	.preview-stats strong {
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 0.76rem;
		white-space: nowrap;
	}

	.preview-card details {
		border-top: 1px solid var(--app-border);
	}

	.preview-card summary {
		cursor: pointer;
		padding: 0.65rem 0.75rem;
		font-size: 0.72rem;
		font-weight: 600;
	}

	.preview-card pre {
		max-height: 18rem;
		overflow: auto;
		border-top: 1px solid var(--app-border);
		padding: 0.75rem;
		font-size: 0.69rem;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
	}

	.history-heading {
		margin-bottom: 0.5rem;
	}

	.history-heading h4 {
		font-size: 0.8rem;
		font-weight: 650;
	}

	.history-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.8rem;
		padding: 0.75rem;
	}

	.history-row + .history-row {
		border-top: 1px solid var(--app-border);
	}

	.history-row.current {
		background: color-mix(in srgb, var(--app-hover) 70%, transparent);
	}

	.history-row strong {
		font-size: 0.74rem;
	}

	.history-row p,
	.history-row small {
		display: block;
		margin-top: 0.2rem;
	}

	.history-empty {
		border: 1px dashed var(--app-border);
		border-radius: 0.8rem;
		padding: 1rem;
		font-size: 0.74rem;
		color: var(--app-muted-fg);
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	}

	.digest {
		font-size: 0.62rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 720px) {
		.preview-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.preview-stats > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.preview-stats > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}

		.history-row {
			align-items: flex-start;
			flex-direction: column;
		}
	}
</style>
