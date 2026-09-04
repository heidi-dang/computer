import subprocess
import sys
import textwrap


def test_complete_logging_releases_queued_handler_resources_and_can_reconfigure() -> None:
    script = textwrap.dedent(
        r"""
        import asyncio
        from pathlib import Path

        import cptr.utils.logger as logging_module

        def semaphore_mapping_count():
            maps = Path("/proc/self/maps")
            if not maps.exists():
                return None
            return sum("/dev/shm/sem." in line for line in maps.read_text().splitlines())

        logging_module.setup_logging()
        first_setup = semaphore_mapping_count()
        asyncio.run(logging_module.complete_logging())
        first_shutdown = semaphore_mapping_count()
        assert logging_module._configured is False

        logging_module.setup_logging()
        second_setup = semaphore_mapping_count()
        asyncio.run(logging_module.complete_logging())
        second_shutdown = semaphore_mapping_count()
        assert logging_module._configured is False

        if first_setup is not None:
            assert first_setup > 0
            assert first_shutdown == 0
            assert second_setup > 0
            assert second_shutdown == 0
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "leaked semaphore objects" not in completed.stderr
