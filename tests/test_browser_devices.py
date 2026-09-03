import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cptr.services.browser_devices import BrowserDeviceStore, BrowserTabInUseError


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

    async def test_owns_active_device_is_owner_scoped_and_status_scoped(self):
        store = BrowserDeviceStore()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        with patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)):
            db.get.return_value = SimpleNamespace(user_id="user_1", status="ACTIVE")
            self.assertTrue(await store.owns_active_device(user_id="user_1", device_id="bdv_1"))
            self.assertFalse(await store.owns_active_device(user_id="user_2", device_id="bdv_1"))
            db.get.return_value = SimpleNamespace(user_id="user_1", status="REVOKED")
            self.assertFalse(await store.owns_active_device(user_id="user_1", device_id="bdv_1"))

    async def test_open_session_rejects_live_session_on_same_tab_before_repointing_lease(self):
        store = BrowserDeviceStore()
        device = SimpleNamespace(user_id="user_1", status="ACTIVE")
        active_session = SimpleNamespace(id="brs_existing", closed_at=None, state="AGENT_CONTROL")
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_existing",
            owner="agent",
            epoch=4,
        )
        db = AsyncMock()
        db.get.side_effect = [device, active_session]
        db.scalars.return_value = SimpleNamespace(first=lambda: lease)
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False

        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            self.assertRaises(BrowserTabInUseError),
        ):
            await store.open_session(user_id="user_1", device_id="bdv_1", tab_id=7)

        self.assertEqual(lease.session_id, "brs_existing")
        self.assertEqual(lease.owner, "agent")
        self.assertEqual(lease.epoch, 4)
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_open_session_rejects_inflight_bootstrap_with_owner_none(self):
        store = BrowserDeviceStore()
        device = SimpleNamespace(user_id="user_1", status="ACTIVE")
        bootstrap_session = SimpleNamespace(
            id="brs_connecting",
            closed_at=None,
            state="CONNECTING",
        )
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_connecting",
            owner="none",
            epoch=4,
        )
        db = AsyncMock()
        db.add = MagicMock()
        db.get.side_effect = [device, bootstrap_session]
        db.scalars.return_value = SimpleNamespace(first=lambda: lease)
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False

        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            self.assertRaises(BrowserTabInUseError),
        ):
            await store.open_session(user_id="user_1", device_id="bdv_1", tab_id=7)

        self.assertIsNone(bootstrap_session.closed_at)
        self.assertEqual(bootstrap_session.state, "CONNECTING")
        self.assertEqual(lease.session_id, "brs_connecting")
        self.assertEqual(lease.owner, "none")
        self.assertEqual(lease.epoch, 4)
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_open_session_retires_released_observing_session_before_reusing_tab(self):
        store = BrowserDeviceStore()
        device = SimpleNamespace(user_id="user_1", status="ACTIVE")
        released_session = SimpleNamespace(id="brs_old", closed_at=None, state="OBSERVING", updated_at=1)
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_old",
            owner="none",
            epoch=4,
            expires_at=123,
            updated_at=1,
        )
        new_session = SimpleNamespace(id="brs_new")
        db = AsyncMock()
        db.add = MagicMock()
        db.get.side_effect = [device, released_session]
        db.scalars.return_value = SimpleNamespace(first=lambda: lease)
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False

        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices.BrowserSession", return_value=new_session),
            patch("cptr.services.browser_devices._now_ms", return_value=999),
        ):
            result = await store.open_session(user_id="user_1", device_id="bdv_1", tab_id=7)

        self.assertIs(result, new_session)
        self.assertEqual(released_session.state, "DISCONNECTED")
        self.assertEqual(released_session.closed_at, 999)
        self.assertEqual(lease.session_id, "brs_new")
        self.assertEqual(lease.owner, "none")
        self.assertEqual(lease.epoch, 5)
        self.assertIsNone(lease.expires_at)
        db.commit.assert_awaited_once()

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

    async def test_transfer_to_none_closes_session_and_marks_it_disconnected(self):
        store = BrowserDeviceStore()
        session = SimpleNamespace(id="brs_1", closed_at=None, snapshot_id="snap_old", state="AGENT_CONTROL", updated_at=1)
        lease = SimpleNamespace(
            device_id="bdv_1",
            tab_id=7,
            session_id="brs_1",
            owner="agent",
            epoch=4,
            expires_at=456,
            updated_at=1,
        )
        db = AsyncMock()
        db.get.return_value = session
        db.scalars.return_value = SimpleNamespace(first=lambda: lease)
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False

        with (
            patch("cptr.services.browser_devices.get_db", new=AsyncMock(return_value=db)),
            patch("cptr.services.browser_devices._now_ms", return_value=999),
        ):
            result = await store.transfer_lease(
                session_id="brs_1",
                expected_epoch=4,
                expected_owner="agent",
                new_owner="none",
            )

        self.assertEqual(result["owner"], "none")
        self.assertEqual(result["epoch"], 5)
        self.assertEqual(result["state"], "DISCONNECTED")
        self.assertEqual(session.state, "DISCONNECTED")
        self.assertEqual(session.closed_at, 999)
        self.assertIsNone(lease.expires_at)

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
