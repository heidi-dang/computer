import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.chat import warm_model_cache


def test_warm_model_cache_survives_provider_discovery_failure() -> None:
    state = SimpleNamespace()

    async def exercise() -> None:
        with (
            patch("cptr.routers.chat._get_connections", new=AsyncMock(return_value=[{"id": "bad-key"}])),
            patch(
                "cptr.routers.chat._get_connection_models",
                new=AsyncMock(side_effect=ValueError("Failed to decrypt API key")),
            ),
            patch(
                "cptr.utils.agents.detection.get_available_agent_model_entries",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await warm_model_cache(state)

    asyncio.run(exercise())
    assert state.model_warmup_failure_count == 1
