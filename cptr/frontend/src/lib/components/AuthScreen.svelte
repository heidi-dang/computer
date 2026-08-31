<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { login, setup, signup } from '$lib/apis/auth';
	import { ApiError } from '$lib/apis';
	import { t } from '$lib/i18n';
	import Spinner from '$lib/components/common/Spinner.svelte';

	interface Props {
		mode: 'password' | 'pam';
		needsSetup?: boolean;
		signupEnabled?: boolean;
		token?: string;
		onauth: () => void;
	}

	let { mode, needsSetup, signupEnabled = false, token = '', onauth }: Props = $props();

	let username = $state('');
	let password = $state('');
	let loading = $state(false);
	let isSignup = $state(false);

	const isSetup = $derived(mode === 'password' && needsSetup);

	async function submit() {
		if (isSetup && password.length < 6) {
			toast.error($t('auth.minChars'));
			return;
		}
		if (!username.trim()) {
			toast.error($t('auth.usernameRequired'));
			return;
		}
		if (!password) {
			toast.error($t('auth.passwordRequired'));
			return;
		}

		loading = true;
		try {
			if (isSetup) {
				await setup(username.trim(), password, token);
				onauth();
			} else if (isSignup) {
				const data = await signup(username.trim(), password);
				if (data.pending) {
					toast.success($t('auth.accountPending'));
					isSignup = false;
					username = '';
					password = '';
				} else {
					onauth();
				}
			} else {
				await login(username.trim(), password);
				onauth();
			}
		} catch (e) {
			const msg = e instanceof ApiError ? e.message : $t('auth.connectionFailed');
			toast.error(msg);
			password = '';
		} finally {
			loading = false;
		}
	}

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		submit();
	}
</script>

<div
	class="auth-shell app-theme flex items-center justify-center h-dvh p-4 sm:p-6"
	style="background: var(--app-bg); color: var(--app-fg);"
>
	<div class="auth-card app-raised-surface w-full max-w-md rounded-3xl border p-5 sm:p-7">
		<div class="mb-5 flex items-center gap-3">
			<img src="/favicon.png" alt="" class="size-9 rounded-xl" />
			<div>
				<h1 class="text-lg font-semibold tracking-tight text-gray-900 dark:text-white">Computer</h1>
				<p class="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500">Private workspace control</p>
			</div>
		</div>

		{#if isSetup}
			<p class="text-xs text-gray-400 dark:text-gray-600 -mt-2 mb-3">
				{$t('auth.createAccountHint')}
			</p>
		{:else if mode === 'pam'}
			<p class="text-xs text-gray-400 dark:text-gray-600 -mt-2 mb-3">
				{$t('auth.signInSystemHint')}
			</p>
		{/if}

		<form onsubmit={handleSubmit} action="javascript:void(0)">
			<h2 class="text-xs text-gray-400 dark:text-gray-600 mb-1">
				{isSetup ? $t('auth.setup') : isSignup ? $t('auth.signUp') : $t('auth.signIn')}
			</h2>
			<input
				type="text"
				placeholder={$t('auth.username')}
				bind:value={username}
				autocomplete="username"
				spellcheck="false"
				class="auth-input block w-full rounded-xl border bg-transparent px-3 py-2.5 text-sm text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none"
			/>
			<input
				type="password"
				placeholder={$t('auth.password')}
				bind:value={password}
				autocomplete={isSetup ? 'new-password' : 'current-password'}
				class="auth-input mt-2 block w-full rounded-xl border bg-transparent px-3 py-2.5 text-sm text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-700 outline-none"
			/>

			<button
				type="submit"
				disabled={loading || !password || !username.trim()}
				class="auth-submit app-interactive mt-3 flex min-h-11 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-medium disabled:opacity-30 disabled:pointer-events-none"
			>
				{#if loading}
					<Spinner size={14} />
				{:else}
					{isSetup
						? $t('auth.createAccountBtn')
						: isSignup
							? $t('auth.signUpBtn')
							: $t('auth.signInBtn')}
				{/if}
			</button>
		</form>

		{#if !isSetup && signupEnabled && mode === 'password'}
			<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-4">
				{#if isSignup}
					{$t('auth.alreadyHaveAccount')}
					<button
						type="button"
						class="text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors"
						onclick={() => {
							isSignup = false;
							password = '';
						}}>{$t('auth.signInLink')}</button
					>
				{:else}
					{$t('auth.dontHaveAccount')}
					<button
						type="button"
						class="text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors"
						onclick={() => {
							isSignup = true;
							password = '';
						}}>{$t('auth.signUpLink')}</button
					>
				{/if}
			</p>
		{/if}
	</div>
</div>

<style>
	.auth-shell {
		background-image:
			radial-gradient(circle at 20% 0%, color-mix(in oklab, var(--app-accent) 12%, transparent), transparent 34rem),
			radial-gradient(circle at 100% 100%, color-mix(in oklab, var(--app-accent) 8%, transparent), transparent 30rem);
	}

	.auth-card {
		box-shadow: 0 2rem 6rem -2.5rem var(--app-shadow-color);
	}

	.auth-input {
		border-color: var(--app-border);
		background: var(--app-surface-subtle);
	}

	.auth-input:focus {
		border-color: color-mix(in oklab, var(--app-accent) 42%, transparent);
		box-shadow: 0 0 0 3px color-mix(in oklab, var(--app-accent) 10%, transparent);
	}

	.auth-submit {
		background: var(--app-accent);
		color: var(--app-bg);
		box-shadow: 0 0.75rem 2rem -1rem color-mix(in oklab, var(--app-accent) 60%, transparent);
	}

	.auth-submit:hover:not(:disabled) {
		background: var(--app-accent-strong);
	}
</style>
