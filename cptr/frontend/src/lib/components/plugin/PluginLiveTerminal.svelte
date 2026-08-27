<script lang="ts">
	import type { PluginTerminalSnapshot, WorkbenchTargetType } from '$lib/apis/plugin';

	export interface PluginTerminalLine {
	sequence: number;
	text: string;
	stream: 'stdout' | 'stderr' | 'system';
	at?: string | number;
	key: string;
	}

	let {
		snapshot = null,
		lines = [],
		connectionState = 'idle'
	}: {
		snapshot?: PluginTerminalSnapshot | null;
		lines?: PluginTerminalLine[];
		connectionState?: 'idle' | 'connecting' | 'live' | 'reconnecting' | 'unavailable' | 'error';
	} = $props();

	let outputElement = $state<HTMLDivElement | undefined>();
	let shouldStickToBottom = true;
	let previousLineCount = 0;

	const targetLabel = $derived.by(() => {
		const value = snapshot?.snapshot;
		if (!snapshot || !value) return 'Waiting for a running CPTR target';
		const id = value.command_id ?? value.id ?? value.monitor_id;
		return `${snapshot.target} ${id ? `· ${id.slice(0, 16)}` : ''}`;
	});
	const status = $derived(snapshot?.snapshot.status ?? 'IDLE');
	const hasLiveTarget = $derived(Boolean(snapshot?.target));

	$effect(() => {
		if (lines.length <= previousLineCount) {
			previousLineCount = lines.length;
			return;
		}
		previousLineCount = lines.length;
		if (shouldStickToBottom && outputElement) {
			outputElement.scrollTop = outputElement.scrollHeight;
		}
	});

	function handleScroll() {
		if (!outputElement) return;
		shouldStickToBottom =
			outputElement.scrollHeight - outputElement.scrollTop - outputElement.clientHeight < 32;
	}

	function streamLabel(stream: PluginTerminalLine['stream']) {
		return stream === 'stderr' ? 'stderr' : stream === 'stdout' ? 'stdout' : 'system';
	}
</script>

<section class="plugin-terminal" aria-label="Live CPTR terminal">
	<header>
		<div class="terminal-title">
			<span class:live={connectionState === 'live'} class="terminal-indicator" aria-hidden="true"></span>
			<div>
				<strong>Live terminal</strong>
				<span>{targetLabel}</span>
			</div>
		</div>
		<div class="terminal-status" data-state={connectionState}>
			<span>{status}</span>
			<span class="connection-label">{connectionState}</span>
		</div>
	</header>
	<div class="terminal-output" bind:this={outputElement} onscroll={handleScroll} role="log" aria-live="polite">
		{#if !hasLiveTarget}
			<div class="terminal-empty">Select a Workbench Session with an active task, command, or monitor.</div>
		{:else if lines.length === 0}
			<div class="terminal-empty">No process output has been emitted for this target yet.</div>
		{:else}
			{#each lines as line (line.key)}
				<div class:stderr={line.stream === 'stderr'} class:system={line.stream === 'system'} class="terminal-line">
					<span class="stream-label">{streamLabel(line.stream)}</span>
					<pre>{line.text}</pre>
				</div>
			{/each}
		{/if}
	</div>
</section>

<style>
	.plugin-terminal {
		position: sticky;
		top: 0;
		z-index: 4;
		overflow: hidden;
		border: 1px solid color-mix(in srgb, var(--border-color, #64748b) 75%, transparent);
		border-radius: 0.8rem;
		background: #111827;
		box-shadow: 0 10px 25px rgb(15 23 42 / 0.14);
	}

	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		min-height: 3.15rem;
		padding: 0.6rem 0.75rem;
		border-bottom: 1px solid rgb(148 163 184 / 0.25);
		background: #172033;
		color: #e2e8f0;
	}

	.terminal-title {
		display: flex;
		align-items: center;
		min-width: 0;
		gap: 0.55rem;
	}

	.terminal-title div {
		display: grid;
		min-width: 0;
		gap: 0.1rem;
	}

	.terminal-title strong {
		font-size: 0.78rem;
		letter-spacing: 0.01em;
	}

	.terminal-title span:not(.terminal-indicator) {
		overflow: hidden;
		max-width: 13rem;
		font: 0.66rem/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: #94a3b8;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.terminal-indicator {
		width: 0.52rem;
		height: 0.52rem;
		border-radius: 999px;
		background: #64748b;
	}

	.terminal-indicator.live {
		background: #34d399;
		box-shadow: 0 0 0 3px rgb(52 211 153 / 0.16);
	}

	.terminal-status {
		display: flex;
		align-items: center;
		gap: 0.38rem;
		font: 0.65rem/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: #cbd5e1;
		text-transform: uppercase;
	}

	.terminal-status .connection-label {
		padding: 0.16rem 0.28rem;
		border-radius: 0.25rem;
		color: #bbf7d0;
		background: rgb(22 163 74 / 0.15);
	}

	.terminal-status[data-state='connecting'] .connection-label,
	.terminal-status[data-state='reconnecting'] .connection-label {
		color: #fde68a;
		background: rgb(202 138 4 / 0.16);
	}

	.terminal-status[data-state='unavailable'] .connection-label,
	.terminal-status[data-state='error'] .connection-label {
		color: #fecaca;
		background: rgb(220 38 38 / 0.16);
	}

	.terminal-output {
		max-height: min(34svh, 19rem);
		overflow: auto;
		padding: 0.55rem 0.65rem 0.75rem;
		font: 0.72rem/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		background: #0b1020;
	}

	.terminal-line {
		display: grid;
		grid-template-columns: 3.5rem minmax(0, 1fr);
		gap: 0.5rem;
		padding: 0.1rem 0;
		color: #d1fae5;
	}

	.stream-label {
		padding-top: 0.05rem;
		font-size: 0.57rem;
		color: #64748b;
		text-transform: uppercase;
	}

	.terminal-line.stderr pre {
		color: #fecaca;
	}

	.terminal-line.system pre {
		color: #bfdbfe;
	}

	pre {
		min-width: 0;
		margin: 0;
		white-space: pre-wrap;
		word-break: break-word;
	}

	.terminal-empty {
		padding: 1.1rem 0.35rem;
		color: #94a3b8;
	}

	@media (max-width: 390px) {
		header {
			align-items: flex-start;
			min-height: 3.6rem;
			padding: 0.55rem 0.6rem;
		}
		.terminal-status {
			align-items: flex-end;
			flex-direction: column;
			gap: 0.2rem;
			font-size: 0.59rem;
		}
		.terminal-title span:not(.terminal-indicator) {
			max-width: 9rem;
		}
		.terminal-output {
			max-height: min(36svh, 16rem);
			padding: 0.45rem 0.5rem 0.6rem;
			font-size: 0.67rem;
		}
		.terminal-line {
			grid-template-columns: 2.8rem minmax(0, 1fr);
			gap: 0.35rem;
		}
	}
</style>
