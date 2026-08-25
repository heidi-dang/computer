"""Bounded generated-application authentication adapters.

This module deliberately does not replace CPTR authentication. Its public
service is only reachable through the authenticated FlowDeck workspace gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class GeneratedAuthError(Exception):
    """Expected, user-safe generated-auth failure."""

    def __init__(self, message: str, *, code: str = "auth_error", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


class AuthProvider(StrEnum):
    LOCAL = "local"
    AUTHJS = "authjs"
    CLERK = "clerk"
    SUPABASE = "supabase"
    FIREBASE = "firebase"
    OAUTH_OIDC = "oauth_oidc"
    NATIVE = "native"


EXTERNAL_PROVIDERS = {
    AuthProvider.AUTHJS,
    AuthProvider.CLERK,
    AuthProvider.SUPABASE,
    AuthProvider.FIREBASE,
    AuthProvider.OAUTH_OIDC,
}
SESSION_COOKIE = "cptr_generated_session"
CSRF_COOKIE = "cptr_generated_csrf"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
CSRF_TTL_SECONDS = 60 * 60
MAX_SESSION_AGE = 60 * 60 * 24 * 30


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    role: str
    provider: str


@dataclass(frozen=True)
class AuthSession:
    user: AuthUser
    expires_at: int
    csrf_token: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        candidate = _password_hash(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(candidate, f"scrypt${salt_hex}${digest_hex}")
    except (ValueError, TypeError):
        return False


def _read_markers(workspace: Path) -> tuple[AuthProvider, dict[str, Any]]:
    """Detect auth from server-read project files without trusting the client."""
    package_path = workspace / "package.json"
    package: dict[str, Any] = {}
    if package_path.is_file():
        try:
            parsed = json.loads(package_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                package = parsed
        except (OSError, ValueError):
            raise GeneratedAuthError("project auth configuration is unreadable", code="invalid_project")
    dependencies = {
        str(key).lower()
        for section in ("dependencies", "devDependencies", "peerDependencies")
        for key in (package.get(section, {}) if isinstance(package.get(section, {}), dict) else {})
    }
    candidates: set[AuthProvider] = set()
    if "next-auth" in dependencies or "@auth/core" in dependencies:
        candidates.add(AuthProvider.AUTHJS)
    if "@clerk/nextjs" in dependencies or "@clerk/clerk-sdk-node" in dependencies:
        candidates.add(AuthProvider.CLERK)
    if "@supabase/supabase-js" in dependencies or "@supabase/ssr" in dependencies:
        candidates.add(AuthProvider.SUPABASE)
    if "firebase" in dependencies or "firebase-admin" in dependencies:
        candidates.add(AuthProvider.FIREBASE)
    if any((workspace / name).exists() for name in (".authrc", "auth.config.ts", "auth.config.js")):
        candidates.add(AuthProvider.OAUTH_OIDC)
    config_path = workspace / ".cptr" / "generated-auth.json"
    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError
            config = parsed
            configured = parsed.get("provider")
            if configured:
                try:
                    candidates.add(AuthProvider(str(configured)))
                except ValueError as exc:
                    raise GeneratedAuthError("unknown generated-auth provider", code="unknown_provider") from exc
        except (OSError, ValueError) as exc:
            raise GeneratedAuthError("generated-auth configuration is invalid", code="invalid_project") from exc
    if len(candidates) > 1:
        raise GeneratedAuthError("multiple authentication providers detected; refusing replacement", code="ambiguous_provider")
    provider = next(iter(candidates), AuthProvider.LOCAL)
    if provider in EXTERNAL_PROVIDERS:
        verifier = config.get("verifier", {})
        if not isinstance(verifier, dict):
            verifier = {}
        config = {
            "issuer": verifier.get("issuer"),
            "audience": verifier.get("audience"),
            "jwks_url": verifier.get("jwks_url"),
            "redirect_uri": verifier.get("redirect_uri"),
            "verified": bool(
                verifier.get("issuer")
                and verifier.get("audience")
                and verifier.get("jwks_url")
                and verifier.get("redirect_uri")
            ),
        }
    return provider, config


class GeneratedAuthService:
    """Workspace-bound generated-auth service with local/native support."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise GeneratedAuthError("workspace does not exist", code="workspace_not_found", status=404)
        self.provider, self.provider_config = _read_markers(self.workspace)
        self.db_path = self.workspace / ".cptr" / "generated-auth.sqlite3"
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

    def metadata(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "supported": True,
            "verified": self.provider not in EXTERNAL_PROVIDERS or bool(self.provider_config.get("verified")),
            "preserved_existing_auth": self.provider != AuthProvider.LOCAL,
            "capabilities": {
                "signup": self.provider in {AuthProvider.LOCAL, AuthProvider.NATIVE},
                "external_callback": self.provider in EXTERNAL_PROVIDERS and bool(self.provider_config.get("verified")),
            },
        }

    def issue_csrf(self) -> str:
        return secrets.token_urlsafe(24)

    def _ensure_local(self) -> None:
        if self.provider in EXTERNAL_PROVIDERS:
            raise GeneratedAuthError(
                "external provider requires server-owned verifier configuration",
                code="provider_unverified",
                status=503,
            )

    def signup(self, email: str, password: str) -> AuthUser:
        self._ensure_local()
        normalized = email.strip().lower()
        if "@" not in normalized or len(password) < 8:
            raise GeneratedAuthError("a valid email and password of at least 8 characters are required", code="invalid_credentials")
        user = AuthUser(secrets.token_urlsafe(18), normalized, "user", self.provider.value)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                    (user.id, user.email, _password_hash(password), user.role, int(time.time())),
                )
        except sqlite3.IntegrityError as exc:
            raise GeneratedAuthError("an account with that email already exists", code="account_exists", status=409) from exc
        return user

    def signin(self, email: str, password: str) -> tuple[AuthUser, str, str, int]:
        self._ensure_local()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row is None or not _password_matches(password, row["password_hash"]):
            raise GeneratedAuthError("invalid email or password", code="invalid_credentials", status=401)
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (_hash_token(session_token), row["id"], _hash_token(csrf_token), expires_at, int(time.time())),
            )
        return AuthUser(row["id"], row["email"], row["role"], self.provider.value), session_token, csrf_token, expires_at

    def session(self, token: str, csrf_token: str | None = None) -> AuthSession:
        if not token or len(token) > 256:
            raise GeneratedAuthError("authentication required", code="unauthenticated", status=401)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sessions.*, users.email, users.role FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=?",
                (_hash_token(token),),
            ).fetchone()
            if row is None:
                raise GeneratedAuthError("authentication required", code="unauthenticated", status=401)
            if row["expires_at"] <= int(time.time()):
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
                connection.commit()
                raise GeneratedAuthError("session expired", code="session_expired", status=401)
            if csrf_token and not hmac.compare_digest(row["csrf_hash"], _hash_token(csrf_token)):
                raise GeneratedAuthError("CSRF validation failed", code="csrf_failed", status=403)
        return AuthSession(
            AuthUser(row["user_id"], row["email"], row["role"], self.provider.value),
            min(row["expires_at"], int(time.time()) + MAX_SESSION_AGE),
            csrf_token or "",
        )

    def signout(self, token: str, csrf_token: str) -> None:
        self.session(token, csrf_token)
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))

    def require_role(self, session: AuthSession, role: str) -> AuthUser:
        if session.user.role != role and session.user.role != "admin":
            raise GeneratedAuthError("forbidden", code="forbidden", status=403)
        return session.user

    def verify_external_callback(
        self,
        *,
        issuer: str,
        audience: str,
        redirect_uri: str,
        state: str,
        expected_state: str,
        nonce: str,
        expected_nonce: str,
        code_verifier: str,
    ) -> None:
        if self.provider not in EXTERNAL_PROVIDERS or not self.provider_config.get("verified"):
            raise GeneratedAuthError("external provider is not verified", code="provider_unverified", status=503)
        if issuer != self.provider_config["issuer"] or audience != self.provider_config["audience"]:
            raise GeneratedAuthError("provider callback identity mismatch", code="callback_denied", status=403)
        if (
            not hmac.compare_digest(state, expected_state)
            or not hmac.compare_digest(nonce, expected_nonce)
            or not code_verifier
            or redirect_uri != self.provider_config["redirect_uri"]
        ):
            raise GeneratedAuthError("unsafe provider callback", code="callback_denied", status=403)