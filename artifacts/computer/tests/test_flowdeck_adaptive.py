import unittest

from cptr.flowdeck.adaptive import adaptive_route


class AdaptiveRoutingTests(unittest.TestCase):
    def test_simple_request_uses_native_direct_path(self):
        route = adaptive_route("hello")
        self.assertEqual(route.path, "native_direct")
        self.assertEqual(route.specialist_ids, ())

    def test_complex_request_uses_existing_specialist_path(self):
        route = adaptive_route("edit the auth flow and run tests")
        self.assertEqual(route.path, "flowdeck_specialist")
        self.assertIn("reviewer", route.specialist_ids)

    def test_model_identity_does_not_trigger_switching(self):
        route = adaptive_route("hello", "configured-model")
        self.assertEqual(route.path, "native_direct")