<script lang="ts">
	import type { McpToolSpec } from '$lib/apis/mcp';

	interface Props {
		tool: McpToolSpec | null;
		serverId: string | null;
		onInvoke: (serverId: string, toolName: string, args: Record<string, unknown>) => void;
		disabled?: boolean;
	}

	let { tool, serverId, onInvoke, disabled = false }: Props = $props();

	// Form values keyed by parameter name
	let formValues = $state<Record<string, string>>({});
	let rawJsonMode = $state(false);
	let rawJson = $state('{}');
	let jsonError = $state('');

	// Reset form when tool changes
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
		if (rawJsonMode) {
			try {
				const parsed = JSON.parse(rawJson);
				jsonError = '';
				return parsed;
			} catch (e: any) {
				jsonError = e.message;
				return null;
			}
		}
		// Build from form fields
		const args: Record<string, unknown> = {};
		for (const [key, propSchema] of propEntries) {
			const raw = formValues[key] ?? '';
			if (raw === '' && !required.includes(key)) continue;
			const type = propSchema.type;
			if (type === 'number' || type === 'integer') {
				args[key] = Number(raw);
			} else if (type === 'boolean') {
				args[key] = raw === 'true';
			} else if (type === 'object' || type === 'array') {
				try { args[key] = JSON.parse(raw); } catch { args[key] = raw; }
			} else {
				args[key] = raw;
			}
		}
		return args;
	}

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		if (!tool || !serverId) return;
		const args = buildArgs();
		if (args === null) return;
		onInvoke(serverId, tool.name, args);
		// Reset form after invocation
		formValues = {};
		rawJson = '{}';
	}

	function handle_submit_keydown() {
		if (!tool || !serverId) return;
		const args = buildArgs();
		if (args === null) return;
		onInvoke(serverId, tool.name, args);
		formValues = {};
		rawJson = '{}';
	}

	function handleKeydown(e: KeyboardEvent) {
		if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
			e.preventDefault();
			handle_submit_keydown();
		}
	}

	const inputClass = "w-full text-xs bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-1.5 text-gray-800 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-400 dark:focus:ring-blue-600 transition";
</script>

