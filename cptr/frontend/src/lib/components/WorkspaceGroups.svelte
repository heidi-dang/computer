<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { getWorkspaceList, type WorkspaceListItem } from '$lib/apis/state';
	import {
		addWorkspaceGroupMember,
		createWorkspaceGroup,
		deleteWorkspaceGroup,
		listWorkspaceGroups,
		removeWorkspaceGroupMember,
		reorderWorkspaceGroupMembers,
		updateWorkspaceGroup,
		updateWorkspaceGroupMember,
		type WorkspaceGroup,
		type WorkspaceGroupMember
	} from '$lib/apis/workspace-os';
	import { requestConfirm } from '$lib/stores/confirm';

	interface Props {
		workspaceId: string;
		workspaceName: string;
	}

	let { workspaceId, workspaceName }: Props = $props();

	let groups = $state<WorkspaceGroup[]>([]);
	let workspaces = $state<WorkspaceListItem[]>([]);
	let loading = $state(true);
	let error = $state('');
	let busyKey = $state('');
	let newGroupName = $state('');
	let newGroupDescription = $state('');
	let groupNameDrafts = $state<Record<string, string>>({});
	let groupDescriptionDrafts = $state<Record<string, string>>({});
	let addMemberDrafts = $state<Record<string, string>>({});
	let memberRoleDrafts = $state<Record<string, string>>({});

	const currentGroups = $derived(
		groups.filter((group) => group.members.some((member) => member.workspace_id === workspaceId))
	);
	const otherGroups = $derived(
		groups.filter((group) => !group.members.some((member) => member.workspace_id === workspaceId))
	);

	function key(groupId: string, suffix: string): string {
		return groupId + ':' + suffix;
	}

	function message(value: unknown): string {
		if (value instanceof Error) return value.message;
		return typeof value === 'string' ? value : 'Workspace Group operation failed';
	}

	function syncDrafts(nextGroups: WorkspaceGroup[]) {
		groupNameDrafts = Object.fromEntries(nextGroups.map((group) => [group.id, group.name]));
		groupDescriptionDrafts = Object.fromEntries(
			nextGroups.map((group) => [group.id, group.description ?? ''])
		);
		memberRoleDrafts = Object.fromEntries(
			nextGroups.flatMap((group) =>
				group.members.map((member) => [key(group.id, member.workspace_id), member.role])
			)
		);
	}

	async function load() {
		loading = true;
		error = '';
		try {
			const [groupResult, workspaceResult] = await Promise.all([
				listWorkspaceGroups(),
				getWorkspaceList()
			]);
			groups = groupResult.groups ?? [];
			workspaces = workspaceResult;
			syncDrafts(groups);
		} catch (cause) {
			error = message(cause);
		} finally {
			loading = false;
		}
	}

	function replaceGroup(group: WorkspaceGroup) {
		groups = groups.some((item) => item.id === group.id)
			? groups.map((item) => (item.id === group.id ? group : item))
			: [...groups, group];
		groupNameDrafts = { ...groupNameDrafts, [group.id]: group.name };
		groupDescriptionDrafts = {
			...groupDescriptionDrafts,
			[group.id]: group.description ?? ''
		};
		memberRoleDrafts = {
			...memberRoleDrafts,
			...Object.fromEntries(
				group.members.map((member) => [key(group.id, member.workspace_id), member.role])
			)
		};
	}

	function availableMembers(group: WorkspaceGroup): WorkspaceListItem[] {
		const existing = new Set(group.members.map((member) => member.workspace_id));
		return workspaces.filter((workspace) => !existing.has(workspace.workspace_id));
	}

	async function createGroup() {
		const name = newGroupName.trim();
		if (!name) return;
		busyKey = 'create';
		try {
			const group = await createWorkspaceGroup({
				name,
				description: newGroupDescription.trim() || null,
				group_type: 'cross-repo',
				members: [{ workspace_id: workspaceId, role: 'member', primary: true }]
			});
			replaceGroup(group);
			newGroupName = '';
			newGroupDescription = '';
			toast.success('Workspace Group created');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function saveGroup(group: WorkspaceGroup) {
		const name = (groupNameDrafts[group.id] ?? '').trim();
		if (!name) return;
		busyKey = key(group.id, 'save');
		try {
			replaceGroup(
				await updateWorkspaceGroup(group.id, {
					name,
					description: groupDescriptionDrafts[group.id]?.trim() ?? ''
				})
			);
			toast.success('Workspace Group updated');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function deleteGroup(group: WorkspaceGroup) {
		const confirmed = await requestConfirm({
			title: 'Delete Workspace Group?',
			message:
				'Delete "' +
				group.name +
				'" and its membership links? This does not delete any Workspace or repository.',
			confirmLabel: 'Delete group'
		});
		if (!confirmed) return;
		busyKey = key(group.id, 'delete');
		try {
			await deleteWorkspaceGroup(group.id);
			groups = groups.filter((item) => item.id !== group.id);
			toast.success('Workspace Group deleted');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function addMember(group: WorkspaceGroup) {
		const memberWorkspaceId = addMemberDrafts[group.id];
		if (!memberWorkspaceId) return;
		busyKey = key(group.id, 'add');
		try {
			replaceGroup(
				await addWorkspaceGroupMember(group.id, {
					workspace_id: memberWorkspaceId,
					role: 'member',
					primary: group.members.length === 0
				})
			);
			addMemberDrafts = { ...addMemberDrafts, [group.id]: '' };
			toast.success('Workspace added to Group');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function saveRole(group: WorkspaceGroup, member: WorkspaceGroupMember) {
		const role = (memberRoleDrafts[key(group.id, member.workspace_id)] ?? '').trim();
		if (!role || role === member.role) return;
		busyKey = key(group.id, 'role:' + member.workspace_id);
		try {
			replaceGroup(
				await updateWorkspaceGroupMember(group.id, member.workspace_id, {
					role
				})
			);
		} catch (cause) {
			memberRoleDrafts = {
				...memberRoleDrafts,
				[key(group.id, member.workspace_id)]: member.role
			};
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function setPrimary(group: WorkspaceGroup, member: WorkspaceGroupMember) {
		if (member.primary) return;
		busyKey = key(group.id, 'primary:' + member.workspace_id);
		try {
			replaceGroup(
				await updateWorkspaceGroupMember(group.id, member.workspace_id, {
					primary: true
				})
			);
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function removeMember(group: WorkspaceGroup, member: WorkspaceGroupMember) {
		const label = member.workspace?.name ?? member.workspace_id;
		const confirmed = await requestConfirm({
			title: 'Remove Workspace from Group?',
			message:
				'Remove "' +
				label +
				'" from "' +
				group.name +
				'"? The Workspace and its repositories remain unchanged.',
			confirmLabel: 'Remove'
		});
		if (!confirmed) return;
		busyKey = key(group.id, 'remove:' + member.workspace_id);
		try {
			replaceGroup(await removeWorkspaceGroupMember(group.id, member.workspace_id));
			toast.success('Workspace removed from Group');
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function moveMember(group: WorkspaceGroup, member: WorkspaceGroupMember, delta: -1 | 1) {
		const ordered = [...group.members].sort((a, b) => a.sort_order - b.sort_order);
		const index = ordered.findIndex((item) => item.workspace_id === member.workspace_id);
		const target = index + delta;
		if (index < 0 || target < 0 || target >= ordered.length) return;
		[ordered[index], ordered[target]] = [ordered[target], ordered[index]];
		busyKey = key(group.id, 'order');
		try {
			replaceGroup(
				await reorderWorkspaceGroupMembers(
					group.id,
					ordered.map((item) => item.workspace_id)
				)
			);
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	async function joinGroup(group: WorkspaceGroup) {
		busyKey = key(group.id, 'join');
		try {
			replaceGroup(
				await addWorkspaceGroupMember(group.id, {
					workspace_id: workspaceId,
					role: 'member',
					primary: group.members.length === 0
				})
			);
			toast.success(workspaceName + ' added to ' + group.name);
		} catch (cause) {
			toast.error(message(cause));
		} finally {
			busyKey = '';
		}
	}

	onMount(() => {
		void load();
	});
</script>

<section class="space-y-5" aria-label="Workspace Groups">
	<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<h3 class="text-base font-semibold">Workspace Groups</h3>
			<p class="mt-1 text-sm text-[var(--app-muted-fg)]">
				Persistent cross-Workspace structure. Group membership is independent from temporary workers
				and task fan-out.
			</p>
		</div>
		<button
			class="group-secondary"
			disabled={loading || Boolean(busyKey)}
			onclick={() => void load()}
		>
			Refresh
		</button>
	</div>

	<div class="group-create">
		<div class="grid gap-2 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_auto]">
			<input class="group-field" placeholder="New Group name" bind:value={newGroupName} />
			<input
				class="group-field"
				placeholder="Description (optional)"
				bind:value={newGroupDescription}
			/>
			<button
				class="group-primary"
				disabled={busyKey === 'create' || !newGroupName.trim()}
				onclick={() => void createGroup()}
			>
				Create with this Workspace
			</button>
		</div>
	</div>

	{#if loading}
		<div class="group-empty">Loading Workspace Groups…</div>
	{:else if error}
		<div class="group-error">
			<p>{error}</p>
			<button class="group-secondary" onclick={() => void load()}>Retry</button>
		</div>
	{:else}
		<div class="space-y-4">
			<div class="group-section-heading">
				<div>
					<h4>Groups containing {workspaceName}</h4>
					<p>
						{currentGroups.length} structural relationship{currentGroups.length === 1 ? '' : 's'}
					</p>
				</div>
			</div>

			{#if currentGroups.length === 0}
				<div class="group-empty">This Workspace is not yet a member of any Workspace Group.</div>
			{:else}
				{#each currentGroups as group (group.id)}
					<article class="group-card">
						<div class="group-card-heading">
							<div class="min-w-0 flex-1">
								<div class="flex flex-wrap items-center gap-2">
									<strong>{group.name}</strong>
									<span class="group-chip">{group.group_type}</span>
									<span class="group-chip">{group.member_count} members</span>
								</div>
								<p>{group.slug}</p>
							</div>
							<button
								class="group-danger"
								disabled={Boolean(busyKey)}
								onclick={() => void deleteGroup(group)}
							>
								Delete
							</button>
						</div>

						<div class="group-edit-grid">
							<input
								class="group-field"
								aria-label={'Group name for ' + group.name}
								value={groupNameDrafts[group.id] ?? group.name}
								oninput={(event) =>
									(groupNameDrafts = {
										...groupNameDrafts,
										[group.id]: event.currentTarget.value
									})}
							/>
							<input
								class="group-field"
								aria-label={'Group description for ' + group.name}
								placeholder="Description"
								value={groupDescriptionDrafts[group.id] ?? ''}
								oninput={(event) =>
									(groupDescriptionDrafts = {
										...groupDescriptionDrafts,
										[group.id]: event.currentTarget.value
									})}
							/>
							<button
								class="group-secondary"
								disabled={Boolean(busyKey)}
								onclick={() => void saveGroup(group)}
							>
								Save
							</button>
						</div>

						<div class="member-list">
							{#each [...group.members].sort((a, b) => a.sort_order - b.sort_order) as member, index (member.id)}
								<div class="member-row" class:disabled={!member.enabled}>
									<div class="member-identity">
										<div class="flex flex-wrap items-center gap-2">
											<strong>{member.workspace?.name ?? member.workspace_id}</strong>
											{#if member.primary}<span class="group-chip primary">Primary</span>{/if}
											{#if member.workspace_id === workspaceId}
												<span class="group-chip">Current</span>
											{/if}
											{#if member.workspace && !member.workspace.available}
												<span class="group-chip unavailable">Unavailable</span>
											{/if}
										</div>
										<small>{member.workspace?.path ?? member.workspace_id}</small>
									</div>

									<input
										class="group-field member-role"
										aria-label={'Role for ' + (member.workspace?.name ?? member.workspace_id)}
										value={memberRoleDrafts[key(group.id, member.workspace_id)] ?? member.role}
										oninput={(event) =>
											(memberRoleDrafts = {
												...memberRoleDrafts,
												[key(group.id, member.workspace_id)]: event.currentTarget.value
											})}
										onchange={() => void saveRole(group, member)}
									/>

									<div class="member-actions">
										<button
											class="group-icon"
											aria-label="Move member up"
											title="Move up"
											disabled={Boolean(busyKey) || index === 0}
											onclick={() => void moveMember(group, member, -1)}
										>
											↑
										</button>
										<button
											class="group-icon"
											aria-label="Move member down"
											title="Move down"
											disabled={Boolean(busyKey) || index === group.members.length - 1}
											onclick={() => void moveMember(group, member, 1)}
										>
											↓
										</button>
										{#if !member.primary}
											<button
												class="group-secondary compact"
												disabled={Boolean(busyKey)}
												onclick={() => void setPrimary(group, member)}
											>
												Make primary
											</button>
										{/if}
										<button
											class="group-danger compact"
											disabled={Boolean(busyKey)}
											onclick={() => void removeMember(group, member)}
										>
											Remove
										</button>
									</div>
								</div>
							{/each}
						</div>

						<div class="group-add-member">
							<select
								class="group-field"
								value={addMemberDrafts[group.id] ?? ''}
								onchange={(event) =>
									(addMemberDrafts = {
										...addMemberDrafts,
										[group.id]: event.currentTarget.value
									})}
							>
								<option value="">Add another Workspace…</option>
								{#each availableMembers(group) as candidate}
									<option value={candidate.workspace_id}>
										{candidate.name} · {candidate.workspace_type}
									</option>
								{/each}
							</select>
							<button
								class="group-primary"
								disabled={Boolean(busyKey) || !addMemberDrafts[group.id]}
								onclick={() => void addMember(group)}
							>
								Add member
							</button>
						</div>
					</article>
				{/each}
			{/if}

			{#if otherGroups.length > 0}
				<div class="group-section-heading secondary-section">
					<div>
						<h4>Other Groups</h4>
						<p>Existing Groups that do not currently contain this Workspace.</p>
					</div>
				</div>
				<div class="other-group-list">
					{#each otherGroups as group (group.id)}
						<div class="other-group-row">
							<div class="min-w-0">
								<div class="flex flex-wrap items-center gap-2">
									<strong>{group.name}</strong>
									<span class="group-chip">{group.member_count} members</span>
								</div>
								<p>{group.description || group.slug}</p>
							</div>
							<button
								class="group-secondary"
								disabled={Boolean(busyKey)}
								onclick={() => void joinGroup(group)}
							>
								Add this Workspace
							</button>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</section>

<style>
	.group-create,
	.group-card,
	.other-group-list {
		border: 1px solid var(--app-border);
		border-radius: 0.9rem;
	}

	.group-create {
		padding: 0.8rem;
	}

	.group-field {
		width: 100%;
		min-width: 0;
		border: 1px solid var(--app-border);
		border-radius: 0.6rem;
		background: color-mix(in srgb, var(--app-bg) 94%, transparent);
		padding: 0.5rem 0.6rem;
		font-size: 0.78rem;
		outline: none;
	}

	.group-field:focus {
		border-color: color-mix(in srgb, var(--app-fg) 35%, var(--app-border));
	}

	.group-primary,
	.group-secondary,
	.group-danger,
	.group-icon {
		border-radius: 0.6rem;
		padding: 0.5rem 0.7rem;
		font-size: 0.72rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.group-primary {
		background: var(--app-fg);
		color: var(--app-bg);
	}

	.group-secondary,
	.group-icon {
		border: 1px solid var(--app-border);
		background: var(--app-hover);
	}

	.group-danger {
		border: 1px solid rgb(239 68 68 / 0.32);
		color: rgb(239 68 68);
	}

	.group-primary:disabled,
	.group-secondary:disabled,
	.group-danger:disabled,
	.group-icon:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.group-error,
	.group-empty {
		border: 1px dashed var(--app-border);
		border-radius: 0.85rem;
		padding: 1rem;
		font-size: 0.78rem;
		color: var(--app-muted-fg);
	}

	.group-error {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		border-color: rgb(239 68 68 / 0.28);
		color: rgb(239 68 68);
	}

	.group-section-heading {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: 1rem;
	}

	.group-section-heading h4 {
		font-size: 0.8rem;
		font-weight: 650;
	}

	.group-section-heading p,
	.group-card-heading p,
	.member-identity small,
	.other-group-row p {
		margin-top: 0.18rem;
		font-size: 0.66rem;
		color: var(--app-muted-fg);
	}

	.secondary-section {
		padding-top: 0.35rem;
	}

	.group-card {
		overflow: hidden;
	}

	.group-card-heading {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.85rem;
	}

	.group-card-heading strong,
	.other-group-row strong {
		font-size: 0.8rem;
	}

	.group-chip {
		display: inline-flex;
		align-items: center;
		border: 1px solid var(--app-border);
		border-radius: 999px;
		padding: 0.1rem 0.4rem;
		font-size: 0.6rem;
		color: var(--app-muted-fg);
	}

	.group-chip.primary {
		color: rgb(34 197 94);
		border-color: rgb(34 197 94 / 0.28);
	}

	.group-chip.unavailable {
		color: rgb(239 68 68);
		border-color: rgb(239 68 68 / 0.28);
	}

	.group-edit-grid {
		display: grid;
		grid-template-columns: minmax(0, 0.75fr) minmax(0, 1.25fr) auto;
		gap: 0.55rem;
		border-top: 1px solid var(--app-border);
		padding: 0.7rem 0.85rem;
	}

	.member-list {
		border-top: 1px solid var(--app-border);
	}

	.member-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(8rem, 0.45fr) auto;
		align-items: center;
		gap: 0.65rem;
		padding: 0.7rem 0.85rem;
	}

	.member-row + .member-row {
		border-top: 1px solid var(--app-border);
	}

	.member-row.disabled {
		opacity: 0.6;
	}

	.member-identity {
		min-width: 0;
	}

	.member-identity strong {
		font-size: 0.74rem;
	}

	.member-identity small {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.member-role {
		padding-block: 0.4rem;
	}

	.member-actions {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.35rem;
	}

	.group-icon {
		width: 2rem;
		padding-inline: 0;
	}

	.compact {
		padding: 0.38rem 0.55rem;
		font-size: 0.66rem;
	}

	.group-add-member {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 0.55rem;
		border-top: 1px solid var(--app-border);
		padding: 0.7rem 0.85rem;
	}

	.other-group-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem 0.85rem;
	}

	.other-group-row + .other-group-row {
		border-top: 1px solid var(--app-border);
	}

	@media (max-width: 760px) {
		.group-edit-grid {
			grid-template-columns: 1fr;
		}

		.member-row {
			grid-template-columns: 1fr;
		}

		.member-actions {
			justify-content: flex-start;
			flex-wrap: wrap;
		}

		.group-add-member {
			grid-template-columns: 1fr;
		}

		.other-group-row {
			align-items: flex-start;
			flex-direction: column;
		}
	}
</style>
