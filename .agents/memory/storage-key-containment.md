---
name: Storage key containment
description: Untrusted attachment identifiers must be contained by resolved upload-root checks.
---

Local blob storage treats every key as untrusted, even when normal database records use UUIDs. Resolved paths must remain inside the upload root, including symlink resolution, and callers must fail closed.

**Why:** Chat attachment metadata can be submitted independently of the managed file-upload route; trusting the UUID convention leaves an authenticated file-read escape.

**How to apply:** Enforce containment in the storage backend itself for get, put, and delete, then preserve a safe caller-facing error. Keep regression coverage for traversal, absolute paths, symlink escapes, and valid keys.