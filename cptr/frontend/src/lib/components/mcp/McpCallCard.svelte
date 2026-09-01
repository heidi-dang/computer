<script lang="ts">
	import { quintOut } from 'svelte/easing';
	import { slide } from 'svelte/transition';
	import type { McpContentItem } from '$lib/apis/mcp';
	import type { McpActivityRow } from '$lib/stores/mcp-activity';

	interface Props {
		record: McpActivityRow;
	}

	let { record }: Props = $props();
	let expanded = $state(true);

	$effect(() => {
		if (record.phase === 'started' || record.phase === 'failed') expanded = true;
	});

	const elapsed = $derived.by(() => {
		const ms =
			record.durationMs ??
			(record.completedAt == null ? null : Math.max(0, record.completedAt - record.startedAt));
		if (ms == null) return null;
		return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
	});

	const resultItems = $derived.by((): McpContentItem[] => {
		if (record.contentItems?.length) return record.contentItems;
		if (!record.resultJson) return [];
		try {
			const parsed = JSON.parse(record.resultJson);
			if (!Array.isArray(parsed)) return [];
			return parsed.filter(
				(item): item is McpContentItem =>
					Boolean(item) && typeof item === 'object' && typeof item.type === 'string'
			);
		} catch {
			return [];
		}
	});

	function toggleExpanded() {
		expanded = !expanded;
	}
</script>

<article class="app-surface w-full min-w-0 rounded-xl border px-3 py-2 shadow-sm">
	<div
		role="button"
		tabindex="0"
		aria-expanded={expanded}
		class="app-interactive flex w-full cursor-pointer select-none items-center gap-2 rounded-lg text-left"
		onclick={toggleExpanded}
		onkeydown={(event) => {
			if (event.key === 'Enter' || event.key === ' ') {
				event.preventDefault();
				toggleExpanded();
			}
		}}
	>
		{#if record.phase === 'started'}
			<svg class="size-4 shrink-0 animate-spin app-accent" viewBox="0 0 24 24" fill="none">
				<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" opacity=".25" />
				<path
					d="M12 3a9 9 0 0 1 9 9"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
				/>
			</svg>
		{:else if record.phase === 'complete'}
			<svg
				class="size-4 shrink-0 text-emerald-500"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
			>
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
				/>
			</svg>
		{:else}
			<svg
				class="size-4 shrink-0 text-red-400"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.5"
			>
				<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />
			</svg>
		{/if}

		<div class="min-w-0 flex-1">
			<div class="flex min-w-0 items-baseline gap-1.5">
				<span class="truncate font-mono text-xs font-semibold sm:text-sm">{record.toolName}</span>
				<span class="shrink-0 text-[0.62rem] app-muted">
					{record.clientLabel}{record.clientVersion ? ` · v${record.clientVersion}` : ''}
				</span>
			</div>
			<div class="mt-0.5 flex items-center gap-1.5 text-[0.62rem] app-muted">
				<span class="app-subtle-surface rounded-md border px-1.5 py-0.5">
					{record.source === 'plugin' ? 'MCP client' : 'Console invocation'}
				</span>
				<span class="truncate">{record.summary}</span>
			</div>
		</div>

		{#if elapsed}
			<span class="shrink-0 text-[0.62rem] tabular-nums app-muted">{elapsed}</span>
		{/if}
		<svg
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="3"
			class="size-2.5 shrink-0 transition-transform duration-150 app-muted {expanded
				? 'rotate-180'
				: ''}"
		>
			<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
		</svg>
	</div>

	{#if expanded}
		<div transition:slide={{ duration: 180, easing: quintOut, axis: 'y' }}>
			<div class="mt-2 space-y-3 border-t pt-3">
				{#if record.argumentsJson}
					<div>
						<div
							class="mb-1.5 px-0.5 text-[0.6rem] font-semibold uppercase tracking-wider app-muted"
						>
							Input
						</div>
						<pre
							class="app-subtle-surface max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border px-3 py-2 font-mono text-[0.7rem] leading-relaxed">{record.argumentsJson}</pre>
					</div>
				{/if}

				{#if record.phase === 'started'}
					<div class="flex items-center gap-2 text-xs app-muted">
						<span class="flex gap-1" aria-hidden="true">
							<span
								class="size-1 animate-bounce rounded-full bg-current"
								style="animation-delay:0ms"
							></span>
							<span
								class="size-1 animate-bounce rounded-full bg-current"
								style="animation-delay:150ms"
							></span>
							<span
								class="size-1 animate-bounce rounded-full bg-current"
								style="animation-delay:300ms"
							></span>
						</span>
						<span>Calling tool…</span>
					</div>
				{/if}

				{#if record.resultJson}
					<div>
						<div
							class="mb-1.5 px-0.5 text-[0.6rem] font-semibold uppercase tracking-wider app-muted"
						>
							Output
						</div>
						{#if resultItems.length > 0}
							<div class="space-y-2">
								{#each resultItems as item, index (index)}
									{#if item.type === 'text'}
										<pre
											class="app-subtle-surface max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border px-3 py-2 font-mono text-[0.7rem] leading-relaxed">{item.text}</pre>
									{:else if item.type === 'image' && item.data}
										<img
											src="data:{item.mimeType ?? 'image/png'};base64,{item.data}"
											alt="MCP tool result"
											class="max-w-full rounded-lg border"
										/>
									{:else if item.type === 'resource'}
										<div class="app-accent break-all px-1 font-mono text-[0.7rem]">{item.uri}</div>
									{:else}
										<pre
											class="whitespace-pre-wrap break-words px-1 font-mono text-[0.7rem] app-muted">{JSON.stringify(
												item,
												null,
												2
											)}</pre>
									{/if}
								{/each}
							</div>
						{:else}
							<pre
								class="app-subtle-surface max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border px-3 py-2 font-mono text-[0.7rem] leading-relaxed">{record.resultJson}</pre>
						{/if}
					</div>
				{/if}

				{#if record.errorJson}
					<div>
						<div
							class="mb-1.5 px-0.5 text-[0.6rem] font-semibold uppercase tracking-wider text-red-400"
						>
							Error
						</div>
						<pre
							class="app-subtle-surface max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border px-3 py-2 font-mono text-[0.7rem] leading-relaxed text-red-400">{record.errorJson}</pre>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</article>
