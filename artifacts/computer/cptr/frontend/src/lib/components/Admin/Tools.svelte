<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		getAdminConfig,
		getToolApproval,
		updateConfig,
		type ToolApprovalGroup,
		type ToolApprovalPolicy
	} from '$lib/apis/admin';
	import { t } from '$lib/i18n';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ModelSelector from '$lib/components/common/ModelSelector.svelte';

	let loading = $state(true);
	let saving = $state(false);
	let reviewModel = $state<string | null>(null);
	let defaultApproval = $state<ToolApprovalPolicy>('review');
	let overrides = $state<Record<string, ToolApprovalPolicy>>({});
	let groups = $state<ToolApprovalGroup[]>([]);
	const toolCount = $derived(groups.reduce((count, group) => count + group.tools.length, 0));

	onMount(async () => {
		try {
			const [config, toolApproval] = await Promise.all([getAdminConfig(), getToolApproval()]);
			reviewModel =
				typeof config['tool_approval.review.model'] === 'string'
					? config['tool_approval.review.model']
					: null;
			defaultApproval =
				parseApproval(config['tool_approval.default_builtin_approval']) ??
				toolApproval.default_approval;
			overrides = parseOverrides(config['tool_approval.builtin_tools']);
			groups = toolApproval.groups;
		} catch {
			toast.error($t('admin.failedToLoadConfig'));
		} finally {
			loading = false;
		}
	});

	async function save() {
		saving = true;
		try {
			await updateConfig({
				'tool_approval.review.model': reviewModel,
				'tool_approval.default_builtin_approval': defaultApproval,
				'tool_approval.builtin_tools': overrides
			});
			toast.success($t('settings.saved'));
		} catch {
			toast.error($t('admin.failedToSave'));
		} finally {
			saving = false;
		}
	}

	function parseApproval(value: unknown): ToolApprovalPolicy | null {
		return value === 'allow' || value === 'review' ? value : null;
	}

	function parseOverrides(value: unknown): Record<string, ToolApprovalPolicy> {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
		const entries = Object.entries(value).flatMap(([name, policy]) => {
			const parsed = parseApproval(policy);
			return parsed ? [[name, parsed] as const] : [];
		});
		return Object.fromEntries(entries);
	}

	function setOverride(name: string, policy: ToolApprovalPolicy | null) {
		const next = { ...overrides };
		if (policy) next[name] = policy;
		else delete next[name];
		overrides = next;
	}

	function effective(tool: { name: string; default_approval: ToolApprovalPolicy | null }) {
		return overrides[tool.name] ?? tool.default_approval ?? defaultApproval ?? 'review';
	}

	function approvalLabel(policy: ToolApprovalPolicy) {
		return policy === 'allow' ? $t('admin.toolApprovalAllow') : $t('admin.toolApprovalReview');
	}
</script>

