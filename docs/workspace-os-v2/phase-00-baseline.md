# Workspace OS v2 — Phase 0 Baseline

Revision inspected: `7394c28eefa2e2900e913bf1288ae73b13a96299` on `main`.

## Verified current state

- Workspace registry is path-centric. The persisted `Workspace` model is keyed by `(user_id, path)` and stores name plus generic UI data.
- Native workspace UX is path-first: `WorkspacePicker.svelte` and `SidebarWorkspaceList.svelte` select, route, cache chats, and identify workspaces using filesystem paths.
- Chat has no first-class `workspace_id`; workspace association remains in metadata/filesystem conventions.
- Path is used both as an execution root and as project identity across chat, memory, sockets, Factory, Direct Coding, SSH, and UI. Migration must preserve execution-root uses while replacing identity uses.
- The live registry contains multiple registrations for the same logical `chatgpt-computer-plugin` Git remote, including different branches/checkouts and one non-Git registration. Therefore Workspace, logical Repository, RepositoryCheckout, and Worker are distinct identities.
- The original `computer` checkout reports three dirty entries, all untracked `.cptr-worktrees/*` directories rather than tracked source modifications.
- Seven legacy Direct Coding workers report `WORKING`, no active commands, and no integrated/closed timestamps. Staleness must be classified conservatively rather than cleaned up automatically.
- FDX 0.1.0 is healthy and exposes read/search/outline/impact/why/evidence/semantic capabilities. Workspace OS should aggregate this existing signal rather than build new repository intelligence.
- Memory Core already provides namespace versioning, canonical records, branches, snapshots, checkpoints, retrieval, consolidation, redaction, and `prepare_context()`.
- The external/control memory read path currently rejects this credential with `CONTROL_SCOPE_REQUIRED: memory:read`; internal Memory Core remains present and testable.
- Workbench `workspace_id` is the durable/default workspace association. `active_workspace_id` is a transient projection for the currently active target and is cleared independently. They must remain separate.
- Local root grants are owner-bound and Workbench-bound, require an explicit server-recognized directive, support revocation/optional TTL, and otherwise persist until revoke/archive/delete. Workspace Admin Role must sit above this primitive and define stricter role lifetime semantics.
- Existing Capability OS operation compatibility (`inspect/resolve/forge/execute/acquire/reflect`) is a live contract and must not be repurposed by Workspace OS.

## Migration inventory rule

Every `workspace.path` usage must be classified as one of:

1. **Execution location** — filesystem/Git/runtime roots; keep path-based.
2. **Project identity** — chat ownership, memory namespace, URL/sidebar/socket keys, resume state, task grouping, cache keys, authorization scope; migrate to stable Workspace ID.

## Phase 0 acceptance

Phase 0 is complete when the implementation proceeds from this evidence, preserves existing work/checkouts, and treats all unverified mutable state as requiring current evidence rather than memory or names.
