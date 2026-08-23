# Frontend type-check notes

`pnpm run check` is a required validation step and must finish with zero errors.

The check currently reports non-fatal warnings from existing code:

- Svelte 5 notices about DOM bindings and initial prop values that are intentionally
  kept as local references for component elements and initial form state.
- CSS language-service warnings for Tailwind `@apply` and `@reference` directives.
  These are processed by the Vite/Tailwind build pipeline, but are not understood
  by `svelte-check`'s CSS parser.
- Existing accessibility hints in administrative and form components. They are
  warnings only; changes to those controls should resolve the relevant hint when
  that component is next revised.

Do not weaken `strict` or exclude chat files to silence these warnings. New
TypeScript errors should fail the check.