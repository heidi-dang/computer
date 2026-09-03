import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.browser_devices import BrowserDeviceStore


class BrowserDeviceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_pairing_approval_can_omit_code(self):
        store = BrowserDeviceStore()
        pairing_row = SimpleNamespace(
            user_id=None,
            code_hash="hashed-code",
            status="PENDING",
            expires_at=9999999999999,
            approved_at=None,
        )
        db = AsyncMock()
        db.get.return_value = pairing_row
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices._matches") as matches,
        ):
            result = await store.approve_pairing(user_id="user_1", pairing_id="pair_1")
        self.assertTrue(result)
        self.assertEqual(pairing_row.user_id, "user_1")
        self.assertEqual(pairing_row.status, "APPROVED")
        self.assertIsNotNone(pairing_row.approved_at)
        matches.assert_not_called()

    async def test_pairing_approval_still_validates_code_when_supplied(self):
        store = BrowserDeviceStore()
        pairing_row = SimpleNamespace(
            user_id=None,
            code_hash="hashed-code",
            status="PENDING",
            expires_at=9999999999999,
            approved_at=None,
        )
        db = AsyncMock()
        db.get.return_value = pairing_row
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices._matches", return_value=False) as matches,
        ):
            result = await store.approve_pairing(user_id="user_1", pairing_id="pair_1", code="123456")
        self.assertFalse(result)
        self.assertIsNone(pairing_row.user_id)
        self.assertEqual(pairing_row.status, "PENDING")
        matches.assert_called_once_with("123456", "hashed-code")

    async def test_pairing_claim_persists_hashes_only(self):
        store = BrowserDeviceStore()
        pairing_row = SimpleNamespace(
            id="pair_1",
            user_id="user_1",
            device_name="Heidi Chrome",
            claim_secret_hash="hash",
            status="APPROVED",
            expires_at=9999999999999,
            claimed_at=None,
        )
        device = SimpleNamespace(
            id="bdv_1",
            user_id="user_1",
            name="Heidi Chrome",
            credential_hash=None,
            credential_version=1,
            status="ACTIVE",
            created_at=1,
            updated_at=1,
        )
        db = AsyncMock()
        db.get.return_value = pairing_row
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        def add(value):
            setattr(value, "id", getattr(value, "id", None) or device.id)

        db.add = add
        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices._matches", return_value=True),
            patch("cptr.services.browser_devices.secrets.token_urlsafe", return_value="device-credential-secret"),
            patch("cptr.services.browser_devices._hash_secret", side_effect=lambda value: f"hashed:{value}"),
        ):
            result = await store.claim_pairing(pairing_id="pair_1", claim_secret="claim-secret")

        self.assertIsNotNone(result)
        claimed_device, raw_credential = result
        self.assertEqual(raw_credential, "device-credential-secret")
        self.assertEqual(claimed_device.credential_hash, "hashed:device-credential-secret")
        self.assertNotEqual(claimed_device.credential_hash, raw_credential)
        self.assertEqual(pairing_row.status, "CLAIMED")
        self.assertIsNotNone(pairing_row.claimed_at)

    async def test_authentication_rejects_revoked_device_before_secret_match(self):
        store = BrowserDeviceStore()
        device = SimpleNamespace(
            status="REVOKED",
            credential_hash="hashed-secret",
            last_seen_at=None,
            updated_at=1,
        )
        db = AsyncMock()
        db.get.return_value = device
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices._matches") as matches,
        ):
            result = await store.authenticate_device(device_id="bdv_1", credential="secret")
        self.assertIsNone(result)
        matches.assert_not_called()

    async def test_transfer_rejects_stale_epoch(self):
        store = BrowserDeviceStore()
        session = SimpleNamespace(id="brs_1", closed_at=None, snapshot_id="snap_old", state="AGENT_CONTROL", updated_at=1)
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_1",
            owner="agent",
            epoch=4,
            updated_at=1,
        )
        scalars = SimpleNamespace(first=lambda: lease)
        db = AsyncMock()
        db.get.return_value = session
        db.scalars.return_value = scalars
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            self.assertRaises(PermissionError),
        ):
            await store.transfer_lease(
                session_id="brs_1",
                expected_epoch=3,
                expected_owner="agent",
                new_owner="human",
            )
        self.assertEqual(lease.owner, "agent")
        self.assertEqual(lease.epoch, 4)

    async def test_return_to_agent_requires_fresh_snapshot_and_increments_epoch(self):
        store = BrowserDeviceStore()
        session = SimpleNamespace(id="brs_1", closed_at=None, snapshot_id="snap_old", state="HUMAN_CONTROL", updated_at=1)
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_1",
            owner="human",
            epoch=8,
            updated_at=1,
        )
        scalars = SimpleNamespace(first=lambda: lease)
        db = AsyncMock()
        db.get.return_value = session
        db.scalars.return_value = scalars
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)):
            with self.assertRaises(PermissionError):
                await store.transfer_lease(
                    session_id="brs_1",
                    expected_epoch=8,
                    expected_owner="human",
                    new_owner="agent",
                    fresh_snapshot_id="snap_old",
                )
            result = await store.transfer_lease(
                session_id="brs_1",
                expected_epoch=8,
                expected_owner="human",
                new_owner="agent",
                fresh_snapshot_id="snap_new",
            )
        self.assertEqual(result["owner"], "agent")
        self.assertEqual(result["epoch"], 9)
        self.assertEqual(result["snapshot_id"], "snap_new")
        self.assertEqual(result["state"], "AGENT_CONTROL")


if __name__ == "__main__":
    unittest.main()
