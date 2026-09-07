import unittest
from unittest.mock import patch

from cptr.services.capability_os.runtime import (
    RuntimeBroker,
    RuntimeClass,
    RuntimeInventory,
    RuntimeUnavailable,
)


class CapabilityOsRuntimeTests(unittest.TestCase):
    def test_inventory_detects_profiles_without_treating_bubblewrap_as_production_isolation(self):
        mapping = {
            "runsc": None,
            "wasmtime": None,
            "firecracker": None,
            "bwrap": "/usr/bin/bwrap",
        }
        with patch("shutil.which", side_effect=lambda name: mapping.get(name)):
            inventory = RuntimeInventory.detect()
        self.assertFalse(inventory.available(RuntimeClass.GVISOR))
        self.assertFalse(inventory.available(RuntimeClass.WASM))
        self.assertFalse(inventory.available(RuntimeClass.MICROVM))
        self.assertTrue(inventory.namespace_dev_available)

    def test_production_generated_native_execution_fails_closed_without_gvisor_or_microvm(self):
        inventory = RuntimeInventory(
            runsc=None,
            wasmtime=None,
            firecracker=None,
            bubblewrap="/usr/bin/bwrap",
        )
        broker = RuntimeBroker(inventory=inventory, production=True)
        with self.assertRaises(RuntimeUnavailable):
            broker.require(RuntimeClass.GVISOR)
        with self.assertRaises(RuntimeUnavailable):
            broker.require(RuntimeClass.MICROVM)
        with self.assertRaises(RuntimeUnavailable):
            broker.require(RuntimeClass.NAMESPACE_DEV)

    def test_development_namespace_profile_must_be_explicit(self):
        inventory = RuntimeInventory(
            runsc=None,
            wasmtime=None,
            firecracker=None,
            bubblewrap="/usr/bin/bwrap",
        )
        broker = RuntimeBroker(inventory=inventory, production=False)
        selected = broker.require(RuntimeClass.NAMESPACE_DEV)
        self.assertEqual(selected.executable, "/usr/bin/bwrap")
        self.assertEqual(selected.runtime_class, RuntimeClass.NAMESPACE_DEV)


if __name__ == "__main__":
    unittest.main()
