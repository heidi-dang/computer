"""Typed FlowDeck contract errors."""


class FlowDeckError(Exception):
    """Base class for deterministic FlowDeck contract failures."""


class RegistryError(FlowDeckError):
    """The canonical agent registry is invalid."""


class UnknownAgentError(FlowDeckError):
    """A delegation or lookup referenced an unknown agent."""


class DelegationPolicyError(FlowDeckError):
    """A delegation violates the depth or role policy."""