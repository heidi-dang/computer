import unittest

from realtime_smoke import classify_assistant_messages, decode_socket_event, split_engine_packets


class RealtimeSmokeHelpersTests(unittest.TestCase):
    def test_engine_packet_and_socket_event_decoding(self):
        body = '0{"sid":"abc"}\x1e42["events:chat",{"message_id":"m1"}]'
        self.assertEqual(
            split_engine_packets(body), ['0{"sid":"abc"}', '42["events:chat",{"message_id":"m1"}]']
        )
        self.assertEqual(
            decode_socket_event('42["events:chat",{"message_id":"m1"}]'),
            ("events:chat", {"message_id": "m1"}),
        )

    def test_assistant_success_and_provider_error_are_separate(self):
        result = classify_assistant_messages(
            [
                {"role": "assistant", "content": "A useful answer", "meta": None},
                {
                    "role": "assistant",
                    "content": "> **Error:** upstream unavailable",
                    "meta": {"error": "upstream unavailable"},
                },
                {"role": "assistant", "content": "", "meta": None},
            ]
        )
        self.assertEqual(result["success_text"], ["A useful answer"])
        self.assertEqual(result["provider_error_text"], ["> **Error:** upstream unavailable"])


if __name__ == "__main__":
    unittest.main()
