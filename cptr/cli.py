import click
import uvicorn

from cptr.env import SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS


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
@click.option(
    "--open-browser",
    is_flag=True,
    default=False,
    help="Open the CPTR dashboard in a browser after startup.",
)
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help="Compatibility override: never open the dashboard browser.",
)
def run(host: str, port: int, reload: bool, open_browser: bool, headless: bool):
    """Start the cptr server."""
    import os
    import secrets

    display_host = "localhost" if host == "0.0.0.0" else host

    token = secrets.token_hex(32)
    os.environ["CPTR_STARTUP_TOKEN"] = token
    os.environ["CPTR_PORT"] = str(port)
    url = f"http://{display_host}:{port}/?token={token}"

    print(f"\n  ➜  {url}\n")
    if should_open_dashboard(open_browser=open_browser, headless=headless):
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "cptr.app:application",
        host=host,
        port=port,
        reload=reload,
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )


def should_open_dashboard(*, open_browser: bool, headless: bool) -> bool:
    """Only a deliberate interactive invocation may launch a dashboard tab."""
    return open_browser and not headless


def main():
    cli()


if __name__ == "__main__":
    main()
