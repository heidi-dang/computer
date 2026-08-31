<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		onclose: () => void;
		class?: string;
		overlayClass?: string;
		children: Snippet;
	}

	let {
		onclose,
		class: className = '',
		overlayClass = 'bg-[#00101f]/65 backdrop-blur-sm items-end sm:items-center justify-center',
		children
	}: Props = $props();

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onclose();
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="fixed inset-0 z-[100] flex px-0 sm:px-4 py-0 sm:py-4 {overlayClass}"
	onmousedown={onclose}
	onkeydown={() => {}}
>
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="app-theme app-raised-surface modal-surface border rounded-t-3xl sm:rounded-3xl overflow-visible shadow-2xl {className}"
		style="color: var(--app-fg);"
		onmousedown={(e) => e.stopPropagation()}
		onkeydown={() => {}}
	>
		{@render children()}
	</div>
</div>

<style>
	.modal-surface {
		box-shadow: 0 1.5rem 5rem -2rem var(--app-shadow-color);
	}

	@media (max-width: 639px) {
		.modal-surface {
			max-width: 100vw;
			max-height: min(92dvh, calc(100dvh - env(safe-area-inset-top, 0px)));
			padding-bottom: env(safe-area-inset-bottom, 0px);
		}
	}
</style>
