<script lang="ts">
	interface Props {
		status?: string;
		runId?: string;
		sending?: boolean;
	}

	let { status = '', runId = '', sending = false }: Props = $props();

	const terminalStatuses = new Set([
		'cancelled',
		'succeeded',
		'completed',
		'failed',
		'unknown',
		'manual_review',
		'manual_review_required',
		'orphaned'
	]);

	const isTerminal = $derived(terminalStatuses.has(status.toLowerCase()));
	const isActive = $derived(!isTerminal && ['preparing', 'active', 'planning', 'verifying'].includes(status.toLowerCase()));

	const label = $derived.by(() => {
		switch (status.toLowerCase()) {
			case 'preparing':
				return 'Preparing FlowDeck…';
			case 'active':
				return 'FlowDeck activity';
			case 'planning':
				return 'Planning';
			case 'verifying':
				return 'Verifying';
			case 'succeeded':
			case 'completed':
				return 'Completed';
			case 'cancelled':
				return 'Cancelled';
			case 'manual_review':
			case 'manual_review_required':
				return 'Manual review required';
			case 'unknown':
				return 'Unknown outcome';
			case 'orphaned':
				return 'Orphaned';
			case 'failed':
				return 'Failed';
			default:
				return sending ? 'Starting orchestration…' : 'FlowDeck';
		}
	});
</script>

{#if status || sending}
	<div
		class="flowdeck-status-strip"
		class:is-terminal={isTerminal}
		class:is-active={isActive}
		role="status"
		aria-live="polite"
	>
		<span class="flowdeck-status-mark" aria-hidden="true">{isTerminal ? '·' : '›'}</span>
		<span class="flowdeck-status-label">{label}</span>
		{#if runId}
			<span class="flowdeck-status-run" title={runId}>run {runId.slice(0, 8)}</span>
		{/if}
	</div>
{/if}

<style>
	.flowdeck-status-strip {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		min-width: 0;
		min-height: 1.75rem;
		padding: 0.15rem 0.2rem;
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
		font-size: 0.6875rem;
		line-height: 1.2;
	}

	.flowdeck-status-mark {
		flex: 0 0 auto;
		color: color-mix(in oklab, var(--app-fg) 45%, transparent);
		font-size: 1rem;
		line-height: 1;
	}

	.flowdeck-status-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.flowdeck-status-run {
		flex: 0 0 auto;
		margin-left: auto;
		color: color-mix(in oklab, var(--app-fg) 38%, transparent);
		font: 0.6rem ui-monospace, SFMono-Regular, monospace;
	}

	.flowdeck-status-strip.is-active .flowdeck-status-mark {
		color: color-mix(in oklab, var(--app-fg) 62%, transparent);
	}

	.flowdeck-status-strip.is-terminal {
		color: color-mix(in oklab, var(--app-fg) 52%, transparent);
	}

	@media (max-width: 430px) {
		.flowdeck-status-strip {
			padding-inline: 0.1rem;
		}

		.flowdeck-status-run {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.flowdeck-status-strip,
		.flowdeck-status-mark {
			animation: none;
			transition: none;
		}
	}
</style>