<div class="flex h-full flex-col">
	{#if loading}
		<div class="flex min-h-40 items-center justify-center"><Spinner size={18} /></div>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto pr-1.5 scrollbar-hover -mr-1.5">
			<div class="mb-5 flex items-start justify-between gap-4">
				<div>
					<div class="mb-1 flex items-center gap-2">
						<h2 class="text-base font-semibold tracking-tight text-gray-900 dark:text-white">{$t('admin.tools')}</h2>
						<span class="rounded-full bg-sky-500/10 px-2 py-0.5 font-mono text-[0.625rem] text-sky-600 dark:text-sky-300">{toolCount}</span>
					</div>
					<p class="text-[0.6875rem] text-gray-400 dark:text-gray-500">
						Configure how built-in tools ask for approval.
					</p>
				</div>
				<div class="flex size-8 shrink-0 items-center justify-center rounded-xl bg-sky-500/10 text-sky-600 dark:text-sky-300">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" class="size-4" aria-hidden="true">
						<path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h10.5M3 6h2.5M14.5 12h6.5M3 12h7.5M10.5 18H21M3 18h2.5" />
						<circle cx="8" cy="6" r="1.5" /><circle cx="12.5" cy="12" r="1.5" /><circle cx="8" cy="18" r="1.5" />
					</svg>
				</div>
			</div>

			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">
				{$t('admin.toolApproval')}
			</h3>
			<div class="mb-5 rounded-2xl border border-gray-200/70 bg-gray-50/60 p-3 dark:border-white/8 dark:bg-white/3">
			<div class="flex flex-col gap-2.5">
				<div class="rounded-xl border border-gray-200/60 bg-white/60 p-2.5 dark:border-white/6 dark:bg-white/3">
					<div class="flex items-center justify-between gap-3">
						<span class="min-w-0 text-xs text-gray-600 dark:text-gray-400">
							{$t('admin.toolApprovalReviewModel')}
						</span>
						<div class="shrink-0">
							<ModelSelector
								bind:selectedModel={reviewModel}
								nullable
								nullLabel={$t('modelSelector.currentModel')}
								preferAbove={false}
							/>
						</div>
					</div>
					<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 -mt-1">
						{$t('admin.toolApprovalReviewModelHint')}
					</p>
				</div>

				<div class="rounded-xl border border-gray-200/60 bg-white/60 p-2.5 dark:border-white/6 dark:bg-white/3">
					<div class="flex items-center justify-between gap-3">
						<span class="min-w-0 text-xs text-gray-600 dark:text-gray-400">
							{$t('admin.toolApprovalDefaultBuiltinApproval')}
						</span>
						<div class="shrink-0 flex gap-1">
							<button
								type="button"
								class="h-6 rounded-md px-2 text-[0.6875rem] transition-colors {defaultApproval ===
								'allow'
									? 'bg-gray-200/60 text-gray-900 dark:bg-white/10 dark:text-white'
									: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-300'}"
								onclick={() => (defaultApproval = 'allow')}
							>
								{$t('admin.toolApprovalAllow')}
							</button>
							<button
								type="button"
								class="h-6 rounded-md px-2 text-[0.6875rem] transition-colors {defaultApproval ===
								'review'
									? 'bg-gray-200/60 text-gray-900 dark:bg-white/10 dark:text-white'
									: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-300'}"
								onclick={() => (defaultApproval = 'review')}
							>
								{$t('admin.toolApprovalReview')}
							</button>
						</div>
					</div>
					<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 -mt-1">
						{$t('admin.toolApprovalDefaultBuiltinApprovalHint')}
					</p>
				</div>
			</div>
			</div>

			<div class="mt-5 rounded-2xl border border-gray-200/70 p-3 dark:border-white/7">
				<div class="flex items-center justify-between mb-2">
					<h3 class="text-xs font-semibold text-gray-800 dark:text-gray-200">
						{$t('admin.toolApprovalBuiltinTools')}
					</h3>
					{#if Object.keys(overrides).length > 0}
						<button
							type="button"
							class="text-[0.625rem] text-gray-400 dark:text-gray-600 hover:text-gray-600 dark:hover:text-gray-400 transition-colors duration-75"
							onclick={() => (overrides = {})}
						>
							{$t('models.resetToDefault')}
						</button>
					{/if}
				</div>
				<div class="flex flex-col gap-2">
					{#each groups as group}
						<div class="rounded-xl px-2 py-2 transition-colors duration-150 hover:bg-gray-100/70 dark:hover:bg-white/4">
							<div class="mb-1">
								<div class="text-[0.75rem] font-medium text-gray-700 dark:text-gray-300">
									{$t(`models.builtinTools.${group.id}`)}
								</div>
								<div class="text-[0.625rem] text-gray-400 dark:text-gray-500">
									{$t(`models.builtinTools.${group.id}Desc`)}
								</div>
							</div>
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-x-3 gap-y-1.5">
								{#each group.tools as tool}
									<div class="min-h-8">
										<div class="flex items-center justify-between gap-2">
											<code
												class="min-w-0 truncate text-[0.6875rem] text-gray-600 dark:text-gray-400"
											>
												{tool.name}
											</code>
											<div class="shrink-0 flex gap-1">
												<button
													type="button"
													class="h-6 rounded-md px-1.5 text-[0.625rem] transition-colors {overrides[
														tool.name
													] === undefined
														? 'bg-gray-200/60 text-gray-900 dark:bg-white/10 dark:text-white'
														: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-300'}"
													onclick={() => setOverride(tool.name, null)}
												>
													{$t('admin.toolApprovalDefault')}
												</button>
												<button
													type="button"
													class="h-6 rounded-md px-1.5 text-[0.625rem] transition-colors {overrides[
														tool.name
													] === 'allow'
														? 'bg-gray-200/60 text-gray-900 dark:bg-white/10 dark:text-white'
														: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-300'}"
													onclick={() => setOverride(tool.name, 'allow')}
												>
													{$t('admin.toolApprovalAllow')}
												</button>
												<button
													type="button"
													class="h-6 rounded-md px-1.5 text-[0.625rem] transition-colors {overrides[
														tool.name
													] === 'review'
														? 'bg-gray-200/60 text-gray-900 dark:bg-white/10 dark:text-white'
														: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-300'}"
													onclick={() => setOverride(tool.name, 'review')}
												>
													{$t('admin.toolApprovalReview')}
												</button>
											</div>
										</div>
										<p class="text-[0.625rem] text-gray-400 dark:text-gray-600 -mt-0.5">
											{$t('admin.toolApprovalEffective', {
												value: approvalLabel(effective(tool))
											})}
										</p>
									</div>
								{/each}
							</div>
						</div>
					{/each}
				</div>
			</div>
		</div>

		<div class="shrink-0 border-t border-gray-200/60 pt-3 dark:border-white/6">
			<button
				class="flex w-full items-center justify-center gap-2 rounded-xl bg-gray-900 px-3 py-2 text-[0.75rem] font-medium text-white shadow-sm transition-all duration-150 hover:-translate-y-px hover:bg-gray-700 disabled:opacity-50 dark:bg-white dark:text-gray-900 dark:hover:bg-gray-200"
				disabled={saving}
				onclick={save}
			>
				{saving ? $t('settings.saving') : $t('settings.save')}
				{#if !saving}<span aria-hidden="true">→</span>{/if}
			</button>
		</div>
	{/if}
</div>
