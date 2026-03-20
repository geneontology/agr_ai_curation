"""GitHub OAuth2 authentication provider with GO users.yaml authorization."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import jwt
import yaml

from src.auth.base import AuthPrincipal, AuthProvider, TokenSet


logger = logging.getLogger(__name__)

# GitHub OAuth endpoints
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

DEFAULT_USERS_YAML_URL = (
    "https://raw.githubusercontent.com/geneontology/go-site/master/metadata/users.yaml"
)
DEFAULT_CACHE_TTL_SECONDS = 3600  # 1 hour


class GitHubAuthProvider(AuthProvider):
    """GitHub OAuth2 provider with GO users.yaml authorization."""

    def __init__(self, config: Dict[str, Any]):
        self.client_id: str = config["client_id"]
        self.client_secret: str = config["client_secret"]
        self.redirect_uri: str = config.get(
            "redirect_uri", "http://localhost:3002/auth/callback"
        )
        self.jwt_secret: str = config["jwt_secret"]
        self.users_yaml_url: str = config.get("users_yaml_url") or DEFAULT_USERS_YAML_URL
        self.cache_ttl: int = int(config.get("cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS))
        self.timeout_seconds: int = int(config.get("timeout_seconds", 10))

        self._allowed_users: Optional[set[str]] = None
        self._cache_time: float = 0.0
        self._cache_lock = threading.Lock()

        # Eagerly load the allowed-users cache at init
        self._refresh_allowed_users()

    # ------------------------------------------------------------------
    # users.yaml caching & authorization
    # ------------------------------------------------------------------

    def _refresh_allowed_users(self) -> None:
        """Fetch users.yaml and rebuild the allowed-username set."""
        try:
            resp = httpx.get(self.users_yaml_url, timeout=self.timeout_seconds, follow_redirects=True)
            resp.raise_for_status()
            users = yaml.safe_load(resp.text)
            if not isinstance(users, list):
                logger.error("users.yaml is not a list — denying all")
                return

            allowed: set[str] = set()
            for user in users:
                if not isinstance(user, dict):
                    continue
                # Check authorization: noctua.go.allow-edit must be true
                auths = user.get("authorizations", {})
                noctua = auths.get("noctua", {}) if isinstance(auths, dict) else {}
                go_auth = noctua.get("go", {}) if isinstance(noctua, dict) else {}
                allow_edit = go_auth.get("allow-edit", False) if isinstance(go_auth, dict) else False
                if not allow_edit:
                    continue
                # Extract GitHub username from accounts
                accounts = user.get("accounts", {})
                if not isinstance(accounts, dict):
                    continue
                github_username = accounts.get("github")
                if github_username and isinstance(github_username, str):
                    allowed.add(github_username.lower())

            self._allowed_users = allowed
            self._cache_time = time.monotonic()
            logger.info(
                "Loaded %d authorized GitHub users from users.yaml", len(allowed)
            )
        except Exception:
            logger.exception("Failed to fetch users.yaml")
            if self._allowed_users is not None:
                logger.warning("Using stale users.yaml cache (%d users)", len(self._allowed_users))
            # If no cache exists at all, _allowed_users stays None → fail closed

    def _get_allowed_users(self) -> set[str]:
        """Return cached allowed-users set, refreshing if TTL expired."""
        elapsed = time.monotonic() - self._cache_time
        if self._allowed_users is None or elapsed > self.cache_ttl:
            with self._cache_lock:
                # Double-check after acquiring lock
                elapsed = time.monotonic() - self._cache_time
                if self._allowed_users is None or elapsed > self.cache_ttl:
                    self._refresh_allowed_users()

        if self._allowed_users is None:
            # Fail closed: no cache available at all
            return set()
        return self._allowed_users

    def _is_authorized(self, github_username: str) -> bool:
        """Check if a GitHub username is authorized via users.yaml."""
        allowed = self._get_allowed_users()
        return github_username.lower() in allowed

    # ------------------------------------------------------------------
    # AuthProvider interface
    # ------------------------------------------------------------------

    def get_login_url(
        self,
        state: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
    ) -> str:
        """Build GitHub authorize URL (PKCE params ignored by GitHub)."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str, code_verifier: str) -> TokenSet:
        """Exchange code for GitHub access token, fetch user, authorize, issue JWT."""
        # Exchange authorization code for access token
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            token_resp = await client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            error = token_data.get("error_description", token_data.get("error", "unknown"))
            raise ValueError(f"GitHub token exchange failed: {error}")

        # Fetch GitHub user profile
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            user_resp = await client.get(
                GITHUB_USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        user_resp.raise_for_status()
        user_data = user_resp.json()

        github_username = user_data.get("login", "")
        if not github_username:
            raise ValueError("GitHub user response missing login")

        # Authorization check against users.yaml
        if not self._is_authorized(github_username):
            logger.warning(
                "GitHub user '%s' authenticated but not authorized in users.yaml",
                github_username,
            )
            raise PermissionError(
                f"GitHub user '{github_username}' is not authorized. "
                "Access is restricted to users listed in the GO users.yaml "
                "with noctua.go.allow-edit permission."
            )

        # Self-sign a JWT as the id_token
        now = int(time.time())
        claims = {
            "sub": github_username,
            "github_username": github_username,
            "email": user_data.get("email"),
            "name": user_data.get("name") or github_username,
            "iat": now,
            "exp": now + 86400,  # 24 hours
        }
        id_token = jwt.encode(claims, self.jwt_secret, algorithm="HS256")

        return TokenSet(
            id_token=id_token,
            access_token=access_token,
        )

    async def validate_token(self, token: str) -> Dict[str, Any]:
        """Decode and verify the self-signed JWT."""
        try:
            decoded = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
            )
        except jwt.PyJWTError as exc:
            logger.error("GitHub JWT validation failed: %s", exc)
            raise
        return decoded

    def extract_principal(self, claims: Dict[str, Any]) -> AuthPrincipal:
        """Map JWT claims to AuthPrincipal."""
        return AuthPrincipal(
            subject=claims.get("sub", ""),
            email=claims.get("email"),
            display_name=claims.get("name"),
            groups=[],
            raw_claims=claims,
            provider=self.provider_name,
        )

    def get_logout_url(self, redirect_uri: Optional[str] = None) -> Optional[str]:
        """GitHub has no logout redirect — return app root."""
        return "/"

    @property
    def provider_name(self) -> str:
        return "github"
