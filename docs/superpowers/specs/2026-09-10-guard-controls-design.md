# Guard Controls Design

## Goal

Add a real `/mcp` Guard Controls surface that lets the authenticated CPTR owner enable or disable configurable approval guards, with server-side persistence and enforcement. The UI must never become the authority boundary: authentication, owner/workspace isolation, token scopes, TOCTOU/stale-state checks, secret redaction, browser lease epochs, Capability OS lease/digest validation, and verification integrity remain non-configurable invariants.

## Architecture

`computer` owns a `GuardPolicyService` and durable owner-scoped guard settings. Browser UI reads/writes policy through normal authenticated `/api/mcp/guards` endpoints. The ChatGPT plugin receives a read-only effective policy through `/api/control/v1/guards`, authenticated by its existing `coding:read` Control API scope. The plugin uses that projection only for prompt-local delegation/secret-write friction; backend enforcement remains authoritative for backend-owned guards.

The registry is code-defined. A row stores only an owner override and monotonic version, never arbitrary guard metadata. Unknown guard IDs fail closed. Locked invariants are returned for display but cannot be mutated.

## Mutable guards

Initial registry:

- `delegation_prompt_approval`: require `allow:delegate` before Delegated Agent tools are enabled for a prompt session.
- `secret_write_prompt_approval`: require `allow:secret-write` before secret materialization is enabled for a prompt session.
- `root_prompt_approval`: require an explicit local-root prompt grant marker before a Workbench can receive local-root authority. The host-level `CPTR_LOCAL_ROOT_GRANTS_ENABLED` prerequisite remains authoritative.
- `network_operation_approval`: require per-operation `allow_network=true` for external network execution; `command:external` remains required.
- `package_install_approval`: require explicit package-install allowance before package-manager commands can execute; command/network scopes remain required.
- `browser_evaluate_approval`: require a separate expression-bound evaluate approval before user-Chrome `evaluate` executes.
- `autonomous_destructive_approval`: require supervisor approval checkpoints for detected external/destructive autonomous assignments.
- `task_review_approval`: require explicit task-review acceptance before reviewed changes are integrated/accepted.
- `capability_os_external_approval`: require owner approval for Capability OS operations already classified as approval-requiring by server standing authority. Disabling this guard may remove the extra owner checkpoint only when the requested authority is already within standing policy; it never widens scopes, leases, digests, or trust policy.
- `destructive_shell_approval`: require an explicit approval boundary before direct-coding commands that match the destructive-command classifier. Disabling it does not bypass workspace ownership, external/network requirements, dedicated SSH routing, root prerequisites, or file confinement.

Defaults are `enabled=true` for every mutable approval guard.

## Locked invariants

The API also returns locked display-only controls for:

- `identity_authentication`
- `control_api_scopes`
- `owner_workspace_isolation`
- `workspace_path_confinement`
- `stale_sha_protection`
- `browser_lease_epoch_validation`
- `secret_output_redaction`
- `dedicated_ssh_boundary`
- `live_ticket_signing_revocation`
- `capability_lease_digest_validation`
- `factory_verification_integrity`
- `cors_origin_validation`

Locked controls are always enabled, `mutable=false`, and PATCH attempts return 409/403 without changing state.

## Persistence

Add owner-scoped tables:

`guard_settings`
- `user_id` TEXT primary key component
- `guard_id` TEXT primary key component
- `enabled` BOOLEAN not null
- `version` BIGINT not null
- `updated_at` BIGINT not null

`guard_setting_events`
- `event_id` TEXT primary key
- `user_id` TEXT not null
- `guard_id` TEXT not null
- `previous_enabled` BOOLEAN not null
- `new_enabled` BOOLEAN not null
- `version` BIGINT not null
- `source` TEXT not null (`mcp_ui` or internal future source)
- `created_at` BIGINT not null

Each mutation runs in one DB transaction, checks `expected_version`, increments the version, and appends the event. Missing rows inherit registry defaults with version `0`.

## API

### Browser owner session

- `GET /api/mcp/guards`
  - requires normal CPTR authenticated owner session
  - returns effective guard catalog plus host capability metadata
- `PATCH /api/mcp/guards/{guard_id}` body `{ enabled: bool, expected_version: int }`
  - owner-scoped mutation
  - unknown IDs: 404
  - locked IDs: 409
  - stale version: 409 with current record
- `POST /api/mcp/guards/reset` body `{ expected_versions: {guard_id: int} }`
  - resets mutable overrides to defaults using optimistic concurrency

### Plugin read-only projection

- `GET /api/control/v1/guards`
  - requires existing `coding:read` scope
  - returns only the authenticated Control API owner’s effective guard catalog
  - no mutation endpoint exists on the Control API

## UI

Add lazy-loaded `/mcp` tab `Guards` with `McpGuardControls.svelte`.

The component shows:

1. protection summary (`N / total approval guards enforced`),
2. configurable guard cards with switch, risk level and short operational effect,
3. host prerequisite state for local root,
4. locked security invariant cards with lock icon and no interactive switch,
5. safe-default reset action.

Mutation UX:

- switch updates optimistically,
- request carries current version,
- failed/stale mutation rolls UI back and refreshes current policy,
- one guard mutation at a time per row; other rows remain usable,
- accessibility: actual `button role="switch"`, `aria-checked`, keyboard focus, visible disabled/busy state,
- no repetitive confirmation modal; the persisted owner setting is the explicit preference,
- high-risk disabled rows visibly indicate relaxed protection.

## Enforcement semantics

The effective authority remains an intersection:

`owner policy ∩ authenticated identity ∩ Control API scopes ∩ host prerequisites ∩ server standing authority ∩ operation-specific leases/epochs/digests`.

A disabled approval guard removes only that named approval requirement. It never manufactures another authority.

Plugin prompt guards are evaluated when opening/resuming a Workbench and snapshotted into the prompt session. The plugin fetches the effective policy from CPTR for each Workbench open/resume. If policy fetch fails, prompt-local approvals fail closed and retain current secure behavior.

Backend-owned guards are evaluated at the enforcement point against the authenticated user. A backend policy read failure fails closed.

## Compatibility and rollout

- Existing installations with no rows behave exactly as today because every mutable guard defaults enabled.
- No CPTR API token rotation is needed: plugin guard reads reuse `coding:read`.
- No existing request schema loses fields; explicit approval fields remain accepted and meaningful when a guard is enabled.
- Locked invariants cannot be disabled through either browser or Control API.

## Verification

Required checks:

- service registry/default/locked behavior,
- owner isolation,
- optimistic concurrency conflict,
- persistence/restart semantics through DB-backed tests,
- UI owner auth and Control API scope enforcement,
- prompt delegation/secret-write red-green tests in plugin,
- backend red-green tests for each enforcement point wired in this change,
- Svelte typecheck and production build,
- backend unit/integration suite,
- plugin tests/typecheck/build,
- final security review confirming no endpoint can mutate locked invariants or widen unrelated authority.
