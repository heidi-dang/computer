---
name: Provider discovery fail-closed
description: Managed OpenAI-compatible endpoints may reject model enumeration while still serving configured runtime models.
---

Treat a model-discovery HTTP 405 as “unverified,” never as available and never as proof that the configured model is unusable. Keep the model visible with the provider response as the reason, disable selection and implicit fallback until an authoritative provider response confirms the model, and preserve the provider as optional rather than silently switching models.

**Why:** The managed model endpoint currently rejects GET `/models` with HTTP 405; configuration presence is not a health check and a different model must not be selected without the user’s choice.

**How to apply:** Any new provider discovery, refresh, model resolution, or fallback path must share the same verified-availability gate and retain truthful pricing/usage metadata.