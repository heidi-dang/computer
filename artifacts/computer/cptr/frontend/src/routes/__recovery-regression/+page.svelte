<script lang="ts">
	import CheckpointPanel from '$lib/components/CheckpointPanel.svelte';
	import GeneratedAuthPanel from '$lib/components/GeneratedAuthPanel.svelte';
	import ManagedRuntimePanel from '$lib/components/ManagedRuntimePanel.svelte';
	import ProjectDatabasePanel from '$lib/components/ProjectDatabasePanel.svelte';

	const fixtureWorkspace = '/workspace/recovery-fixture';
</script>

<svelte:head>
	<title>Recovery panel regression fixtures</title>
</svelte:head>

<main class="fixture-page">
	<header>
		<p class="eyebrow">ISOLATED RECOVERY FIXTURE</p>
		<h1>Recovery panels</h1>
		<p>
			Development-only fixture for verifying that managed runtime, project database, generated
			authentication, and checkpoint failures stay user-safe.
		</p>
	</header>

	<ManagedRuntimePanel workspace={fixtureWorkspace} />
	<ProjectDatabasePanel workspace={fixtureWorkspace} />
	<GeneratedAuthPanel workspace={fixtureWorkspace} />
	<CheckpointPanel workspace={fixtureWorkspace} />
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

	header p:last-child {
		max-width: 42rem;
		margin: 0;
		color: #9ea7b5;
		line-height: 1.5;
	}
</style>
