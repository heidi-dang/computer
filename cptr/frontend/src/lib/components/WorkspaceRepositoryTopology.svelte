<script lang="ts">
	import type { WorkspaceRepositorySummary } from '$lib/apis/workspace-os';

	interface Props {
		workspaceName: string;
		repositories: WorkspaceRepositorySummary[];
	}

	let { workspaceName, repositories }: Props = $props();

	const enabledRepositories = $derived(repositories.filter((repository) => repository.enabled));
	const availableCheckouts = $derived(
		enabledRepositories.reduce(
			(total, repository) =>
				total + repository.checkouts.filter((checkout) => checkout.available).length,
			0
		)
	);

	function revision(value: string | null): string {
		return value ? value.slice(0, 8) : 'unknown';
	}
</script>

<section class="topology" aria-label="Repository topology">
	<div class="topology-heading">
		<div>
			<h4>Repository topology</h4>
			<p>Logical Workspace membership → canonical repositories → known checkouts.</p>
		</div>
		<div class="topology-summary" aria-label="Repository topology summary">
			<span><strong>{enabledRepositories.length}</strong> repositories</span>
			<span><strong>{availableCheckouts}</strong> available checkouts</span>
		</div>
	</div>

	<div class="topology-root">
		<div class="workspace-node">
			<span>Workspace</span>
			<strong>{workspaceName}</strong>
		</div>
		<div class="topology-trunk" aria-hidden="true"></div>
		<div class="repository-grid">
			{#if enabledRepositories.length === 0}
				<div class="topology-empty">No enabled repositories are attached to this Workspace.</div>
			{:else}
				{#each enabledRepositories as repository (repository.repository_id)}
					<article class="repository-node" class:primary={repository.primary}>
						<div class="repository-title">
							<div class="min-w-0">
								<div class="flex flex-wrap items-center gap-2">
									<strong>{repository.name}</strong>
									{#if repository.primary}<span class="chip">Primary</span>{/if}
									<span class="chip">{repository.role}</span>
								</div>
								<p>{repository.canonical_remote_identity}</p>
							</div>
							<span class="provider">{repository.provider ?? 'git'}</span>
						</div>

						<div class="checkout-list">
							{#if repository.checkouts.length === 0}
								<div class="checkout-empty">No checkout observations.</div>
							{:else}
								{#each repository.checkouts as checkout (checkout.checkout_id)}
									<div class="checkout-row" class:unavailable={!checkout.available}>
										<div>
											<div class="flex flex-wrap items-center gap-2">
												<strong>{checkout.branch || repository.default_branch || 'detached'}</strong
												>
												{#if checkout.canonical}<span class="chip">Canonical</span>{/if}
												<span class="chip">{checkout.checkout_kind}</span>
											</div>
											<small>{checkout.upstream || 'no upstream'}</small>
										</div>
										<div class="checkout-state">
											<span>{checkout.available ? 'Available' : 'Unavailable'}</span>
											<code>{revision(checkout.last_seen_revision)}</code>
										</div>
									</div>
								{/each}
							{/if}
						</div>
					</article>
				{/each}
			{/if}
		</div>
	</div>
</section>

<style>
	.topology {
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
		overflow: hidden;
	}

	.topology-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 1rem;
		border-bottom: 1px solid var(--app-border);
		padding: 0.85rem 0.9rem;
	}

	.topology-heading h4 {
		font-size: 0.84rem;
		font-weight: 650;
	}

	.topology-heading p,
	.repository-title p,
	.checkout-row small {
		margin-top: 0.2rem;
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.topology-summary {
		display: flex;
		flex-wrap: wrap;
		gap: 0.65rem;
		font-size: 0.68rem;
		color: var(--app-muted-fg);
	}

	.topology-summary strong {
		color: var(--app-fg);
	}

	.topology-root {
		padding: 0.9rem;
	}

	.workspace-node {
		display: inline-flex;
		min-width: 12rem;
		flex-direction: column;
		gap: 0.25rem;
		border: 1px solid var(--app-border);
		border-radius: 0.75rem;
		background: var(--app-hover);
		padding: 0.7rem 0.8rem;
	}

	.workspace-node span {
		font-size: 0.64rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--app-muted-fg);
	}

	.workspace-node strong {
		font-size: 0.82rem;
	}

	.topology-trunk {
		width: 1px;
		height: 1rem;
		margin-left: 1.35rem;
		background: var(--app-border);
	}

	.repository-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.75rem;
	}

	.repository-node {
		border: 1px solid var(--app-border);
		border-radius: 0.8rem;
		overflow: hidden;
		min-width: 0;
	}

	.repository-node.primary {
		border-color: color-mix(in srgb, var(--app-fg) 28%, var(--app-border));
	}

	.repository-title {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem;
	}

	.repository-title strong {
		font-size: 0.78rem;
	}

	.repository-title p {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.chip,
	.provider {
		display: inline-flex;
		align-items: center;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.1rem 0.4rem;
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.provider {
		flex: none;
	}

	.checkout-list {
		border-top: 1px solid var(--app-border);
	}

	.checkout-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.65rem 0.75rem;
	}

	.checkout-row + .checkout-row {
		border-top: 1px solid var(--app-border);
	}

	.checkout-row strong {
		font-size: 0.7rem;
	}

	.checkout-state {
		display: flex;
		flex: none;
		flex-direction: column;
		align-items: flex-end;
		gap: 0.15rem;
		font-size: 0.62rem;
		color: var(--app-muted-fg);
	}

	.checkout-state code {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	}

	.checkout-row.unavailable {
		opacity: 0.58;
	}

	.checkout-empty,
	.topology-empty {
		padding: 0.85rem;
		font-size: 0.72rem;
		color: var(--app-muted-fg);
	}

	@media (max-width: 760px) {
		.topology-heading {
			flex-direction: column;
		}

		.repository-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
