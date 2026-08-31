"""Helpers for updating per-model chat configuration."""

from __future__ import annotations


def apply_bulk_model_active_state(all_config: dict, model_ids: list[str], is_active: bool) -> dict:
    """Return model config with one active-state change applied to requested models."""
    updated = dict(all_config)
    for model_id in dict.fromkeys(model_ids):
        if not model_id or model_id == "*":
            continue

        entry = dict(updated.get(model_id) or {})
        entry["is_active"] = is_active

        # Active is the default state. Avoid persisting a redundant entry when
        # there is no other per-model configuration to preserve.
        if is_active and not entry.get("params") and set(entry) <= {"is_active", "params"}:
            updated.pop(model_id, None)
        else:
            updated[model_id] = entry

    return updated