{#if !tool || !serverId}
	<div class="flex-1 flex items-center justify-center text-xs text-gray-400 dark:text-gray-500 px-4 text-center">
		<div>
			<div class="text-gray-300 dark:text-gray-600 mb-2">
				<svg class="size-8 mx-auto" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25">
					<path d="M12 22V18" /><path d="M9 3V7" /><path d="M15 3V7" />
					<path d="M18 7H6C5.44772 7 5 7.44772 5 8V13C5 15.7614 7.23858 18 10 18H14C16.7614 18 19 15.7614 19 13V8C19 7.44772 18.5523 7 18 7Z"/>
				</svg>
			</div>
			Select a tool from the server list to invoke it
		</div>
	</div>
{:else}
	<form onsubmit={handleSubmit} class="flex flex-col h-full">
		<!-- Tool header -->
		<div class="px-4 pt-3 pb-2 border-b border-gray-100 dark:border-gray-800">
			<div class="flex items-center justify-between gap-2">
				<div class="min-w-0">
					<div class="font-mono text-sm font-semibold text-gray-800 dark:text-gray-100 truncate">{tool.name}</div>
					{#if tool.description}
						<div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">{tool.description}</div>
					{/if}
				</div>
				<!-- Raw JSON toggle -->
				<button
					type="button"
					class="shrink-0 text-[0.65rem] px-2 py-1 rounded-lg {rawJsonMode ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'} hover:opacity-80 transition-all"
					onclick={() => {
						if (!rawJsonMode) {
							// Populate raw JSON from form values
							const args = buildArgs();
							if (args) rawJson = JSON.stringify(args, null, 2);
						}
						rawJsonMode = !rawJsonMode;
					}}
				>
					{rawJsonMode ? '← Form' : 'JSON →'}
				</button>
			</div>
		</div>

		<!-- Parameters -->
		<div class="flex-1 overflow-y-auto px-4 py-3 space-y-3">
			{#if rawJsonMode}
				<!-- Raw JSON editor -->
				<div>
					<label class="block text-[0.65rem] uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1.5">Arguments (JSON)</label>
					<textarea
						class="{inputClass} font-mono min-h-32 resize-y"
						bind:value={rawJson}
						onkeydown={handleKeydown}
						spellcheck={false}
						{disabled}
					></textarea>
					{#if jsonError}
						<p class="text-[0.65rem] text-red-500 mt-1">{jsonError}</p>
					{/if}
				</div>
			{:else if propEntries.length === 0}
				<p class="text-xs text-gray-400 dark:text-gray-500 py-2">This tool takes no parameters.</p>
			{:else}
				{#each propEntries as [key, propSchema] (key)}
					{@const isRequired = required.includes(key)}
					{@const ptype = propSchema.type ?? 'string'}
					{@const desc = propSchema.description ?? ''}
					{@const enumVals = propSchema.enum ?? null}
					<div>
						<label class="block text-xs text-gray-600 dark:text-gray-300 mb-1 font-medium">
							<span class="font-mono">{key}</span>
							{#if isRequired}<span class="text-red-400 ml-0.5">*</span>{/if}
							<span class="text-[0.6rem] text-gray-400 dark:text-gray-500 font-normal ml-1">({ptype})</span>
						</label>
						{#if desc}
							<p class="text-[0.65rem] text-gray-400 dark:text-gray-500 mb-1 leading-relaxed">{desc}</p>
						{/if}
						{#if enumVals}
							<select
								class="{inputClass}"
								bind:value={formValues[key]}
								{disabled}
							>
								<option value="">— choose —</option>
								{#each enumVals as opt}<option value={opt}>{opt}</option>{/each}
							</select>
						{:else if ptype === 'boolean'}
							<select class="{inputClass}" bind:value={formValues[key]} {disabled}>
								<option value="">— choose —</option>
								<option value="true">true</option>
								<option value="false">false</option>
							</select>
						{:else if ptype === 'object' || ptype === 'array'}
							<textarea
								class="{inputClass} font-mono min-h-20 resize-y"
								bind:value={formValues[key]}
								placeholder={ptype === 'array' ? '[]' : '{}'}
								spellcheck={false}
								{disabled}
							></textarea>
						{:else if ptype === 'number' || ptype === 'integer'}
							<input
								type="number"
								class="{inputClass}"
								bind:value={formValues[key]}
								{disabled}
							/>
						{:else}
							<!-- string / default -->
							{#if propSchema.maxLength > 100 || !propSchema.maxLength}
								<textarea
									class="{inputClass} min-h-16 resize-y"
									bind:value={formValues[key]}
									placeholder={propSchema.examples?.[0] ?? ''}
									onkeydown={handleKeydown}
									{disabled}
								></textarea>
							{:else}
								<input
									type="text"
									class="{inputClass}"
									bind:value={formValues[key]}
									placeholder={propSchema.examples?.[0] ?? ''}
									{disabled}
								/>
							{/if}
						{/if}
					</div>
				{/each}
			{/if}
		</div>

		<!-- Invoke button -->
		<div class="px-4 pb-4 pt-2 shrink-0">
			<button
				type="submit"
				class="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-xl text-sm font-medium
					bg-blue-500 hover:bg-blue-600 active:bg-blue-700
					text-white transition-colors duration-100
					disabled:opacity-40 disabled:cursor-not-allowed"
				{disabled}
			>
				<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path stroke-linecap="round" stroke-linejoin="round" d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z"/>
				</svg>
				Invoke
				<span class="text-blue-200 text-[0.6rem]">⌘↵</span>
			</button>
			<p class="text-center text-[0.6rem] text-gray-400 dark:text-gray-500 mt-1.5">Ctrl+Enter also submits</p>
		</div>
	</form>
{/if}
