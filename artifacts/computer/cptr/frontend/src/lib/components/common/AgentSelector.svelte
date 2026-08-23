<script lang="ts">
	import { onMount } from 'svelte';
	import DropdownMenu from '../DropdownMenu.svelte';
	import Icon from '../Icon.svelte';

	export type ChatAgent = 'computer' | 'heidi';

	interface Props {
		selectedAgent: ChatAgent;
		onchange?: (agent: ChatAgent) => void;
	}

	let { selectedAgent = $bindable(), onchange }: Props = $props();
	let buttonEl: HTMLButtonElement | undefined = $state();
	let open = $state(false);
	let isSmallViewport = $state(false);

	const agents: { id: ChatAgent; label: string; description: string; icon: string }[] = [
		{
			id: 'computer',
			label: 'Computer',
			description: 'Use the normal Computer chat',
			icon: 'spark'
		},
		{
			id: 'heidi',
			label: 'Heidi',
			description: 'Run through controlled FlowDeck orchestration',
			icon: 'shield'
		}
	];

	const selected = $derived(agents.find((agent) => agent.id === selectedAgent) ?? agents[0]);

	function updateViewport() {
		isSmallViewport = (window.visualViewport?.width ?? window.innerWidth) < 640;
	}

	onMount(() => {
		updateViewport();
		window.addEventListener('resize', updateViewport);
		window.visualViewport?.addEventListener('resize', updateViewport);
		return () => {
			window.removeEventListener('resize', updateViewport);
			window.visualViewport?.removeEventListener('resize', updateViewport);
		};
	});

	function choose(agent: ChatAgent) {
		selectedAgent = agent;
		onchange?.(agent);
		open = false;
	}

	function close() {
		open = false;
	}

	const menuItems = $derived(
		agents.map((agent) => ({
			label: `${agent.label} — ${agent.description}`,
			tooltip: `${agent.label}: ${agent.description}`,
			icon: agent.icon,
			active: agent.id === selectedAgent,
			check: true,
			onclick: () => choose(agent.id)
		}))
	);
</script>

<span class="relative inline-flex min-w-0 {open ? 'z-[1001]' : ''}">
	<button
		bind:this={buttonEl}
		type="button"
		class="agent-picker-trigger app-interactive flex min-h-9 min-w-0 max-w-[12rem] items-center gap-1.5 rounded-xl border px-2.5 py-1.5 text-xs font-medium transition-colors sm:max-w-[15rem] {selectedAgent ===
			'heidi'
			? 'flowdeck-agent-chip'
			: 'app-surface'}"
		aria-haspopup="menu"
		aria-expanded={open}
		title={`${selected.label}: ${selected.description}`}
		onclick={() => (open = !open)}
	>
		<span
			class="flex size-5 shrink-0 items-center justify-center rounded-lg {selectedAgent === 'heidi'
				? 'bg-cyan-400/15 text-cyan-300'
				: 'app-subtle-surface app-icon-muted'}"
		>
			<Icon name={selected.icon} size={13} strokeWidth={1.9} />
		</span>
		<span class="min-w-0 flex-1 text-left leading-tight">
			<span class="block text-[0.6rem] uppercase tracking-[0.13em] opacity-60">Agent</span>
			<span class="block truncate">{selected.label}</span>
		</span>
		<svg
			class="size-3 shrink-0 opacity-55"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2.5"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<polyline points="6 9 12 15 18 9" />
		</svg>
	</button>

	{#if open && buttonEl}
		<DropdownMenu
			items={menuItems}
			anchor={buttonEl}
			onclose={close}
			preferAbove
			forceAbove={isSmallViewport}
			maxHeight={isSmallViewport ? '10rem' : '15rem'}
			className="w-[min(22rem,calc(100vw-1rem))]"
			align="end"
		/>
	{/if}
</span>

<style>
	.flowdeck-agent-chip {
		border-color: color-mix(in oklab, #22d3ee 45%, transparent);
		background:
			linear-gradient(135deg, color-mix(in oklab, #0e7490 24%, transparent), transparent),
			color-mix(in oklab, var(--app-surface) 92%, #083344);
		color: color-mix(in oklab, var(--app-fg) 88%, #67e8f9);
	}

	@media (max-width: 380px) {
		.agent-picker-trigger {
			max-width: 9.5rem;
			padding-inline: 0.45rem;
		}
	}
</style>