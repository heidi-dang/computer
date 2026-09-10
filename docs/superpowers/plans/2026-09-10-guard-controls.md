# Guard Controls Implementation Plan

> **Execution:** implement in isolated latest-main worktrees for `heidi-dang/computer` and `heidi-dang/chatgpt-computer-plugin`. Follow TDD for each behavior. Do not merge or deploy without a separate exact authorization.

**Goal:** Add a real `/mcp` Guard Controls UI backed by durable owner-scoped policy that removes only selected approval requirements while preserving all unrelated authentication, scope, ownership, isolation, lease/epoch/digest, redaction, and verification invariants.

**Architecture:** `computer` owns the registry, persistence, policy evaluation, UI mutation API, Control API read projection, and backend-owned enforcement. The plugin fetches the owner’s effective read-only policy at Workbench open/resume and snapshots prompt-local delegation/secret-write permissions. Policy reads fail closed.

**Success criteria:** every displayed mutable toggle changes a real enforcement path; every locked invariant rejects mutation; policy is owner-scoped, restart-persistent, versioned/audited, and concurrency-safe; default behavior is unchanged on upgrade; frontend/plugin/backend checks pass.

---

## Task 1 — Durable policy registry and service

**Files**
- Create `cptr/models/guard_controls.py`
- Modify `cptr/models/__init__.py`
- Create `cptr/migrations/versions/0036_guard_controls.py`
- Create `cptr/services/guard_controls.py`
- Create `tests/test_guard_controls_service.py`

**Behavior**
- Code-owned registry for mutable approval guards and locked invariants.
- Mutable defaults are enabled; locked invariants are always enabled and immutable.
- Owner-scoped `guard_settings` row stores enabled/version/updated_at.
- `guard_setting_events` records every effective owner mutation.
- `set_guard` performs expected-version compare and increments monotonically.
- Concurrent/stale write returns a typed conflict without partial mutation.
- `reset` writes defaults with version increments rather than deleting history.
- Unknown guard IDs fail closed.

**TDD**
1. Write tests for defaults, immutable rejection, owner isolation, version conflict, persistence, event append, reset.
2. Run focused tests and confirm RED because service/models do not exist.
3. Implement minimum model/service/migration.
4. Re-run focused tests until GREEN.

## Task 2 — Browser UI API and read-only Control API projection

**Files**
- Create `cptr/routers/guard_controls.py`
- Modify `cptr/routers/__init__.py`
- Modify `cptr/app.py`
- Create `tests/test_guard_controls_router.py`

**Interfaces**
- `GET /api/mcp/guards` — normal CPTR owner session.
- `PATCH /api/mcp/guards/{guard_id}` — normal CPTR owner session; body `{enabled, expected_version}`.
- `POST /api/mcp/guards/reset` — normal CPTR owner session; body `{expected_versions}`.
- `GET /api/control/v1/guards` — existing scoped bearer, requires `coding:read`, read-only only.

**Security**
- No Control API mutation route.
- UI never receives/stores CPTR API bearer.
- Response includes host capability metadata (not secret config), including whether local-root host support exists.
- Owner identity comes only from authenticated request state / Control API principal.

**TDD**
1. Write router tests for owner auth, control scope, owner isolation, locked/unknown/stale conflicts.
2. Run RED.
3. Add router and registration.
4. Run GREEN.

## Task 3 — Backend direct-coding/secret/network/package/destructive enforcement

**Files**
- Modify `cptr/routers/coding.py`
- Modify `tests/test_direct_coding.py`
- Modify `tests/test_local_root_commands.py` only if behavior coverage needs extension.

**Semantics**
- `secret_write_prompt_approval` ON: retain exact `allow:secret-write` requirement. OFF: body token is optional, but owned Workbench validation, workspace/host target checks, root prerequisite for host paths, permissions, and redaction remain.
- `network_operation_approval` ON: external command/browser navigation needs explicit `allow_network=true`. OFF: the per-call flag is not required, but `command:external` is still mandatory for external effects.
- `package_install_approval` ON: package-manager install commands require explicit package-install permission. Add `allow_package_install` to direct command request/client/tool schema. OFF: this extra flag is not required; network/external scope rules still apply.
- `destructive_shell_approval` ON: retain destructive classifier unless an existing local-root grant authorizes it. OFF: classifier does not block, but all remaining scope/workspace/SSH/network/root boundaries stay active.
- `root_prompt_approval` controls only the explicit-root prompt friction where CPTR itself can verify it; host `CPTR_LOCAL_ROOT_GRANTS_ENABLED`, owned Workbench, root identity availability and root directive remain mandatory. It does not synthesize root authority.

**TDD**
1. Add failing policy-on/policy-off tests around secret, external, package, destructive commands and managed browser external navigation.
2. Run RED.
3. Inject `guard_policy_service.is_enabled(user_id, ...)` at route enforcement points; never alter locked invariants.
4. Run GREEN and existing direct-coding/root tests.

## Task 4 — Browser evaluate approval

**Files**
- Modify `cptr/routers/browser_device.py`
- Modify `tests/test_browser_device_router.py`

**Semantics**
- `browser_evaluate_approval` ON: current expression/session/user-bound single-use approval token remains required.
- OFF: approval token is optional, but browser session ownership, agent lease, expected epoch, command reservation/result lifecycle and device authentication remain unchanged.

