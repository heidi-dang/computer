<script lang="ts">
	import type { McpFailureState } from '$lib/stores/mcp-diagnostics';

	type Props = {
		diagnostic: McpFailureState;
		onrevealactivity?: (requestId: string | null, correlationId: string | null) => void;
	};

	let { diagnostic, onrevealactivity }: Props = $props();

	function yesNo(value: boolean | null): string {
		return value == null ? '—' : value ? 'Yes' : 'No';
	}

	function shortId(value: string | null): string {
		if (!value) return '—';
		return value.length > 28 ? `${value.slice(0, 12)}…${value.slice(-10)}` : value;
	}
</script>

<section class="app-surface rounded-xl border p-3" aria-label="Request diagnostic detail">
	<div class="mb-3 flex items-center justify-between gap-3">
		<div>
			<p class="text-xs font-semibold">Failure diagnostic</p>
			<p class="mt-0.5 text-[0.65rem] app-muted">Safe correlated operational metadata</p>
		</div>
		{#if diagnostic.requestId || diagnostic.correlationId}
			<button
				type="button"
				class="app-interactive min-h-11 rounded-lg px-3 text-[0.68rem] font-medium app-accent sm:min-h-0 sm:py-2"
				onclick={() => onrevealactivity?.(diagnostic.requestId, diagnostic.correlationId)}
			>
				Show Activity
			</button>
		{/if}
	</div>

	<dl class="grid grid-cols-2 gap-x-4 gap-y-3 text-[0.7rem] sm:grid-cols-4">
		<div>
			<dt class="app-muted">Stage</dt>
			<dd class="mt-0.5 break-all font-mono">{diagnostic.stage}</dd>
		</div>
		<div>
			<dt class="app-muted">Error code</dt>
			<dd class="mt-0.5 break-all font-mono">{diagnostic.errorCode}</dd>
		</div>
		<div>
			<dt class="app-muted">HTTP status</dt>
			<dd class="mt-0.5">{diagnostic.httpStatus ?? '—'}</dd>
		</div>
		<div>
			<dt class="app-muted">Retryable</dt>
			<dd class="mt-0.5">{yesNo(diagnostic.retryable)}</dd>
		</div>
		<div>
			<dt class="app-muted">Duration</dt>
			<dd class="mt-0.5">{diagnostic.durationMs == null ? '—' : `${diagnostic.durationMs} ms`}</dd>
		</div>
		<div class="col-span-2 sm:col-span-1">
			<dt class="app-muted">Request ID</dt>
			<dd class="mt-0.5 break-all font-mono" title={diagnostic.requestId ?? undefined}>
				{shortId(diagnostic.requestId)}
			</dd>
		</div>
		<div class="col-span-2 sm:col-span-1">
			<dt class="app-muted">Correlation ID</dt>
			<dd class="mt-0.5 break-all font-mono" title={diagnostic.correlationId ?? undefined}>
				{shortId(diagnostic.correlationId)}
			</dd>
		</div>
		<div class="col-span-2 sm:col-span-4">
			<dt class="app-muted">Summary</dt>
			<dd class="mt-1 leading-5">{diagnostic.summary}</dd>
		</div>
	</dl>
</section>
