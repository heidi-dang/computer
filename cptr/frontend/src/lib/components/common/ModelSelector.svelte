<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { chatModels, type ChatModel } from '$lib/stores/chat';
	import DropdownMenu from '../DropdownMenu.svelte';
	import { t } from '$lib/i18n';

	interface Props {
		selectedModel: string | null;
		preferAbove?: boolean;
		align?: 'start' | 'end';
		nullable?: boolean;
		nullLabel?: string;
		onchange?: (model: string | null) => void;
		onclose?: () => void;
	}
	let {
		selectedModel = $bindable(),
		preferAbove = true,
		align = 'end',
		nullable = false,
		nullLabel = 'Current model',
		onchange,
		onclose
	}: Props = $props();

	let btnEl: HTMLButtonElement | undefined = $state();
	let searchInputEl: HTMLInputElement | undefined = $state();
	let open = $state(false);
	let search = $state('');
	let highlightedIndex = $state(0);
	let isSmallViewport = $state(false);

	const SOURCE_LABELS: Record<string, string> = {
		openai: 'OpenAI',
		anthropic: 'Anthropic',
		deepseek: 'DeepSeek',
		google: 'Google',
		gemini: 'Gemini',
		openrouter: 'OpenRouter',
		ollama: 'Ollama',
		'openai-compatible': 'OpenAI Compatible',
		codex: 'Codex',
		claude: 'Claude',
		hermes: 'Hermes',
		opencode: 'OpenCode'
	};

	function formatSourceName(value: string): string {
		const trimmed = value.trim();
		if (!trimmed) return 'Other';
		const alias = SOURCE_LABELS[trimmed.toLowerCase()];
		if (alias) return alias;
		return trimmed.replace(/[_-]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
	}

	function sourceKey(model: ChatModel): string {
		if (model.provider === 'agent') {
			return `agent:${(model.agent_id || model.profile_id || 'agent').toLowerCase()}`;
		}
		return `connection:${(model.connection_id || model.source_name || model.provider || 'other').toLowerCase()}`;
	}

	function sourceLabel(model: ChatModel): string {
		if (model.provider === 'agent') {
			return formatSourceName(model.agent_id || model.profile_id || 'Agent');
		}
		const configuredName = model.source_name?.trim();
		return configuredName || formatSourceName(model.provider || 'Other');
	}

	function displayModelName(model: ChatModel): string {
		if (model.provider !== 'agent') return model.name || model.id;
		for (const candidate of [model.name, model.id]) {
			if (!candidate.startsWith('agent:')) continue;
			const slash = candidate.indexOf('/');
			if (slash >= 0 && slash < candidate.length - 1) return candidate.slice(slash + 1);
		}
		return model.name || model.id;
	}

	function modelSearchText(model: ChatModel): string {
		return [
			displayModelName(model),
			model.name,
			model.id,
			model.provider,
			model.source_name,
			model.agent_id,
			model.profile_id,
			sourceLabel(model)
		]
			.filter(Boolean)
			.join(' ')
			.toLowerCase();
	}

	const selectorMaxHeight = $derived(isSmallViewport ? 'min(58dvh,30rem)' : 'min(55vh,28rem)');

	const matchingModels = $derived(
		search.trim()
			? $chatModels.filter((model) => modelSearchText(model).includes(search.trim().toLowerCase()))
			: $chatModels
	);

	const modelGroups = $derived.by(() => {
		const groups = new Map<string, { label: string; models: ChatModel[] }>();
		for (const model of matchingModels) {
			const key = sourceKey(model);
			const existing = groups.get(key);
			if (existing) existing.models.push(model);
			else groups.set(key, { label: sourceLabel(model), models: [model] });
		}
		return Array.from(groups.values());
	});

	const filtered = $derived(modelGroups.flatMap((group) => group.models));

	const selectedEntry = $derived($chatModels.find((model) => model.id === selectedModel));
	const selectedLabel = $derived(
		selectedEntry
			? selectedEntry.provider === 'agent'
				? `${sourceLabel(selectedEntry)} · ${displayModelName(selectedEntry)}`
				: displayModelName(selectedEntry)
			: ''
	);

	const menuItems = $derived.by(() => {
		const items = [
			...(nullable
				? [
						{
							label: nullLabel,
							tooltip: nullLabel,
							active: selectedModel === null || selectedModel === '',
							check: true,
							onclick: () => {
								selectedModel = null;
								onchange?.(null);
							}
						}
					]
				: []),
			...modelGroups.flatMap((group) =>
				group.models.map((model) => ({
					label: displayModelName(model),
					tooltip: `${group.label} · ${displayModelName(model)}`,
					section: group.label,
					wrapLabel: true,
					active: model.id === selectedModel,
					check: true,
					onclick: () => {
						selectedModel = model.id;
						onchange?.(model.id);
					}
				}))
			)
		];

		const highlighted = Math.min(highlightedIndex, Math.max(items.length - 1, 0));
		return items.map((item, index) => ({ ...item, highlighted: index === highlighted }));
	});

	function updateViewportSize() {
		isSmallViewport = (window.visualViewport?.width ?? window.innerWidth) < 640;
	}

	onMount(() => {
		updateViewportSize();
		window.addEventListener('resize', updateViewportSize);
		window.visualViewport?.addEventListener('resize', updateViewportSize);
		return () => {
			window.removeEventListener('resize', updateViewportSize);
			window.visualViewport?.removeEventListener('resize', updateViewportSize);
		};
	});

	async function focusSearchInput() {
		await tick();
		await tick();
		searchInputEl?.focus();
		searchInputEl?.select();
	}

	function selectedIndex() {
		if (nullable && (selectedModel === null || selectedModel === '')) return 0;
		const index = filtered.findIndex((m) => m.id === selectedModel);
		return index >= 0 ? index + (nullable ? 1 : 0) : 0;
	}

	function resetHighlightedIndex() {
		const total = filtered.length + (nullable ? 1 : 0);
		highlightedIndex = total > 0 ? Math.min(selectedIndex(), total - 1) : 0;
	}

	function moveHighlightedIndex(delta: number) {
		const total = menuItems.length;
		if (total === 0) return;
		highlightedIndex = (highlightedIndex + delta + total) % total;
	}

	export async function openSelector() {
		if ($chatModels.length === 0 && !nullable) return;
		open = true;
		search = '';
		resetHighlightedIndex();
		await focusSearchInput();
	}

	async function toggle() {
		if (open) {
			open = false;
			onclose?.();
			return;
		}
		await openSelector();
	}

	function closeSelector() {
		open = false;
		onclose?.();
	}
</script>

<span class="relative inline-flex {open ? 'z-[1001]' : ''}">
	<button
		bind:this={btnEl}
		class="touch-target app-interactive flex items-center gap-1.5 px-2 py-1 rounded-xl text-[0.6875rem] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={toggle}
		title={selectedLabel || nullLabel}
	>
		<span class="truncate max-w-[12rem] sm:max-w-[16rem]"
			>{selectedModel === null || selectedModel === ''
				? nullLabel
				: $chatModels.length === 0
					? $t('modelSelector.noModels')
					: selectedLabel || $t('modelSelector.selectModel')}</span
		>
		{#if $chatModels.length > 0 || nullable}
			<svg
				class="w-3 h-3 opacity-50"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.5"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<polyline points="6 9 12 15 18 9" />
			</svg>
		{/if}
	</button>

	{#if open && btnEl && ($chatModels.length > 0 || nullable)}
		<DropdownMenu
			items={menuItems}
			anchor={btnEl}
			onclose={closeSelector}
			{preferAbove}
			forceAbove={preferAbove}
			maxHeight={selectorMaxHeight}
			className="model-selector-menu w-[min(24rem,calc(100vw-1rem))]"
			scrollActiveIntoView
			scrollActiveBlock="center"
			{align}
		>
			{#snippet header()}
				<div class="flex items-center gap-2 h-10 px-2.5">
					<svg
						class="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
					</svg>
					<input
						bind:this={searchInputEl}
						value={search}
						placeholder={$t('modelSelector.search')}
						class="w-full bg-transparent text-base sm:text-xs text-gray-500 dark:text-gray-400 placeholder:text-gray-300 dark:placeholder:text-gray-600 outline-none"
						oninput={(e) => {
							search = e.currentTarget.value;
							resetHighlightedIndex();
						}}
						onkeydown={(e) => {
							if (e.key === 'Escape') {
								closeSelector();
							} else if (e.key === 'ArrowDown') {
								e.preventDefault();
								moveHighlightedIndex(1);
							} else if (e.key === 'ArrowUp') {
								e.preventDefault();
								moveHighlightedIndex(-1);
							} else if (e.key === 'Enter') {
								e.preventDefault();
								menuItems[Math.min(highlightedIndex, menuItems.length - 1)]?.onclick();
								closeSelector();
							}
						}}
					/>
				</div>
			{/snippet}
			{#snippet empty()}
				<div class="px-3 py-1.5 text-[0.6875rem] text-gray-400 dark:text-gray-500 text-center">
					{$t('modelSelector.noMatches')}
				</div>
			{/snippet}
		</DropdownMenu>
	{/if}
</span>
