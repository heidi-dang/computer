"""Web tools package: search and URL reading for the AI agent.

Exposes `web_search_handler` and `read_url_handler` consumed by tools.py.
"""

from cptr.utils.web.search import web_search_handler
from cptr.utils.web.reader import read_url_handler

__all__ = ["web_search_handler", "read_url_handler"]
