import unittest

from cptr.services.browser_visual_frames import BrowserVisualFrame, BrowserVisualFrameStore


def frame(*, session_id: str, frame_id: str, data: bytes) -> BrowserVisualFrame:
    return BrowserVisualFrame(
        device_id="bdv_1",
        session_id=session_id,
        frame_id=frame_id,
        mime_type="image/jpeg",
        width=640,
        height=480,
        created_at_ms=123,
        data=data,
    )


class BrowserVisualFrameStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_frame_is_isolated_per_session_on_same_device(self):
        store = BrowserVisualFrameStore()
        github = frame(session_id="brs_github", frame_id="frm_github", data=b"github")
        replit = frame(session_id="brs_replit", frame_id="frm_replit", data=b"replit")

        await store.put(github)
        await store.put(replit)

        self.assertEqual(
            await store.latest(device_id="bdv_1", session_id="brs_github"),
            github,
        )
        self.assertEqual(
            await store.latest(device_id="bdv_1", session_id="brs_replit"),
            replit,
        )
        self.assertEqual(
            await store.wait_next(
                device_id="bdv_1",
                session_id="brs_github",
                after_frame_id=None,
                timeout_seconds=0.1,
            ),
            github,
        )

    async def test_clearing_one_session_preserves_other_session_frame(self):
        store = BrowserVisualFrameStore()
        await store.put(frame(session_id="brs_one", frame_id="frm_1", data=b"one"))
        second = frame(session_id="brs_two", frame_id="frm_2", data=b"two")
        await store.put(second)

        await store.clear(device_id="bdv_1", session_id="brs_one")

        self.assertIsNone(await store.latest(device_id="bdv_1", session_id="brs_one"))
        self.assertEqual(await store.latest(device_id="bdv_1", session_id="brs_two"), second)


if __name__ == "__main__":
    unittest.main()
