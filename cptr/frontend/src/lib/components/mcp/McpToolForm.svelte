<script lang="ts">
	import type { McpToolSpec } from '$lib/apis/mcp';
	import { coerceMcpToolArguments, formatMcpToolArguments } from '$lib/utils/mcp-console';

	interface Props {
		tool: McpToolSpec | null;
		serverId: string | null;
		onInvoke: (serverId: string, toolName: string, args: Record<string, unknown>) => void;
		disabled?: boolean;
	}

	let { tool, serverId, onInvoke, disabled = false }: Props = $props();

	let formValues = $state<Record<string, string>>({});
	let rawJsonMode = $state(false);
	let rawJson = $state('{}');
	let jsonError = $state('');

	$effect(() => {
		if (tool) {
			formValues = {};
			rawJson = '{}';
			jsonError = '';
		}
	});

	const schema = $derived(tool?.parameters ?? { type: 'object', properties: {} });
	const properties = $derived((schema as any).properties ?? {});
	const required = $derived((schema as any).required ?? []);
	const propEntries = $derived(Object.entries(properties) as [string, any][]);

	function buildArgs(): Record<string, unknown> | null {
		const result = coerceMcpToolArguments({
			rawJsonMode,
			rawJson,
			formValues,
			properties,
			required
		});
		jsonError = result.error ?? '';
		return result.args;
	}

	function submitCurrent() {
		if (!tool || !serverId) return;
		const args = buildArgs();
		if (args === null) return;
		onInvoke(serverId, tool.name, args);
		formValues = {};
		rawJson = '{}';
		jsonError = '';
	}

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		submitCurrent();
	}

	function handleKeydown(e: KeyboardEvent) {
		if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
			e.preventDefault();
			submitCurrent();
		}
	}

	const inputClass =
		'mcp-tool-input w-full rounded-lg border px-2.5 py-1.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-50';
</script>

