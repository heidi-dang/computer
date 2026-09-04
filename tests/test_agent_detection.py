import asyncio
from types import SimpleNamespace

import cptr.utils.agents.detection as detection


def test_disabled_profiles_skip_detection_and_preserve_configured_models(monkeypatch) -> None:
    detection_calls: list[str] = []

    async def fake_profiles() -> list[dict]:
        return [
            {
                "id": "disabled-cline",
                "agent": "cline",
                "name": "Disabled Cline",
                "mode": "disabled",
                "command": "must-not-run",
                "models": ["configured-model"],
                "default_model": "configured-model",
            },
            {
                "id": "enabled-codex",
                "agent": "codex",
                "name": "Enabled Codex",
                "mode": "enabled",
                "command": "codex",
                "models": [],
                "default_model": "",
            },
        ]

    async def fake_detect(profile: dict) -> detection.AgentDetection:
        detection_calls.append(profile["id"])
        return detection.AgentDetection(
            "ready",
            profile["command"],
            "1.0.0",
            None,
            ["detected-model"],
        )

    monkeypatch.setattr(detection, "get_raw_agent_profiles", fake_profiles)
    monkeypatch.setattr(detection, "detect_profile", fake_detect)

    async def scenario() -> tuple[dict, list[dict[str, str]]]:
        state = SimpleNamespace()
        status = await detection.get_agent_status(state)
        models = await detection.get_available_agent_model_entries(state)
        return status, models

    status, models = asyncio.run(scenario())
    profiles = {entry["id"]: entry for entry in status["profiles"]}

    disabled = profiles["disabled-cline"]
    assert detection_calls == ["enabled-codex"]
    assert disabled["detected"]["status"] == "disabled"
    assert disabled["detected"]["command"] is None
    assert disabled["available"] is False
    assert disabled["config"]["models"] == ["configured-model"]

    enabled = profiles["enabled-codex"]
    assert enabled["detected"]["status"] == "ready"
    assert enabled["available"] is True
    assert enabled["config"]["models"] == ["detected-model"]
    assert [entry["profile_id"] for entry in models] == ["enabled-codex"]
