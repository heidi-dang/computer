<script lang="ts">
	import type {
		McpBenchmarkLeaderboard,
		McpEngineeringSessionsResponse
	} from '$lib/apis/mcp';

	type Props = {
		benchmark: McpBenchmarkLeaderboard | null;
		engineering: McpEngineeringSessionsResponse | null;
	};

	let { benchmark, engineering }: Props = $props();
	const leader = $derived(benchmark?.models[0] ?? null);
	const latestSession = $derived(engineering?.sessions[0] ?? null);
	const sessionCount = $derived(engineering?.sessions.length ?? 0);

	function percent(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value)) return '—';
		return `${Math.round(value * 100)}%`;
	}

	function score(value: number | null | undefined, max = 100): string {
		if (value == null || !Number.isFinite(value)) return '—';
		return `${Math.round(value * 10) / 10}/${max}`;
	}
</script>

<section class="app-raised-surface overflow-hidden rounded-2xl border shadow-sm">
	<div class="app-surface flex flex-wrap items-start justify-between gap-3 border-b px-3 py-2.5 sm:px-4 sm:py-3">
		<div>
			<h2 class="text-sm font-semibold">Coding benchmark</h2>
			<p class="mt-0.5 text-[0.6875rem] app-muted">
				Objective standardized grading plus observed engineering evidence.
			</p>
		</div>
		{#if benchmark}
			<span class="app-subtle-surface rounded-full border px-2 py-1 text-[0.62rem] app-muted">
				{benchmark.suite_id} · v{benchmark.suite_version}
			</span>
		{/if}
	</div>

	<div class="grid grid-cols-1 lg:grid-cols-2">
		<div class="border-b p-3 sm:p-4 lg:border-b-0 lg:border-r">
			<div class="mb-3 flex items-center justify-between gap-2">
				<div>
					<p class="text-[0.62rem] font-semibold uppercase tracking-wide text-emerald-500">
						Comparable standardized
					</p>
					<p class="mt-0.5 text-[0.65rem] app-muted">Server-owned hidden grader · same suite/version</p>
				</div>
				<span class="rounded-full bg-emerald-500/10 px-2 py-1 text-[0.6rem] text-emerald-500">Leaderboard</span>
			</div>
			{#if leader}
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Model</p>
						<p class="mt-1 truncate text-xs font-semibold" title={leader.model_reported ?? leader.model_canonical}>
							{leader.model_reported ?? leader.model_canonical}
						</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Best score</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{score(leader.best_score, benchmark?.max_score ?? 100)}</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Average</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{score(leader.average_score, benchmark?.max_score ?? 100)}</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Perfect runs</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{leader.perfect_runs}/{leader.attempts}</p>
					</div>
				</div>
			{:else}
				<p class="app-subtle-surface rounded-xl border p-3 text-xs app-muted">
					No standardized benchmark has been submitted yet. Start one with the CPTR benchmark tool to create a comparable result.
				</p>
			{/if}
		</div>

		<div class="p-3 sm:p-4">
			<div class="mb-3 flex items-center justify-between gap-2">
				<div>
					<p class="text-[0.62rem] font-semibold uppercase tracking-wide app-muted">Observed real-work</p>
					<p class="mt-0.5 text-[0.65rem] app-muted">Operational telemetry · not comparable across different tasks</p>
				</div>
				<span class="app-subtle-surface rounded-full border px-2 py-1 text-[0.6rem] app-muted">{sessionCount} sessions</span>
			</div>
			{#if latestSession}
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Model</p>
						<p class="mt-1 truncate text-xs font-semibold" title={latestSession.model_reported ?? latestSession.model_canonical ?? 'Unreported'}>
							{latestSession.model_reported ?? latestSession.model_canonical ?? 'Unreported'}
						</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Reliability</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{percent(latestSession.reliability)}</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Verification</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{percent(latestSession.verification_ratio)}</p>
					</div>
					<div class="app-subtle-surface rounded-xl border p-2.5">
						<p class="text-[0.6rem] app-muted">Tool calls</p>
						<p class="mt-1 text-base font-semibold tabular-nums">{latestSession.tool_calls}</p>
					</div>
				</div>
			{:else}
				<p class="app-subtle-surface rounded-xl border p-3 text-xs app-muted">
					Real-work evidence appears after ChatGPT uses MCP coding tools in an attributed session.
				</p>
			{/if}
			<p class="mt-2 text-[0.62rem] leading-5 app-muted">
				Observed real-work scores are not comparable benchmark scores because task difficulty and scope differ.
			</p>
		</div>
	</div>
</section>
