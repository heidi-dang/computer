<script lang="ts">
	import { onMount } from 'svelte';
	import { ApiError } from '$lib/apis';
	import {
		getMcpGuardControls,
		resetMcpGuardControls,
		updateMcpGuardControl,
		type McpGuardControl,
		type McpGuardControls
	} from '$lib/apis/mcp';

	let snapshot = $state<McpGuardControls | null>(null);
	let loading = $state(true);
	let errorMessage = $state<string | null>(null);
	let notice = $state<string | null>(null);
	let savingGuardId = $state<string | null>(null);
	let resetBusy = $state(false);
	let requestGeneration = 0;

	const approvalGuards = $derived(snapshot?.guards.filter((guard) => guard.mutable) ?? []);
	const lockedGuards = $derived(snapshot?.guards.filter((guard) => !guard.mutable) ?? []);
	const enabledApprovals = $derived(approvalGuards.filter((guard) => guard.enabled).length);

	function errorText(error: unknown, fallback: string): string {
		if (error instanceof ApiError) {
			if (error.status === 409) return 'Settings changed elsewhere. The latest state has been reloaded.';
			if (error.status === 401) return 'Your session expired. Sign in again to change Guard Controls.';
		}
		return error instanceof Error && error.message && error.message !== '[object Object]'
			? error.message
			: fallback;
	}

	function riskLabel(risk: McpGuardControl['risk']): string {
		if (risk === 'critical') return 'Critical';
		if (risk === 'high') return 'High';
		return 'Medium';
	}

	function riskClass(risk: McpGuardControl['risk']): string {
		if (risk === 'critical') return 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-300';
		if (risk === 'high') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300';
		return 'app-subtle-surface app-muted border';
	}

	async function loadGuards({ quiet = false }: { quiet?: boolean } = {}) {
		const generation = ++requestGeneration;
		if (!quiet) loading = true;
		errorMessage = null;
		try {
			const next = await getMcpGuardControls();
			if (generation !== requestGeneration) return;
			snapshot = next;
		} catch (error) {
			if (generation !== requestGeneration) return;
			errorMessage = errorText(error, 'Guard Controls could not be loaded.');
		} finally {
			if (generation === requestGeneration) loading = false;
		}
	}

	async function toggleGuard(guard: McpGuardControl) {
		if (!guard.mutable || savingGuardId || resetBusy) return;
		savingGuardId = guard.id;
		errorMessage = null;
		notice = null;
		try {
			const changed = await updateMcpGuardControl(guard.id, !guard.enabled, guard.version);
			if (!snapshot) return;
			snapshot = {
				...snapshot,
				guards: snapshot.guards.map((item) => (item.id === changed.id ? changed : item)),
				enabled_mutable_count: snapshot.guards.reduce(
					(count, item) =>
						count +
						(item.mutable
							? item.id === changed.id
								? Number(changed.enabled)
								: Number(item.enabled)
							: 0),
					0
				)
			};
			notice = `${changed.label} ${changed.enabled ? 'enabled' : 'disabled'}.`;
		} catch (error) {
			if (error instanceof ApiError && error.status === 409) {
				await loadGuards({ quiet: true });
				notice = 'GUARD_VERSION_CONFLICT: settings changed elsewhere; refreshed to the latest state.';
			} else {
				errorMessage = errorText(error, 'The guard setting could not be changed.');
			}
		} finally {
			savingGuardId = null;
		}
	}

	async function resetDefaults() {
		if (!snapshot || resetBusy || savingGuardId) return;
		resetBusy = true;
		errorMessage = null;
		notice = null;
		const expectedVersions = Object.fromEntries(
			snapshot.guards.filter((guard) => guard.mutable).map((guard) => [guard.id, guard.version])
		);
		try {
			snapshot = await resetMcpGuardControls(expectedVersions);
			notice = 'Approval controls reset to secure defaults.';
		} catch (error) {
			if (error instanceof ApiError && error.status === 409) {
				await loadGuards({ quiet: true });
				notice = 'GUARD_VERSION_CONFLICT: settings changed elsewhere; refreshed to the latest state.';
			} else {
				errorMessage = errorText(error, 'Guard Controls could not be reset.');
			}
		} finally {
			resetBusy = false;
		}
	}

	onMount(() => {
		void loadGuards();
	});
