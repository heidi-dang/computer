#!/bin/bash
set -e
# The imported CPTR artifact deliberately keeps its own frontend package
# manager boundary. Its trimmed artifact/package.json cannot match the
# generated starter entries that may remain in the shared lockfile, so a
# frozen workspace install fails before the real packages are reconciled.
pnpm install --no-frozen-lockfile
pnpm --filter db push
