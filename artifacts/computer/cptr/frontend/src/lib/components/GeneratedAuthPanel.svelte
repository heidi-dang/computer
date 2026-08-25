<script lang="ts">
	import {
		getGeneratedAuthConfig,
		getGeneratedAuthCsrf,
		getGeneratedAuthSession,
		signInGeneratedAuth,
		signUpGeneratedAuth,
		signOutGeneratedAuth,
		type GeneratedAuthConfig
	} from '$lib/apis/generatedAuth';

	let { workspace }: { workspace: string } = $props();
	let config = $state<GeneratedAuthConfig | null>(null);
	let session = $state<{ user: { email: string; role: string }; expires_at: number } | null>(null);
	let email = $state('');
	let password = $state('');
	let csrf = $state('');
	let error = $state('');
	let loading = $state(false);

	async function inspect() {
		if (!workspace) return;
		loading = true;
		error = '';
		try {
			config = await getGeneratedAuthConfig(workspace);
			const csrfResponse = await getGeneratedAuthCsrf(workspace);
			csrf = csrfResponse.csrf;
			try {
				session = await getGeneratedAuthSession(workspace);
			} catch {
				session = null;
			}
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Auth inspection failed';
		} finally {
			loading = false;
		}
	}

	async function signIn() {
		loading = true;
		error = '';
		try {
			await signInGeneratedAuth(workspace, email, password, csrf);
			session = await getGeneratedAuthSession(workspace);
			password = '';
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Sign in failed';
		} finally {
			loading = false;
		}
	}

	async function signUp() {
		loading = true;
		error = '';
		try {
			await signUpGeneratedAuth(workspace, email, password, csrf);
			await signIn();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Sign up failed';
			loading = false;
		}
	}

	async function signOut() {
		loading = true;
		try {
			await signOutGeneratedAuth(workspace);
			session = null;
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Sign out failed';
		} finally {
			loading = false;
		}
	}
</script>

<section class="auth-panel" data-testid="generated-auth-panel" aria-labelledby="generated-auth-title">
	<div class="heading">
		<div><span class="eyebrow">GENERATED APP</span><h2 id="generated-auth-title">Authentication</h2></div>
		<button type="button" onclick={inspect} disabled={loading || !workspace}>Inspect</button>
	</div>
	{#if config}
		<div class="summary">
			<strong>{config.provider}</strong>
			<span>{config.preserved_existing_auth ? 'Existing auth preserved' : 'Bounded local auth'}</span>
			<span>{config.verified ? 'Verified' : 'External verifier not configured'}</span>
		</div>
		{#if config.capabilities.signup}
			<div class="form">
				<input type="email" bind:value={email} placeholder="Email" aria-label="Generated app email" />
				<input type="password" bind:value={password} placeholder="Password" aria-label="Generated app password" />
				<button type="button" onclick={signIn} disabled={loading || !csrf}>Sign in</button>
				<button type="button" onclick={signUp} disabled={loading || !csrf}>Sign up</button>
			</div>
		{/if}
		{#if session}
			<div class="session" role="status">
				<span>{session.user.email} · {session.user.role}</span>
				<button type="button" onclick={signOut} disabled={loading}>Sign out</button>
			</div>
		{:else}
			<p class="muted">No generated-app session is active.</p>
		{/if}
	{/if}
	{#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
	.auth-panel{margin-top:1.25rem;padding:1.25rem;border:1px solid #30343c;border-radius:1rem;background:#111318;color:#e7e9ed}
	.heading{display:flex;justify-content:space-between;align-items:start}.eyebrow{font:600 .65rem ui-monospace;color:#8f98a8;letter-spacing:.12em}h2{margin:.25rem 0 0;font-size:1.1rem}
	button,input{border:1px solid #3b424e;border-radius:.45rem;padding:.55rem;background:#191d24;color:#e7e9ed}button{background:#e8edf4;color:#111318;font-weight:600}.summary,.form,.session{display:flex;gap:.65rem;align-items:center;flex-wrap:wrap;margin-top:1rem}.summary{color:#b7c1cf;font-size:.78rem}.form input{flex:1;min-width:10rem}.session{justify-content:space-between}.muted{color:#8f98a8;font-size:.8rem}.error{color:#ff9d9d}
</style>