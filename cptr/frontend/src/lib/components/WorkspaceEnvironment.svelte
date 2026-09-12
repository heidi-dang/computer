<script lang="ts">
	import { toast } from 'svelte-sonner';
	import {
		createWorkspaceEnvironment,
		createWorkspaceEnvironmentVersion,
		getWorkspaceEnvironmentVersions,
		setWorkspaceEnvironmentActiveVersion,
		setWorkspaceEnvironmentTarget,
		type EnvironmentProfileView,
		type EnvironmentVersionView
	} from '$lib/apis/workspace-os';

	interface Props {
		workspaceId: string;
		profiles: EnvironmentProfileView[];
		onrefresh: () => void | Promise<void>;
	}

	let { workspaceId, profiles, onrefresh }: Props = $props();

	let newProfileName = $state('');
	let newProfileDescription = $state('');
	let newProfileRuntime = $state('default');
	let creatingProfile = $state(false);
	let busyKey = $state('');
	let versions = $state<Record<string, EnvironmentVersionView[]>>({});
	let versionErrors = $state<Record<string, string>>({});
	let runtimeDrafts = $state<Record<string, string>>({});
	let envDrafts = $state<Record<string, string>>({});
	let packageDrafts = $state<Record<string, string>>({});
	let settingsDrafts = $state<Record<string, string>>({});
	let credentialDrafts = $state<Record<string, string>>({});
	let targetDrafts = $state<Record<string, string>>({});
	let makeActiveDrafts = $state<Record<string, boolean>>({});

	$effect(() => {
		for (const profile of profiles) {
			if (!(profile.profile_id in targetDrafts)) {
				targetDrafts = {
					...targetDrafts,
					[profile.profile_id]: profile.target_name ?? ''
				};
			}
			if (!(profile.profile_id in runtimeDrafts)) {
				runtimeDrafts = {
					...runtimeDrafts,
					[profile.profile_id]: profile.active_version?.runtime_profile ?? 'default'
				};
				envDrafts = { ...envDrafts, [profile.profile_id]: '{}' };
				packageDrafts = { ...packageDrafts, [profile.profile_id]: '{}' };
				settingsDrafts = { ...settingsDrafts, [profile.profile_id]: '{}' };
				credentialDrafts = { ...credentialDrafts, [profile.profile_id]: '[]' };
				makeActiveDrafts = { ...makeActiveDrafts, [profile.profile_id]: true };
			}
		}
	});

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Environment operation failed';
	}

	function parseObject(value: string, label: string): Record<string, unknown> {
		const parsed = JSON.parse(value || '{}') as unknown;
		if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
			throw new Error(label + ' must be a JSON object');
		}
		return parsed as Record<string, unknown>;
	}

	function parseCredentialRefs(value: string): unknown[] {
		const parsed = JSON.parse(value || '[]') as unknown;
		if (!Array.isArray(parsed)) throw new Error('Credential references must be a JSON array');
		return parsed;
	}

	async function createProfile() {
		if (!newProfileName.trim()) return;
		creatingProfile = true;
		try {
			await createWorkspaceEnvironment(workspaceId, {
				name: newProfileName.trim(),
				description: newProfileDescription.trim() || undefined,
				initial_spec: {
					runtime_profile: newProfileRuntime.trim() || 'default',
					environment_variables: {},
					packages: {},
					settings: {},
					credential_refs: []
				}
			});
			newProfileName = '';
			newProfileDescription = '';
			newProfileRuntime = 'default';
			await onrefresh();
			toast.success('Environment profile created');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			creatingProfile = false;
		}
	}

	async function loadVersions(profile: EnvironmentProfileView, force = false) {
		if (!force && versions[profile.profile_id]) return;
		busyKey = profile.profile_id + ':versions';
		try {
			const result = await getWorkspaceEnvironmentVersions(workspaceId, profile.profile_id);
			versions = { ...versions, [profile.profile_id]: result.versions };
			versionErrors = { ...versionErrors, [profile.profile_id]: '' };
		} catch (cause) {
			versionErrors = { ...versionErrors, [profile.profile_id]: message(cause) };
		} finally {
			busyKey = '';
		}
	}

	async function createVersion(profile: EnvironmentProfileView) {
		busyKey = profile.profile_id + ':create-version';
		try {
			const spec = {
				runtime_profile: runtimeDrafts[profile.profile_id]?.trim() || 'default',
				environment_variables: parseObject(
					envDrafts[profile.profile_id] ?? '{}',
					'Environment variables'
				),
				packages: parseObject(packageDrafts[profile.profile_id] ?? '{}', 'Packages'),
				settings: parseObject(settingsDrafts[profile.profile_id] ?? '{}', 'Settings'),
				credential_refs: parseCredentialRefs(credentialDrafts[profile.profile_id] ?? '[]')
			};
			await createWorkspaceEnvironmentVersion(
				workspaceId,
				profile.profile_id,
				spec,
				makeActiveDrafts[profile.profile_id] ?? true
			);
			await Promise.all([loadVersions(profile, true), onrefresh()]);
			toast.success('Environment version created');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function activateVersion(profile: EnvironmentProfileView, version: EnvironmentVersionView) {
		if (profile.active_version_id === version.version_id) return;
		busyKey = profile.profile_id + ':activate';
		try {
			await setWorkspaceEnvironmentActiveVersion(
				workspaceId,
				profile.profile_id,
				version.version_id
			);
			await onrefresh();
			toast.success('Environment version activated');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function saveTarget(profile: EnvironmentProfileView) {
		busyKey = profile.profile_id + ':target';
		try {
			await setWorkspaceEnvironmentTarget(
				workspaceId,
				profile.profile_id,
				targetDrafts[profile.profile_id]?.trim() || null
			);
			await onrefresh();
			toast.success('Environment target updated');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}
</script>

<section class="space-y-5" aria-label="Workspace environment">
	<div>
		<h3 class="text-base font-semibold">Environment lifecycle</h3>
		<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
			Immutable versions with explicit activation and target binding. Secret material is not
			returned; use broker credential references instead.
		</p>
	</div>

	<div class="environment-create">
		<div class="grid gap-2 md:grid-cols-2">
			<input class="field" placeholder="Profile name" bind:value={newProfileName} />
			<input class="field" placeholder="Runtime profile" bind:value={newProfileRuntime} />
			<input
				class="field md:col-span-2"
				placeholder="Description (optional)"
				bind:value={newProfileDescription}
			/>
		</div>
		<div class="mt-2 flex justify-end">
			<button
				class="primary"
				disabled={creatingProfile || !newProfileName.trim()}
				onclick={() => void createProfile()}
			>
				{creatingProfile ? 'Creating…' : 'Create profile'}
			</button>
		</div>
	</div>

	{#if profiles.length === 0}
		<div class="environment-empty">No Environment Profiles are configured for this Workspace.</div>
	{:else}
		<div class="space-y-4">
			{#each profiles as profile (profile.profile_id)}
				<article class="profile-card">
					<div class="profile-heading">
						<div class="min-w-0">
							<div class="flex flex-wrap items-center gap-2">
								<strong>{profile.name}</strong>
								{#if profile.active_version}
									<span class="chip">Active v{profile.active_version.version_number}</span>
								{/if}
								{#if profile.target_name}<span class="chip">{profile.target_name}</span>{/if}
							</div>
							<p>{profile.description || profile.profile_id}</p>
						</div>
						<button
							class="secondary"
							disabled={Boolean(busyKey)}
							onclick={() => void loadVersions(profile, true)}
						>
							{busyKey === profile.profile_id + ':versions' ? 'Loading…' : 'Version history'}
						</button>
					</div>

					<div class="profile-stats">
						<div>
							<span>Runtime</span>
							<strong>{profile.active_version?.runtime_profile ?? 'none'}</strong>
						</div>
						<div>
							<span>Env names</span>
							<strong>{profile.active_version?.environment_variable_names.length ?? 0}</strong>
						</div>
						<div>
							<span>Packages</span>
							<strong>{profile.active_version?.package_count ?? 0}</strong>
						</div>
						<div>
							<span>Credential refs</span>
							<strong>{profile.active_version?.credential_refs.length ?? 0}</strong>
						</div>
					</div>

					<div class="target-row">
						<input
							class="field"
							placeholder="Target name"
							value={targetDrafts[profile.profile_id] ?? ''}
							oninput={(event) =>
								(targetDrafts = {
									...targetDrafts,
									[profile.profile_id]: event.currentTarget.value
								})}
						/>
						<button
							class="secondary"
							disabled={Boolean(busyKey)}
							onclick={() => void saveTarget(profile)}
						>
							Save target
						</button>
					</div>

					<details class="version-builder">
						<summary>Create immutable version</summary>
						<div class="space-y-3 pt-3">
							<label>
								<span>Runtime profile</span>
								<input
									class="field"
									value={runtimeDrafts[profile.profile_id] ?? 'default'}
									oninput={(event) =>
										(runtimeDrafts = {
											...runtimeDrafts,
											[profile.profile_id]: event.currentTarget.value
										})}
								/>
							</label>
							<div class="grid gap-3 lg:grid-cols-2">
								<label>
									<span>Environment variables JSON</span>
									<textarea
										class="json-field"
										value={envDrafts[profile.profile_id] ?? '{}'}
										oninput={(event) =>
											(envDrafts = {
												...envDrafts,
												[profile.profile_id]: event.currentTarget.value
											})}
									></textarea>
								</label>
								<label>
									<span>Packages JSON</span>
									<textarea
										class="json-field"
										value={packageDrafts[profile.profile_id] ?? '{}'}
										oninput={(event) =>
											(packageDrafts = {
												...packageDrafts,
												[profile.profile_id]: event.currentTarget.value
											})}
									></textarea>
								</label>
								<label>
									<span>Settings JSON</span>
									<textarea
										class="json-field"
										value={settingsDrafts[profile.profile_id] ?? '{}'}
										oninput={(event) =>
											(settingsDrafts = {
												...settingsDrafts,
												[profile.profile_id]: event.currentTarget.value
											})}
									></textarea>
								</label>
								<label>
									<span>Credential references JSON</span>
									<textarea
										class="json-field"
										value={credentialDrafts[profile.profile_id] ?? '[]'}
										oninput={(event) =>
											(credentialDrafts = {
												...credentialDrafts,
												[profile.profile_id]: event.currentTarget.value
											})}
									></textarea>
								</label>
							</div>
							<div class="builder-footer">
								<label class="checkbox-row">
									<input
										type="checkbox"
										checked={makeActiveDrafts[profile.profile_id] ?? true}
										onchange={(event) =>
											(makeActiveDrafts = {
												...makeActiveDrafts,
												[profile.profile_id]: event.currentTarget.checked
											})}
									/>
									<span>Make active immediately</span>
								</label>
								<button
									class="primary"
									disabled={Boolean(busyKey)}
									onclick={() => void createVersion(profile)}
								>
									Create version
								</button>
							</div>
							<p class="security-note">
								Raw credential-like values are rejected by the backend. Credential references should
								identify broker-managed sources, not contain secrets.
							</p>
						</div>
					</details>

					{#if versionErrors[profile.profile_id]}
						<div class="environment-error">{versionErrors[profile.profile_id]}</div>
					{/if}

					{#if versions[profile.profile_id]}
						<div class="version-list">
							{#each versions[profile.profile_id] as version (version.version_id)}
								<div
									class="version-row"
									class:active={profile.active_version_id === version.version_id}
								>
									<div class="min-w-0">
										<div class="flex flex-wrap items-center gap-2">
											<strong>v{version.version_number}</strong>
											{#if profile.active_version_id === version.version_id}
												<span class="chip">Active</span>
											{/if}
											<span class="chip">{version.runtime_profile}</span>
											<code>{version.digest.slice(0, 10)}</code>
										</div>
										<small>
											{new Date(version.created_at_ms).toLocaleString()} ·
											{version.environment_variable_names.length} env ·
											{version.package_count} packages ·
											{version.credential_refs.length} refs
										</small>
									</div>
									{#if profile.active_version_id !== version.version_id}
										<button
											class="secondary compact"
											disabled={Boolean(busyKey)}
											onclick={() => void activateVersion(profile, version)}
										>
											Activate
										</button>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</article>
			{/each}
		</div>
	{/if}
</section>

<style>
	.environment-create,
	.profile-card {
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
	}

	.environment-create {
		padding: 0.85rem;
	}

	.field,
	.json-field {
		width: 100%;
		border: 1px solid var(--app-border);
		border-radius: 0.62rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		outline: none;
	}

	.field {
		padding: 0.5rem 0.65rem;
		font-size: 0.76rem;
	}

	.json-field {
		min-height: 8rem;
		resize: vertical;
		padding: 0.65rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.68rem;
		line-height: 1.4;
	}

	.primary,
	.secondary {
		border-radius: 0.62rem;
		padding: 0.48rem 0.72rem;
		font-size: 0.72rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}

	.secondary {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.primary:disabled,
	.secondary:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.compact {
		padding: 0.34rem 0.52rem;
		font-size: 0.65rem;
	}

	.profile-card {
		overflow: hidden;
	}

	.profile-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.8rem;
		padding: 0.85rem;
	}

	.profile-heading strong,
	.version-row strong {
		font-size: 0.78rem;
	}

	.profile-heading p,
	.version-row small,
	.security-note {
		margin-top: 0.2rem;
		font-size: 0.65rem;
		color: var(--app-muted-fg);
	}

	.chip {
		display: inline-flex;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.1rem 0.4rem;
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.profile-stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		border-top: 1px solid var(--app-border);
	}

	.profile-stats > div {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.7rem 0.85rem;
	}

	.profile-stats > div + div {
		border-left: 1px solid var(--app-border);
	}

	.profile-stats span,
	.version-builder label > span {
		font-size: 0.64rem;
		color: var(--app-muted-fg);
	}

	.profile-stats strong {
		font-size: 0.76rem;
	}

	.target-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.55rem;
		border-top: 1px solid var(--app-border);
		padding: 0.7rem 0.85rem;
	}

	.version-builder {
		border-top: 1px solid var(--app-border);
		padding: 0.7rem 0.85rem;
	}

	.version-builder summary {
		cursor: pointer;
		font-size: 0.74rem;
		font-weight: 650;
	}

	.version-builder label {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.builder-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}

	.checkbox-row {
		flex-direction: row !important;
		align-items: center;
		font-size: 0.7rem;
		color: var(--app-muted-fg);
	}

	.security-note {
		line-height: 1.45;
	}

	.version-list {
		border-top: 1px solid var(--app-border);
	}

	.version-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.7rem 0.85rem;
	}

	.version-row + .version-row {
		border-top: 1px solid var(--app-border);
	}

	.version-row.active {
		background: color-mix(in srgb, var(--app-hover) 75%, transparent);
	}

	.version-row code {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.62rem;
		color: var(--app-muted-fg);
	}

	.environment-empty,
	.environment-error {
		border: 1px dashed var(--app-border);
		border-radius: 0.8rem;
		padding: 1rem;
		font-size: 0.74rem;
		color: var(--app-muted-fg);
	}

	.environment-error {
		margin: 0.75rem;
		border-color: rgb(239 68 68 / 0.3);
		color: rgb(239 68 68);
	}

	@media (max-width: 720px) {
		.profile-stats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}

		.profile-stats > div:nth-child(3) {
			border-left: 0;
			border-top: 1px solid var(--app-border);
		}

		.profile-stats > div:nth-child(4) {
			border-top: 1px solid var(--app-border);
		}

		.target-row {
			grid-template-columns: 1fr;
		}

		.builder-footer {
			align-items: flex-start;
			flex-direction: column;
		}

		.version-row {
			align-items: flex-start;
			flex-direction: column;
		}
	}
</style>
