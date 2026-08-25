import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, FlowDeckMode
from cptr.flowdeck.designer import (
    DesignerContractError,
    DesignerRequest,
    design_contract,
    run_designer,
)
from cptr.flowdeck.durable import DurableFlowDeck, OperationStatus
from cptr.models.base import Base
from cptr.models.workspaces import Workspace


class DesignerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "App.tsx").write_text(
            "export const App = () => <main className='signal'>Hi</main>;",
            encoding="utf-8",
        )
        (self.root / "src" / "theme.css").write_text(
            ":root { --brand: #c95f49; border-radius: 12px; } "
            "@media (max-width: 600px) { main { padding: 1rem; } }",
            encoding="utf-8",
        )
        self.db_file = tempfile.NamedTemporaryFile(delete=False)
        self.db_file.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_file.name}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False)
        )
        async with self.store.session_factory() as session:
            session.add(
                Workspace(
                    user_id="designer-user",
                    path=str(self.root),
                    name="designer fixture",
                    data={},
                    created_at=1,
                )
            )
            await session.commit()
        self.env = {
            "CPTR_FLOWDECK_ENABLED": "true",
            "CPTR_FLOWDECK_MODE": "controlled",
            "CPTR_FLOWDECK_GOVERNANCE": "strict",
        }

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_file.name)
        self.temp.cleanup()

    async def test_extract_and_variants_are_bounded_and_durable(self):
        request = DesignerRequest(
            "designer-extract",
            "extract",
            str(self.root),
            "designer-user",
            {},
        )
        with patch.dict(os.environ, self.env):
            result = await run_designer(request, store=self.store)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["operation"], "design_system")
        run = await self.store.get_run_by_request_key("designer-extract")
        operations = await self.store.get_run_operations(run.id)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].status, OperationStatus.SUCCEEDED.value)
        self.assertTrue(operations[0].authoritative_evidence["authoritative"])

        variant_result = await run_designer(
            DesignerRequest(
                "designer-variants", "variants", str(self.root), "designer-user", {}
            ),
            store=self.store,
        )
        self.assertEqual(len(variant_result["result"]["variants"]), 3)

    async def test_selection_is_read_only_and_screenshot_contract_fails_closed(self):
        with patch.dict(os.environ, self.env):
            result = await run_designer(
                DesignerRequest(
                    "designer-mix",
                    "mix",
                    str(self.root),
                    "designer-user",
                    {"selection": {"variant_ids": ["a", "b"]}},
                ),
                store=self.store,
            )
        self.assertFalse(result["result"]["applied"])
        (self.root / "fake.png").write_bytes(b"not an image")
        with self.assertRaises(DesignerContractError):
            await run_designer(
                DesignerRequest(
                    "designer-shot",
                    "screenshot_to_ui",
                    str(self.root),
                    "designer-user",
                    {"screenshot": "fake.png"},
                ),
                store=self.store,
            )

    def test_contract_and_registry_capability(self):
        self.assertEqual(
            design_contract("variants", {})["read_only"],
            True,
        )
        self.assertEqual(Capability.DESIGN_INSPECTION.value, "design_inspection")
        self.assertEqual(
            FlowDeckConfig(
                enabled=True,
                mode=FlowDeckMode.CONTROLLED,
                governance="strict",
            ).mode,
            FlowDeckMode.CONTROLLED,
        )