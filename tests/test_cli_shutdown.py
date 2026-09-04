from click.testing import CliRunner

import cptr.cli as cli_module


def test_run_forwards_bounded_graceful_shutdown_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_uvicorn_run(application: str, **kwargs: object) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_uvicorn_run)

    result = CliRunner().invoke(cli_module.cli, ["run", "--headless"])

    assert result.exit_code == 0
    assert captured["application"] == "cptr.app:application"
    assert captured["timeout_graceful_shutdown"] == 7
