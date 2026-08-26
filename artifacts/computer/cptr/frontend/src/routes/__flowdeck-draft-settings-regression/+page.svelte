<script lang="ts">
	import SettingsModal from '$lib/components/SettingsModal.svelte';
</script>

<svelte:head>
	<title>FlowDeck draft settings isolation fixture</title>
</svelte:head>

<SettingsModal initialTab="general" onclose={() => {}} />
