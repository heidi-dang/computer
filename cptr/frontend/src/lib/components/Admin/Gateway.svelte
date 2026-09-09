<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { onMount } from 'svelte';
	import { fetchJSON, jsonBody } from '$lib/apis';
	import { getModelConfig, updateConfig } from '$lib/apis/admin';
	import { t } from '$lib/i18n';
	import Icon from '$lib/components/Icon.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	interface ApiKey {
		id: string;
		name: string;
		scopes?: string[];
		created_at: number;
	}

	interface GatewayModel {
		id: string;
		name: string;
		provider: string;
	}

	let keys = $state<ApiKey[]>([]);
	let models = $state<GatewayModel[]>([]);
	let loading = $state(true);
	let creating = $state(false);
	let saving = $state(false);
	let newKeyName = $state('');
	let newKeyCapabilityOs = $state(false);
	let updatingCapabilityKeyId = $state('');
	let selectedModel = $state('');

	/** Newly created key, shown once, then hidden */
	let revealedKey = $state('');
	const capabilityScopes = ['capability:read', 'capability:write', 'capability:execute'] as const;
	const openWebUIHeaders = `{
  "X-OpenWebUI-User-Name": "{{USER_NAME}}",
  "X-OpenWebUI-User-Id": "{{USER_ID}}",
  "X-OpenWebUI-User-Email": "{{USER_EMAIL}}",
  "X-OpenWebUI-User-Role": "{{USER_ROLE}}",
  "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
  "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
  "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
  "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
  "X-OpenWebUI-Task": "{{TASK}}"
}`;

	async function loadSettings() {
		try {
			const [loadedKeys, modelConfig, gatewayConfig] = await Promise.all([
				fetchJSON<ApiKey[]>('/v1/keys'),
				getModelConfig(),
				fetchJSON<{ config: Record<string, unknown> }>('/api/admin/config/gateway')
			]);
			keys = loadedKeys;
			models = modelConfig.models;
			selectedModel =
				typeof gatewayConfig.config['gateway.model'] === 'string'
					? gatewayConfig.config['gateway.model']
					: '';
		} catch {
			toast.error($t('admin.gateway.loadError'));
		} finally {
			loading = false;
		}
	}

	async function save() {
		saving = true;
		try {
			await updateConfig({ 'gateway.model': selectedModel });
			toast.success($t('settings.saved'));
		} catch {
			toast.error($t('admin.gateway.modelSaveError'));
		} finally {
			saving = false;
		}
	}

	function hasCapabilityOs(key: ApiKey) {
		return capabilityScopes.every((scope) => (key.scopes ?? []).includes(scope));
	}

	async function createKey() {
		if (!newKeyName.trim()) {
			newKeyName = 'default';
		}
		creating = true;
		try {
			const result = await fetchJSON<{ key: string; id: string; name: string }>(
				'/v1/keys',
				jsonBody({ name: newKeyName.trim(), capability_os: newKeyCapabilityOs })
			);
			revealedKey = result.key;
			newKeyName = '';
			newKeyCapabilityOs = false;
			const loadedKeys = await fetchJSON<ApiKey[]>('/v1/keys');
			keys = loadedKeys;
			toast.success($t('admin.gateway.keyCreated'));
		} catch {
			toast.error($t('admin.gateway.createError'));
		} finally {
			creating = false;
		}
	}

	async function setCapabilityOs(key: ApiKey, enabled: boolean) {
		updatingCapabilityKeyId = key.id;
		try {
			const updated = await fetchJSON<ApiKey & { ok: boolean }>(
				`/api/gateway/keys/${key.id}`,
				{ ...jsonBody({ capability_os: enabled }), method: 'PUT' }
			);
			keys = keys.map((item) => (item.id === key.id ? { ...item, scopes: updated.scopes } : item));
			toast.success(
				$t(enabled ? 'admin.gateway.capabilityEnabled' : 'admin.gateway.capabilityDisabled')
			);
		} catch {
			toast.error($t('admin.gateway.capabilityUpdateError'));
		} finally {
			updatingCapabilityKeyId = '';
		}
	}

	async function deleteKey(id: string) {
		try {
			await fetchJSON(`/v1/keys/${id}`, { method: 'DELETE' });
			keys = keys.filter((k) => k.id !== id);
			if (keys.length === 0) revealedKey = '';
			toast.success($t('admin.gateway.keyDeleted'));
		} catch {
			toast.error($t('admin.gateway.deleteError'));
		}
	}

	function copyKey() {
		navigator.clipboard.writeText(revealedKey);
		toast.success($t('admin.gateway.copied'));
	}

	function copyHeaders() {
		navigator.clipboard.writeText(openWebUIHeaders);
		toast.success($t('admin.gateway.copied'));
	}

	function formatDate(ts: number) {
		return new Date(ts * 1000).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		});
	}

	onMount(loadSettings);