**TDD**
1. Add RED test proving evaluate succeeds without approval only when guard is disabled and still requires other mutation invariants.
2. Implement guard lookup.
3. Re-run focused browser tests GREEN.

## Task 5 — Autonomous approval and task review

**Files**
- Modify `cptr/services/supervisor.py`
- Modify `cptr/routers/control.py`
- Modify `tests/test_supervisor_core.py`
- Modify `tests/test_control_api.py`

**Semantics**
- `autonomous_destructive_approval` ON: current classifier creates APPROVAL_REQUIRED checkpoint. OFF: skip that owner checkpoint only; execution policy, model qualification, workspace writer lease and downstream command guards remain.
- `task_review_approval` ON: user-started delegated tasks retain `review_required=True`. OFF: create them with `review_required=False`. Autonomous child tasks already explicitly use `review_required=False` and remain unchanged.
- Delegation marker handling in backend is controlled by `delegation_prompt_approval`: ON retains `allow:delegate`; OFF removes only marker requirement while qualified model/profile and scopes remain mandatory.

**TDD**
1. RED tests for policy-controlled approval checkpoint, task `review_required`, and backend delegation marker requirement.
2. Implement lookups at create/delegate boundaries.
3. Re-run supervisor/control tests GREEN.

## Task 6 — Capability OS critical approval policy adapter

**Files**
- Modify `cptr/services/capability_os/authority.py` and its construction/wiring only as needed.
- Modify relevant Capability OS tests (`tests/test_capability_os_authority.py` or closest existing authority tests).

**Semantics**
- Standing task authority policy remains the ceiling.
- `capability_os_external_approval` ON: critical permissions require verified approval as today.
- OFF: skip only the explicit approval verifier for critical permissions that are already fully covered by standing task policy and all requested destinations/credentials are already allow-listed. Forbidden/out-of-policy permissions, unknown credentials, unknown network destinations, leases/digests/trust remain denied.
- Policy read failure retains approval requirement.

**TDD**
1. RED authority test showing a policy-covered critical request remains approval-required when guard ON and succeeds without approval only when OFF.
2. Implement owner-aware policy hook without widening TaskAuthorityPolicy.
3. Run Capability OS authority/control tests GREEN.

## Task 7 — `/mcp` Guard Controls UI

**Files**
- Create `cptr/frontend/src/lib/components/mcp/McpGuardControls.svelte`
- Modify `cptr/frontend/src/routes/mcp/+page.svelte`

**UX**
- Lazy-loaded `Guards` tab.
- Protection summary and root-host-capability status.
- Each mutable guard uses accessible `button role="switch"`, `aria-checked`, busy/disabled state, risk + concise effect.
- Optimistic switch update with rollback/refetch on stale/network failure.
- Locked invariant cards render non-interactive lock state.
- Reset-to-safe-defaults action uses current versions.
- Responsive/mobile layout and existing app theme classes; no new visual framework/dependency.

**Verification**
- `npm --prefix cptr/frontend run check`
- `npm --prefix cptr/frontend run build`
- format check.

## Task 8 — Plugin policy client and prompt-session enforcement

**Files**
- Modify `server/client/computer-client.ts`
- Modify `server/mcp.ts`
- Modify `server/schemas/tools.ts` for `allow_package_install` contract if needed.
- Modify `tests/client.test.ts`
- Modify `tests/mcp.test.ts` and/or `tests/mcp-compact.test.ts`

**Behavior**
- `ComputerClient.getGuardControls()` performs read-only `GET /api/control/v1/guards`.
- Workbench open/resume fetches policy once and snapshots prompt-local flags.
- Fetch failure => guard treated enabled (fail closed).
- `delegation_prompt_approval` OFF allows Delegated Agent tools without `allow:delegate`; ON preserves current requirement.
- `secret_write_prompt_approval` OFF allows materialize-secret tool routing without `allow:secret-write`; backend still applies its own effective guard and target security.
- Existing explicit approval fields stay accepted for compatibility.

**TDD**
1. Add client path/auth test and MCP policy ON/OFF/fetch-failure tests; run RED.
2. Implement minimal client/MCP changes.
3. Run focused plugin tests GREEN.

## Task 9 — Full verification and security review

**Backend**
- focused guard/direct-coding/browser/control/supervisor/Capability OS tests
- full Python test suite
- Ruff/format/type checks used by repo
- frontend Svelte check + production build + format check

**Plugin**
- focused client/MCP tests
- full test suite
- typecheck/build/lint/format scripts defined by repository

**Security assertions to inspect in final diff**
- no unauthenticated guard mutation path
- no plugin guard-write capability
- no locked invariant mutation path
- no owner ID accepted from caller payload
- no scope manufacturing
- `command:external` still mandatory for external effects
- host root prerequisite/owned Workbench/root identity unchanged
- browser expected epoch/lease unchanged
- Capability OS standing policy remains ceiling
- policy lookup failure is fail-closed
- default upgrade behavior exactly matches current secure behavior

## Delivery boundary

After verification, commit the isolated feature branches if repository state is clean and changes are scoped. Do not push/PR/merge/deploy unless explicitly authorized by the user for those exact delivery actions. Report both branch SHAs and verification evidence.
