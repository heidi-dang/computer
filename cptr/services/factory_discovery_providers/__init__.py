"""Metadata-only external discovery providers for the Dark Factory."""

from cptr.services.factory_discovery_providers.github import GitHubDiscoveryProvider
from cptr.services.factory_discovery_providers.mcp_registry import McpRegistryDiscoveryProvider
from cptr.services.factory_discovery_providers.npm_registry import NpmRegistryDiscoveryProvider
from cptr.services.factory_discovery_providers.official_docs import OfficialDocsDiscoveryProvider
from cptr.services.factory_discovery_providers.pypi import PyPIRegistryDiscoveryProvider

__all__ = [
    "GitHubDiscoveryProvider",
    "McpRegistryDiscoveryProvider",
    "NpmRegistryDiscoveryProvider",
    "OfficialDocsDiscoveryProvider",
    "PyPIRegistryDiscoveryProvider",
]
