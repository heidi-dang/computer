"""Vercel ASGI entrypoint for the cptr backend.

Vercel functions are short-lived, so the desktop app's background scheduler,
bot manager, and model warm-up lifecycle must not run during a request.
"""

from contextlib import asynccontextmanager

from cptr.app import app as cptr_app


@asynccontextmanager
async def serverless_lifespan(_app):
    yield


cptr_app.router.lifespan_context = serverless_lifespan
app = cptr_app
