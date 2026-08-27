<script lang="ts">
	import type { WorkbenchSessionEvent } from '$lib/apis/plugin';

	let { event }: { event: WorkbenchSessionEvent } = $props();

	const sourceLabel = $derived(
		event.source === 'plugin' ? 'ChatGPT via CPTR' : event.source === 'workbench' ? 'Workbench' : 'CPTR'
	);
	const eventTime = $derived.by(() => {
		const value = new Date(event.created_at);
		return Number.isNaN(value.getTime())
			? ''
			: value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	});
	const targetLabel = $derived(
		event.target_type && event.target_id
			? `${event.target_type} · ${event.target_id.slice(0, 14)}`
			: null
	);
</script>

<article class="plugin-activity-message" data-source={event.source}>
	<div class="plugin-activity-avatar" aria-hidden="true">
		{event.source === 'plugin' ? 'C' : event.source === 'workbench' ? 'W' : 'P'}
	</div>
	<div class="plugin-activity-content">
		<div class="plugin-activity-meta">
			<strong>{sourceLabel}</strong>
			<span>{eventTime}</span>
		</div>
		<p>{event.summary}</p>
		<div class="plugin-activity-tags">
			<span>{event.event_type}</span>
			{#if event.state}<span>{event.state}</span>{/if}
			{#if targetLabel}<span>{targetLabel}</span>{/if}
		</div>
	</div>
</article>

<style>
	.plugin-activity-message {
		display: grid;
		grid-template-columns: 30px minmax(0, 1fr);
		gap: 0.65rem;
		padding: 0.65rem 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border-color, #d6d6d6) 62%, transparent);
	}

	.plugin-activity-avatar {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 9px;
		font-size: 0.7rem;
		font-weight: 700;
		color: white;
		background: #4f46e5;
	}

	.plugin-activity-message[data-source='workbench'] .plugin-activity-avatar {
		background: #0f766e;
	}

	.plugin-activity-message[data-source='system'] .plugin-activity-avatar {
		background: #64748b;
	}

	.plugin-activity-content {
		min-width: 0;
	}

	.plugin-activity-meta,
	.plugin-activity-tags {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		flex-wrap: wrap;
	}

	.plugin-activity-meta strong {
		font-size: 0.78rem;
		color: var(--text-primary, #111827);
	}

	.plugin-activity-meta span {
		font-size: 0.7rem;
		color: var(--text-secondary, #6b7280);
	}

	p {
		margin: 0.2rem 0 0.38rem;
		font-size: 0.88rem;
		line-height: 1.35;
		color: var(--text-primary, #111827);
		word-break: break-word;
	}

	.plugin-activity-tags span {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		padding: 0.12rem 0.35rem;
		border-radius: 0.3rem;
		font: 0.64rem/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		color: var(--text-secondary, #475569);
		background: color-mix(in srgb, var(--border-color, #e2e8f0) 60%, transparent);
	}

	:global(.dark) .plugin-activity-meta strong,
	:global(.dark) p {
		color: #e5e7eb;
	}

	@media (max-width: 390px) {
		.plugin-activity-message {
			grid-template-columns: 25px minmax(0, 1fr);
			gap: 0.5rem;
		}
		.plugin-activity-avatar {
			width: 25px;
			height: 25px;
			border-radius: 7px;
			font-size: 0.62rem;
		}
	}
</style>
