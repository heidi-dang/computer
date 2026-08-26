<script lang="ts">
	import { onMount } from 'svelte';
	import Icon from '$lib/components/Icon.svelte';
	import {
		captureCheckpoint,
		listCheckpoints,
		restoreCheckpoint,
		type Checkpoint
	} from '$lib/apis/checkpoints';

	let { workspace }: { workspace: string } = $props();
	let checkpoints = $state<Checkpoint[]>([]);
	let selected = $state('');
	let busy = $state(false);
	let message = $state('');
	let error = $state('');

	async function refresh() {
		if (!workspace) return;
		try {
			checkpoints = (await listCheckpoints(workspace)).checkpoints;
			if (!selected && checkpoints[0]) selected = checkpoints[0].checkpoint_id;
		} catch (cause) {
			error = 'Checkpoint state is unavailable. Try again shortly.';
		}
	}

	function key() {
		return `checkpoint-${crypto.randomUUID()}`;
	}

	async function capture() {
		busy = true; error = ''; message = '';
		try {
			const result = await captureCheckpoint(workspace, key());
			message = `Captured ${result.revision.slice(0, 10)}.`;
			await refresh();
			selected = result.checkpoint_id;
		} catch (cause) {
			error = 'Capture was not completed. Review the worktree and try again.';
		} finally { busy = false; }
	}

	async function restore() {
		if (!selected) return;
		busy = true; error = ''; message = '';
		try {
			const result = await restoreCheckpoint(workspace, selected, key());
			message = `Restored ${result.revision.slice(0, 10)}.`;
			await refresh();
		} catch (cause) {
			error = 'Restore requires review. No changes were applied.';
		} finally { busy = false; }
	}

	$effect(() => { if (workspace) void refresh(); });
</script>

<section class="checkpoint-panel" aria-labelledby="checkpoint-title">
	<div class="panel-heading">
		<div><span class="panel-kicker">Recovery surface</span><h2 id="checkpoint-title">Checkpoints</h2></div>
		<Icon name="refresh" size={15} />
	</div>
	<p class="panel-copy">Capture a verified Git revision or restore one safely. Dirty worktrees and uncertain outcomes are refused.</p>
	<div class="checkpoint-actions">
		<button type="button" onclick={() => void capture()} disabled={busy || !workspace}>Capture checkpoint</button>
		<select bind:value={selected} aria-label="Checkpoint to restore" disabled={busy || !checkpoints.length}>
			<option value="" disabled>Select a checkpoint</option>
			{#each checkpoints as checkpoint}
				<option value={checkpoint.checkpoint_id}>{checkpoint.revision.slice(0, 10)} · {checkpoint.status}</option>
			{/each}
		</select>
		<button type="button" class="restore" onclick={() => void restore()} disabled={busy || !selected}>Restore</button>
	</div>
	{#if message}<p class="checkpoint-message" role="status">{message}</p>{/if}
	{#if error}<p class="checkpoint-error" role="alert">{error}</p>{/if}
</section>

<style>
	.checkpoint-panel { margin: 1.25rem 0; padding: 1.1rem 1.2rem; border: 1px solid var(--border-color, #2d333b); border-radius: 12px; background: var(--surface-color, #161b22); }
	.panel-heading { display: flex; justify-content: space-between; align-items: start; }
	.panel-kicker { color: var(--text-muted, #8b949e); font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; }
	h2 { margin: .25rem 0 0; font-size: 1.05rem; }
	.panel-copy { color: var(--text-muted, #8b949e); font-size: .86rem; line-height: 1.45; max-width: 52rem; }
	.checkpoint-actions { display: flex; gap: .6rem; flex-wrap: wrap; }
	button, select { min-height: 2.25rem; border: 1px solid var(--border-color, #39414b); border-radius: 7px; padding: .4rem .7rem; background: var(--background-color, #0d1117); color: inherit; }
	button { cursor: pointer; } button:disabled { cursor: not-allowed; opacity: .55; } .restore { border-color: #a66a39; }
	.checkpoint-message { color: #7ee787; } .checkpoint-error { color: #ff7b72; }
</style>