</script>

<div class="flex flex-col min-h-full">
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-1">
		{$t('admin.gateway.title')}
	</h2>
	<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mb-4">
		{$t('admin.gateway.description')}
	</p>

	{#if loading}
		<div class="flex justify-center py-8">
			<Spinner size={16} />
		</div>
	{:else}
		<div class="mb-5 border-b border-gray-100 dark:border-white/5 pb-4">
			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">
				{$t('admin.gateway.model')}
			</h3>
			<div class="flex items-center gap-2">
				<select
					class="flex-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-gray-400 dark:focus:border-white/20 transition-colors"
					bind:value={selectedModel}
					disabled={saving}
				>
					<option value="">{$t('admin.gateway.modelFallback')}</option>
					{#each models as model (model.id)}
						<option value={model.id}>{model.id}</option>
					{/each}
				</select>
			</div>
			<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1.5">
				{$t('admin.gateway.modelDescription')}
			</p>
		</div>

		{#if revealedKey}
			<div class="mb-5 border-b border-gray-100 dark:border-white/5 pb-4">
				<div class="flex items-center justify-between gap-2 mb-2">
					<h3 class="text-xs text-gray-400 dark:text-gray-600">
						{$t('admin.gateway.newKey')}
					</h3>
					<button
						class="shrink-0 text-[0.6875rem] text-gray-500 hover:text-gray-900 dark:text-gray-500 dark:hover:text-white transition-colors"
						onclick={copyKey}
					>
						{$t('admin.gateway.copy')}
					</button>
				</div>
				<div class="flex items-center gap-2">
					<code
						class="flex-1 min-w-0 text-[0.6875rem] font-mono bg-gray-50 dark:bg-white/4 px-2.5 py-2 rounded-lg border border-gray-100 dark:border-white/5 text-gray-600 dark:text-gray-400 select-all break-all"
					>
						{revealedKey}
					</code>
				</div>
				<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1.5">
					{$t('admin.gateway.keyWarning')}
				</p>
			</div>
		{/if}

		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">{$t('admin.gateway.keys')}</h3>
		<div class="flex items-center gap-2 mb-2">
			<input
				type="text"
				class="flex-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-gray-400 dark:focus:border-white/20 transition-colors"
				placeholder={$t('admin.gateway.keyNamePlaceholder')}
				bind:value={newKeyName}
				onkeydown={(e) => e.key === 'Enter' && createKey()}
				disabled={creating}
			/>
			<button
				class="shrink-0 text-[0.8125rem] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100 disabled:opacity-50"
				onclick={createKey}
				disabled={creating}
			>
				{#if creating}
					<Spinner size={12} />
				{:else}
					{$t('admin.gateway.createKey')}
				{/if}
			</button>
		</div>
		<label class="mb-4 flex cursor-pointer items-start gap-2 text-[0.6875rem] text-gray-500 dark:text-gray-500">
			<input
				type="checkbox"
				class="mt-0.5 h-3 w-3 rounded accent-gray-600"
				bind:checked={newKeyCapabilityOs}
				disabled={creating}
			/>
			<span>
				<span class="block text-gray-600 dark:text-gray-400">{$t('admin.gateway.capabilityOs')}</span>
				<span class="block text-gray-400 dark:text-gray-600">
					{$t('admin.gateway.capabilityOsDescription')}
				</span>
			</span>
		</label>

		{#if keys.length === 0}
			<div
				class="flex flex-col items-center justify-center py-8 text-gray-400 dark:text-gray-600 border-b border-gray-100 dark:border-white/5"
			>
				<Icon name="shield" size={24} class="mb-2 opacity-40" />
				<p class="text-xs">{$t('admin.gateway.noKeys')}</p>
			</div>
		{:else}
			<div
				class="divide-y divide-gray-100 dark:divide-white/5 border-b border-gray-100 dark:border-white/5"
			>
				{#each keys as key (key.id)}
					<div class="flex min-h-10 items-center justify-between gap-3 py-1.5">
						<div class="flex min-w-0 items-center gap-2">
							<Icon name="shield" size={12} class="shrink-0 text-gray-400 dark:text-gray-600" />
							<span class="truncate text-xs font-medium text-gray-700 dark:text-gray-300">
								{key.name}
							</span>
							<span class="shrink-0 text-[0.625rem] text-gray-400 dark:text-gray-600">
								{formatDate(key.created_at)}
							</span>
							{#if hasCapabilityOs(key)}
								<span class="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[0.625rem] text-gray-500 dark:bg-white/6 dark:text-gray-500">
									{$t('admin.gateway.capabilityOs')}
								</span>
							{/if}
						</div>
						<div class="flex shrink-0 items-center gap-1">
							<button
								class="min-h-7 rounded px-2 text-[0.6875rem] text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50 dark:text-gray-500 dark:hover:bg-white/6 dark:hover:text-white"
								onclick={() => setCapabilityOs(key, !hasCapabilityOs(key))}
								disabled={updatingCapabilityKeyId === key.id}
							>
								{#if updatingCapabilityKeyId === key.id}
									<Spinner size={11} />
								{:else if hasCapabilityOs(key)}
									{$t('admin.gateway.capabilityDisable')}
								{:else}
									{$t('admin.gateway.capabilityEnable')}
								{/if}
							</button>
							<button
								class="shrink-0 p-1 text-gray-300 transition-colors hover:text-gray-600 disabled:opacity-50 dark:text-gray-700 dark:hover:text-gray-400"
								onclick={() => deleteKey(key.id)}
								title={$t('admin.delete')}
								disabled={updatingCapabilityKeyId === key.id}
							>
								<Icon name="trash" size={12} />
							</button>
						</div>
					</div>
				{/each}
			</div>
		{/if}

		<div class="mt-5">
			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">
				{$t('admin.gateway.howToConnect')}
			</h3>
			<div class="space-y-1.5 text-[0.6875rem] font-mono text-gray-600 dark:text-gray-400">
				<div>
					<span class="text-gray-400 dark:text-gray-600">{$t('admin.gateway.baseUrl')}</span>
					<span class="text-gray-700 dark:text-gray-300"
						>{`${typeof window !== 'undefined' ? window.location.origin : ''}/v1`}</span
					>
				</div>
				<div>
					<span class="text-gray-400 dark:text-gray-600">{$t('admin.gateway.apiKey')}</span>
					<span class="text-gray-700 dark:text-gray-300">sk-cptr-...</span>
				</div>
				<div class="pt-1">
					<div class="flex items-center justify-between gap-2 mb-1">
						<span class="text-gray-400 dark:text-gray-600">{$t('admin.gateway.headers')}</span>
						<button
							class="shrink-0 text-[0.6875rem] font-sans text-gray-500 hover:text-gray-900 dark:text-gray-500 dark:hover:text-white transition-colors"
							onclick={copyHeaders}
						>
							{$t('admin.gateway.copy')}
						</button>
					</div>
					<button
						class="w-full text-left whitespace-pre-wrap rounded-lg border border-gray-100 dark:border-white/5 bg-gray-50 dark:bg-white/4 px-2.5 py-2 text-[0.6875rem] font-mono text-gray-700 dark:text-gray-300 transition-colors hover:border-gray-200 dark:hover:border-white/10"
						onclick={copyHeaders}
					>
						{openWebUIHeaders}
					</button>
				</div>
			</div>
		</div>

		<div class="mt-auto pt-6 flex justify-end">
			<button
				class="text-[0.8125rem] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100 disabled:opacity-50"
				onclick={() => save()}
				disabled={saving}>{$t('settings.save')}</button
			>
		</div>
	{/if}
</div>
