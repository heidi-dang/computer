import click
import socket
import uvicorn


def _server_is_listening(host: str, port: int) -> bool:
    """Return whether a CPTR-compatible listener already owns the bind port."""
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.25):
            return True
    except OSError:
        return False


@click.group()
def cli():
    """Your computer, from anywhere."""
    pass


@cli.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind to. Use 0.0.0.0 to allow access from other devices.",
)
@click.option("--port", default=8000, type=int, help="Port to bind to.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload.")
@click.option("--headless", is_flag=True, default=False, help="Don't open browser (default).")
@click.option(
    "--open-browser", is_flag=True, default=False, help="Open the local CPTR UI in a browser."
)
def run(host: str, port: int, reload: bool, headless: bool, open_browser: bool):
    """Start the cptr server."""
    import os
    import secrets

    display_host = "localhost" if host == "0.0.0.0" else host

    token = secrets.token_hex(32)
    os.environ["CPTR_STARTUP_TOKEN"] = token
    os.environ["CPTR_PORT"] = str(port)
    url = f"http://{display_host}:{port}/?token={token}"

    print(f"\n  ➜  {url}\n")
    # A failed/retried start must not open another browser tab while the
    # existing server is still serving the same port.  Probe before scheduling
    # the delayed opener so a second process cannot create a tab merely because
    # its later bind will fail.
    already_running = _server_is_listening(host, port)
    if open_browser and not headless and not already_running:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "cptr.app:application",
        host=host,
        port=port,
        reload=reload,
    )


def main():
    cli()


if __name__ == "__main__":
    main()
