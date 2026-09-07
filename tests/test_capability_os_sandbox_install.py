import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy/capability-os/cptr-sandbox-broker.service"
SOCKET = ROOT / "deploy/capability-os/cptr-sandbox-broker.socket"
SLICE = ROOT / "deploy/capability-os/cptr-sandbox.slice"
APPARMOR = ROOT / "deploy/capability-os/apparmor-cptr-runsc"


class CapabilityOsSandboxInstallTests(unittest.TestCase):
    def test_socket_contract_is_root_cptr_0660(self):
        text = SOCKET.read_text()
        self.assertIn("ListenStream=/run/cptr-sandbox-broker.sock", text)
        self.assertIn("SocketUser=root", text)
        self.assertIn("SocketGroup=cptr", text)
        self.assertIn("SocketMode=0660", text)
        self.assertIn("RemoveOnStop=true", text)

    def test_service_executes_only_root_installed_zipapp_and_is_hardened(self):
        text = SERVICE.read_text()
        self.assertIn("ExecStart=/usr/local/libexec/cptr-sandbox-broker.pyz", text)
        self.assertNotIn("/srv/cptr/repos", text)
        self.assertNotIn("backend-deploy/current", text)
        self.assertIn("EnvironmentFile=-/etc/cptr/capability-os-sandbox.conf", text)
        self.assertNotIn("CPTR_SANDBOX_PYTHON_ROOTFS=", text)
        for required in (
            "User=root", "Group=cptr", "NoNewPrivileges=true", "ProtectSystem=strict",
            "ProtectHome=true", "RestrictAddressFamilies=AF_UNIX",
            "ReadOnlyPaths=-/var/lib/cptr/data/capability-os/blobs",
            "ReadWritePaths=/run/cptr-sandbox", "UMask=0077",
            "RuntimeDirectory=cptr-sandbox", "RuntimeDirectoryMode=0710",
            "Slice=cptr-sandbox.slice",
            "Environment=CPTR_SANDBOX_SYSTEMD_SLICE=cptr-sandbox.slice",
            "Environment=CPTR_SANDBOX_RUNTIME_USER=cptr",
            "Environment=CPTR_SANDBOX_RUNTIME_GROUP=cptr",
            "ProtectControlGroups=true",
            "CapabilityBoundingSet=CAP_DAC_OVERRIDE",
            "AmbientCapabilities=",
        ):
            self.assertIn(required, text)
        self.assertNotIn("DelegateSubgroup=", text)
        self.assertNotIn("Delegate=memory pids", text)
        self.assertNotIn("Environment=CPTR_SANDBOX_STATE_ROOT=/var/lib/cptr/sandbox", text)
        self.assertNotIn("ReadWritePaths=/var/lib/cptr/sandbox", text)
        self.assertNotIn("ReadWritePaths=/sys/fs/cgroup", text)
        self.assertNotIn("CAP_SETUID", text)
        self.assertNotIn("CAP_SETGID", text)
        self.assertNotIn("CAP_SYS_ADMIN", text)

    def test_runsc_apparmor_profile_is_executable_specific_and_userns_capable(self):
        text = APPARMOR.read_text()
        self.assertIn("profile cptr-runsc /usr/bin/runsc flags=(unconfined)", text)
        self.assertIn("userns,", text)
        self.assertIn("@{exec_path} mr,", text)
        self.assertNotIn("/usr/bin/**", text)
        self.assertNotRegex(text, r"(?m)^\\s*capability,\\s*$")

    def test_sandbox_slice_enables_accounting_without_global_limits(self):
        text = SLICE.read_text()
        self.assertIn("[Slice]", text)
        for required in ("CPUWeight=100", "IOAccounting=yes", "MemoryAccounting=yes", "TasksAccounting=yes"):
            self.assertIn(required, text)
        self.assertNotIn("CPUAccounting=", text)
        self.assertNotIn("MemoryMax=", text)
        self.assertNotIn("TasksMax=", text)


if __name__ == "__main__":
    unittest.main()
