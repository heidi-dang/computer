# FlowDeck Phase 10 Scope Review

Status: **BLOCKED — definitive repository specification not present**

## Evidence reviewed

The repository was searched for roadmap, phase, acceptance, generated-app,
authentication, Auth.js, Clerk, Supabase, Firebase, OAuth, and OIDC scope
definitions. The reviewed project records include:

- `docs/FLOWDECK_BASELINE.md`
- `docs/FLOWDECK_PHASE3_DESIGN.md`
- `docs/FLOWDECK_PHASE3_ACCEPTANCE.md`
- `docs/FLOWDECK_PHASE4_DEBUG_AUDIT.md`
- `docs/FLOWDECK_PHASE5_ACCEPTANCE.md`
- `docs/FLOWDECK_PHASE6_ACCEPTANCE.md`
- `docs/FLOWDECK_PHASE7_ACCEPTANCE.md`
- `docs/FLOWDECK_PHASE8_ACCEPTANCE.md`
- `docs/FLOWDECK_PHASE9_ACCEPTANCE.md`
- `docs/FLOWDECK_REGRESSION_MATRIX.md`
- `docs/TASK21_FINAL_AUTHORITY_AUDIT.md`
- repository `README` and project documentation
- `.agents/memory/MEMORY.md` and its linked Phase 1–9 decision notes

The only Phase 10 statements found in the repository are in the Phase 9
acceptance record:

- “Phase 10 has not started.”
- “Phase 10 itself was not started.”

No repository roadmap, design, contract, acceptance rubric, generated-project
template, provider-adapter interface, or end-to-end test plan defines Phase 10.

## Required action before implementation

Do not invent Phase 10 from the current prompt. Add or identify the canonical
Phase 10 specification in the repository first, including its generated-app
boundary, provider-adapter contract, server-owned configuration rules,
CPTR/FlowDeck authority relationship, accepted provider set, native-auth
contract, session/CSRF/CORS requirements, acceptance score rubric, and
disposable-fixture test requirements.

## Preservation statement

No Phase 10 implementation was retained. Phases 1–9 remain unchanged and
frozen. In particular, the accepted Phase 9 PostgreSQL rules remain
authoritative: `CPTR_PROJECT_DATABASE_URL` is the only project binding;
CPTR’s internal `DATABASE_URL` is excluded; cancellation is durable;
transactional rollback/checkpoint semantics are preserved; late outcomes are
discarded; and cancelled idempotency keys cannot resurrect work.