</script>

<div class="h-full overflow-y-auto p-3 sm:p-5">
	<div class="mx-auto flex w-full max-w-6xl flex-col gap-4 pb-8">
		<section class="app-surface overflow-hidden rounded-2xl border">
			<div class="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
				<div class="flex min-w-0 items-start gap-3">
					<div class="app-accent-surface flex size-11 shrink-0 items-center justify-center rounded-2xl border">
						<svg viewBox="0 0 24 24" class="size-5 app-accent" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
							<path stroke-linecap="round" stroke-linejoin="round" d="M12 3 5.5 5.6v5.6c0 4.2 2.6 7.8 6.5 9.8 3.9-2 6.5-5.6 6.5-9.8V5.6L12 3Z" />
							<path stroke-linecap="round" d="M9.2 12.1 11 14l3.9-4.2" />
						</svg>
					</div>
					<div class="min-w-0">
						<div class="flex flex-wrap items-center gap-2">
							<h2 class="text-base font-semibold sm:text-lg">Guard Controls</h2>
							<span class="app-subtle-surface rounded-full border px-2 py-0.5 text-[0.68rem] font-medium app-muted">Owner policy</span>
						</div>
						<p class="mt-1 max-w-2xl text-xs leading-5 app-muted sm:text-sm">
							Toggle approval friction without removing the locked authority, identity, isolation, lease, or verification boundaries underneath it.
						</p>
					</div>
				</div>

				{#if snapshot}
					<div class="grid grid-cols-3 gap-2 sm:min-w-72" aria-label="Guard summary">
						<div class="app-subtle-surface rounded-xl border px-3 py-2 text-center">
							<div class="text-base font-semibold">{enabledApprovals}/{approvalGuards.length}</div>
							<div class="text-[0.65rem] app-muted">Approvals on</div>
						</div>
						<div class="app-subtle-surface rounded-xl border px-3 py-2 text-center">
							<div class="text-base font-semibold">{lockedGuards.length}</div>
							<div class="text-[0.65rem] app-muted">Locked</div>
						</div>
						<div class="app-subtle-surface rounded-xl border px-3 py-2 text-center">
							<div class="text-base font-semibold">{snapshot.host_capabilities.local_root_grants ? 'Ready' : 'Off'}</div>
							<div class="text-[0.65rem] app-muted">Host root</div>
						</div>
					</div>
				{/if}
			</div>
		</section>

		{#if notice}
			<div class="app-accent-surface rounded-xl border px-3 py-2.5 text-xs" role="status" aria-live="polite">
				{notice}
			</div>
		{/if}

		{#if errorMessage}
			<div class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3" role="alert">
				<div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
					<p class="text-xs text-rose-700 dark:text-rose-300">{errorMessage}</p>
					<button class="app-interactive min-h-11 rounded-xl border px-3 text-xs font-medium sm:min-h-9" onclick={() => void loadGuards()}>
						Retry
					</button>
				</div>
			</div>
		{/if}

		{#if loading && !snapshot}
			<div class="app-surface rounded-2xl border p-5" role="status" aria-live="polite">
				<div class="flex items-center gap-3">
					<div class="app-accent-surface size-9 rounded-xl border"></div>
					<div>
						<p class="text-sm font-medium">Loading Guard Controls…</p>
						<p class="mt-1 text-xs app-muted">Reading the authenticated owner policy.</p>
					</div>
				</div>
			</div>
		{:else if snapshot}
			<section class="app-surface rounded-2xl border p-3 sm:p-4">
				<div class="mb-3 flex flex-col gap-2 px-1 sm:flex-row sm:items-end sm:justify-between">
					<div>
						<h3 class="text-sm font-semibold">Approval controls</h3>
						<p class="mt-1 text-xs app-muted">Off removes only that approval checkpoint. Other authority boundaries remain enforced.</p>
					</div>
					<button
						class="app-interactive min-h-11 rounded-xl border px-3 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-9"
						disabled={resetBusy || savingGuardId !== null}
						onclick={() => void resetDefaults()}
					>
						{resetBusy ? 'Resetting…' : 'Reset defaults'}
					</button>
				</div>

				<div class="grid gap-2 lg:grid-cols-2">
					{#each approvalGuards as guard (guard.id)}
						<div class="app-subtle-surface flex min-h-24 items-center gap-3 rounded-xl border p-3 sm:p-4">
							<div class="min-w-0 flex-1">
								<div class="flex flex-wrap items-center gap-2">
									<h4 class="text-xs font-semibold sm:text-sm">{guard.label}</h4>
									<span class="rounded-full px-2 py-0.5 text-[0.62rem] font-semibold {riskClass(guard.risk)}">{riskLabel(guard.risk)}</span>
								</div>
								<p class="mt-1.5 text-[0.72rem] leading-5 app-muted">{guard.description}</p>
								{#if guard.id === 'root_prompt_approval' && !snapshot.host_capabilities.local_root_grants}
									<p class="mt-1 text-[0.68rem] font-medium app-muted">Host root grants are disabled by the operator.</p>
								{/if}
							</div>
							<button
								class="app-interactive relative min-h-11 w-14 shrink-0 rounded-full border p-1 disabled:cursor-not-allowed disabled:opacity-50"
								class:app-interactive-active={guard.enabled}
								role="switch"
								aria-checked={guard.enabled}
								aria-label={`${guard.label}: ${guard.enabled ? 'enabled' : 'disabled'}`}
								disabled={savingGuardId !== null || resetBusy}
								onclick={() => void toggleGuard(guard)}
							>
								<span class="block size-8 rounded-full border app-surface transition-transform duration-150 {guard.enabled ? 'translate-x-4' : 'translate-x-0'}"></span>
								<span class="sr-only">{savingGuardId === guard.id ? 'Saving' : guard.enabled ? 'Enabled' : 'Disabled'}</span>
							</button>
						</div>
					{/each}
				</div>
			</section>

			<section class="app-surface rounded-2xl border p-3 sm:p-4">
				<div class="mb-3 px-1">
					<div class="flex flex-wrap items-center gap-2">
						<h3 class="text-sm font-semibold">Locked invariants</h3>
						<span class="app-accent-surface rounded-full border px-2 py-0.5 text-[0.65rem] font-semibold app-accent">Always enforced</span>
					</div>
					<p class="mt-1 text-xs app-muted">These controls cannot be disabled from the MCP UI or the Control API.</p>
				</div>

				<div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
					{#each lockedGuards as guard (guard.id)}
						<div class="app-subtle-surface rounded-xl border p-3">
							<div class="flex items-start gap-2.5">
								<div class="app-accent-surface flex size-8 shrink-0 items-center justify-center rounded-lg border">
									<svg viewBox="0 0 24 24" class="size-3.5 app-accent" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
										<rect x="5" y="10" width="14" height="10" rx="2" />
										<path d="M8.5 10V7.5a3.5 3.5 0 0 1 7 0V10" />
									</svg>
								</div>
								<div class="min-w-0">
									<div class="flex flex-wrap items-center gap-1.5">
										<h4 class="text-xs font-semibold">{guard.label}</h4>
										<span class="rounded-full px-1.5 py-0.5 text-[0.58rem] font-semibold {riskClass(guard.risk)}">{riskLabel(guard.risk)}</span>
									</div>
									<p class="mt-1 text-[0.7rem] leading-5 app-muted">{guard.description}</p>
								</div>
							</div>
						</div>
					{/each}
				</div>
			</section>
		{/if}
	</div>
</div>

<style>
	@media (prefers-reduced-motion: reduce) {
		:global(.transition-transform) {
			transition: none !important;
		}
	}
</style>
