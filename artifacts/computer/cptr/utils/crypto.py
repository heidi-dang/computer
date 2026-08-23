"""Symmetric encryption for API keys using Fernet (AES-128-CBC).

Derives a stable Fernet key from the server's JWT secret so keys are
encrypted at rest but can be decrypted by any process with the same secret.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _derive_fernet_key(jwt_secret: str) -> bytes:
    """Derive a 32-byte URL-safe base64 Fernet key from the JWT secret."""
    digest = hashlib.sha256(jwt_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_key(plaintext: str, jwt_secret: str) -> str:
    """Encrypt an API key. Returns 'encrypted:<token>'."""
    f = Fernet(_derive_fernet_key(jwt_secret))
    token = f.encrypt(plaintext.encode()).decode()
    return f"encrypted:{token}"


def decrypt_key(stored: str, jwt_secret: str) -> str:
    """Decrypt an API key. Accepts 'encrypted:<token>' or plain text fallback."""
    if not stored.startswith("encrypted:"):
        return stored  # Not encrypted, return as-is
    token = stored[len("encrypted:") :]
    f = Fernet(_derive_fernet_key(jwt_secret))
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt API key. JWT secret may have changed")


def mask_key(key: str) -> str:
    """Mask an API key for display: 'sk-abc...xyz' → 'sk-ab...yz'."""
    if not key or len(key) < 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"
