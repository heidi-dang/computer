<script lang="ts">
	import { onMount } from 'svelte';
	import { getFlowDeckOrchestration, type FlowDeckOrchestration } from '$lib/apis/flowdeck';
	import FlowDeckStatusStrip from '$lib/components/chat/FlowDeckStatusStrip.svelte';

	const runId = 'report-reload-fixture';
	const workspace = '/workspace/project';
	let run = $state<FlowDeckOrchestration | null>(null);
	let loadError = $state('');

	onMount(() => {
		void getFlowDeckOrchestration(runId, workspace)
			.then((result) => {
				run = result;
			})
			.catch(() => {
				loadError = 'The report reload fixture could not load its run.';
			});
	});
</script>

<svelte:head>
	<title>FlowDeck evidence report reload fixture</title>
</svelte:head>

<main class="fixture-page">
	<header>
		<p class="eyebrow">ISOLATED FLOWDECK FIXTURE</p>
		<h1>Evidence report recovery</h1>
		<p>
			Development-only fixture for verifying that an interrupted evidence report remains retryable
			after the run is rehydrated.
		</p>
	</header>

	{#if loadError}
		<p class="fixture-error" role="alert">{loadError}</p>
	{:else if run}
		<FlowDeckStatusStrip
			status={String(run.status || run.state || 'succeeded')}
			{runId}
			{workspace}
			isAudit={true}
			events={Array.isArray(run.events) ? run.events : []}
			evidenceSummary={run.evidence_summary || null}
		/>
	{:else}
		<p class="fixture-loading" role="status">Loading run evidence…</p>
	{/if}
</main>

<style>
	:global(body) {
		margin: 0;
		background: #0b0d10;
	}

	.fixture-page {
		box-sizing: border-box;
		min-height: 100vh;
		max-width: 900px;
		margin: 0 auto;
		padding: 2rem 1.25rem 4rem;
		background: #0b0d10;
		color: #e7e9ed;
		font-family:
			Inter,
			ui-sans-serif,
			system-ui,
			-apple-system,
			BlinkMacSystemFont,
			'Segoe UI',
			sans-serif;
	}

	header {
		margin-bottom: 1.5rem;
	}

	.eyebrow {
		margin: 0;
		color: #8f98a8;
		font:
			600 0.68rem ui-monospace,
			SFMono-Regular,
			Menlo,
			monospace;
		letter-spacing: 0.12em;
	}

	h1 {
		margin: 0.4rem 0;
		font-size: 1.75rem;
	}

	header p:last-child,
	.fixture-loading,
	.fixture-error {
		max-width: 42rem;
		margin: 0;
		color: #9ea7b5;
		line-height: 1.5;
	}

	.fixture-error {
		color: #fca5a5;
	}
</style>