{#if !tool || !serverId}
	<div class="app-theme flex flex-1 items-center justify-center px-4 text-center text-xs app-muted">
		<div>
			<div
				class="app-subtle-surface mx-auto mb-2 flex size-10 items-center justify-center rounded-xl border"
			>
				<svg
					class="size-7 app-muted"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.25"
				>
					<path d="M12 22V18" /><path d="M9 3V7" /><path d="M15 3V7" />
					<path
						d="M18 7H6C5.44772 7 5 7.44772 5 8V13C5 15.7614 7.23858 18 10 18H14C16.7614 18 19 15.7614 19 13V8C19 7.44772 18.5523 7 18 7Z"
					/>
				</svg>
			</div>
			Select a tool from the server list to invoke it
		</div>
	</div>
{:else}
	<form onsubmit={handleSubmit} class="app-theme flex h-full flex-col">
		<div class="app-surface border-b px-4 pb-2 pt-3">
			<div class="flex items-center justify-between gap-2">
				<div class="min-w-0">
					<div class="truncate font-mono text-sm font-semibold">{tool.name}</div>
					{#if tool.description}
						<div class="mt-0.5 line-clamp-2 text-xs app-muted">{tool.description}</div>
					{/if}
				</div>
				<button
					type="button"
					class="app-interactive shrink-0 rounded-lg border px-2 py-1 text-[0.65rem] {rawJsonMode
						? 'app-accent-surface app-accent'
						: 'app-subtle-surface app-muted'}"
					onclick={() => {
						if (!rawJsonMode) {
							const args = buildArgs();
							if (!args) return;
							rawJson = formatMcpToolArguments(args);
						}
						rawJsonMode = !rawJsonMode;
						jsonError = '';
					}}
				>
					{rawJsonMode ? '← Form' : 'JSON →'}
				</button>
			</div>
		</div>

		<div class="flex-1 space-y-3 overflow-y-auto px-4 py-3">
			{#if rawJsonMode}
				<div>
					<label
						for="mcp-arguments-json"
						class="mb-1.5 block text-[0.65rem] uppercase tracking-wider app-muted"
						>Arguments (JSON)</label
					>
					<textarea
						id="mcp-arguments-json"
						class="{inputClass} min-h-32 resize-y font-mono"
						bind:value={rawJson}
						onkeydown={handleKeydown}
						spellcheck={false}
						{disabled}
					></textarea>
					{#if jsonError}
						<p class="mt-1 text-[0.65rem] text-red-400">{jsonError}</p>
					{/if}
				</div>
			{:else if propEntries.length === 0}
				<p class="py-2 text-xs app-muted">This tool takes no parameters.</p>
			{:else}
				{#each propEntries as [key, propSchema] (key)}
					{@const isRequired = required.includes(key)}
					{@const ptype = propSchema.type ?? 'string'}
					{@const desc = propSchema.description ?? ''}
					{@const enumVals = propSchema.enum ?? null}
					<div>
						<label for={`mcp-argument-${key}`} class="mb-1 block text-xs font-medium">
							<span class="font-mono">{key}</span>
							{#if isRequired}<span class="ml-0.5 text-red-400">*</span>{/if}
							<span class="ml-1 text-[0.6rem] font-normal app-muted">({ptype})</span>
						</label>
						{#if desc}
							<p class="mb-1 text-[0.65rem] leading-relaxed app-muted">{desc}</p>
						{/if}
						{#if enumVals}
							<select
								id={`mcp-argument-${key}`}
								class={inputClass}
								bind:value={formValues[key]}
								{disabled}
							>
								<option value="">— choose —</option>
								{#each enumVals as opt}<option value={opt}>{opt}</option>{/each}
							</select>
						{:else if ptype === 'boolean'}
							<select
								id={`mcp-argument-${key}`}
								class={inputClass}
								bind:value={formValues[key]}
								{disabled}
							>
								<option value="">— choose —</option>
								<option value="true">true</option>
								<option value="false">false</option>
							</select>
						{:else if ptype === 'object' || ptype === 'array'}
							<textarea
								id={`mcp-argument-${key}`}
								class="{inputClass} min-h-20 resize-y font-mono"
								bind:value={formValues[key]}
								placeholder={ptype === 'array' ? '[]' : '{}'}
								spellcheck={false}
								{disabled}
							></textarea>
						{:else if ptype === 'number' || ptype === 'integer'}
							<input
								id={`mcp-argument-${key}`}
								type="number"
								class={inputClass}
								bind:value={formValues[key]}
								{disabled}
							/>
						{:else if propSchema.maxLength > 100 || !propSchema.maxLength}
							<textarea
								id={`mcp-argument-${key}`}
								class="{inputClass} min-h-16 resize-y"
								bind:value={formValues[key]}
								placeholder={propSchema.examples?.[0] ?? ''}
								onkeydown={handleKeydown}
								{disabled}
							></textarea>
						{:else}
							<input
								id={`mcp-argument-${key}`}
								type="text"
								class={inputClass}
								bind:value={formValues[key]}
								placeholder={propSchema.examples?.[0] ?? ''}
								{disabled}
							/>
						{/if}
					</div>
				{/each}
			{/if}
			{#if !rawJsonMode && jsonError}
				<p class="text-[0.65rem] text-red-400">{jsonError}</p>
			{/if}
		</div>

		<div class="app-surface shrink-0 border-t px-4 pb-4 pt-3">
			<button
				type="submit"
				class="app-interactive app-accent-surface app-accent flex min-h-11 w-full items-center justify-center gap-2 rounded-xl border px-4 py-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40"
				{disabled}
			>
				<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z"
					/>
				</svg>
				Invoke
				<span class="text-[0.6rem] opacity-70">⌘↵</span>
			</button>
			<p class="mt-1.5 text-center text-[0.6rem] app-muted">Ctrl+Enter also submits</p>
		</div>
	</form>
{/if}

<style>
	.mcp-tool-input {
		background: var(--app-surface-subtle);
		color: var(--app-fg);
		border-color: var(--app-border);
	}

	.mcp-tool-input::placeholder {
		color: var(--app-fg-subtle);
	}

	.mcp-tool-input:focus {
		outline: none;
		box-shadow: 0 0 0 1px var(--app-focus-ring);
	}
</style>
