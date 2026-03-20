"""Authentication provider factory."""

import os

from src.auth.base import AuthProvider
from src.auth.providers.cognito_config import create_cognito_provider
from src.auth.providers.dev import DevAuthProvider
from src.auth.providers.oidc import OIDCAuthProvider
from src.config import (
    get_auth_provider,
    is_cognito_configured,
    is_dev_mode,
)
from src.lib.config.groups_loader import get_group_claim_key


def create_auth_provider() -> AuthProvider:
    """Create auth provider for the current environment."""
    if is_dev_mode():
        return DevAuthProvider()

    provider_type = get_auth_provider()

    if provider_type == "cognito":
        if not is_cognito_configured():
            raise ValueError("AUTH_PROVIDER=cognito but Cognito is not fully configured")
        return create_cognito_provider()

    if provider_type == "oidc":
        issuer = os.getenv("OIDC_ISSUER_URL")
        client_id = os.getenv("OIDC_CLIENT_ID")
        redirect_uri = os.getenv("OIDC_REDIRECT_URI")
        if not issuer or not client_id or not redirect_uri:
            raise ValueError(
                "AUTH_PROVIDER=oidc requires OIDC_ISSUER_URL, OIDC_CLIENT_ID, and OIDC_REDIRECT_URI"
            )

        return OIDCAuthProvider(
            {
                "issuer_url": issuer,
                "client_id": client_id,
                "client_secret": os.getenv("OIDC_CLIENT_SECRET"),
                "redirect_uri": redirect_uri,
                "group_claim": os.getenv("OIDC_GROUP_CLAIM", get_group_claim_key()),
                "scopes": os.getenv("OIDC_SCOPES", "openid profile email"),
                "logout_url": os.getenv("OIDC_LOGOUT_URL"),
                "logout_redirect_param": os.getenv(
                    "OIDC_LOGOUT_REDIRECT_PARAM", "post_logout_redirect_uri"
                ),
            }
        )

    if provider_type == "github":
        client_id = os.getenv("GITHUB_CLIENT_ID")
        client_secret = os.getenv("GITHUB_CLIENT_SECRET")
        jwt_secret = os.getenv("GITHUB_JWT_SECRET")
        if not client_id or not client_secret or not jwt_secret:
            raise ValueError(
                "AUTH_PROVIDER=github requires GITHUB_CLIENT_ID, "
                "GITHUB_CLIENT_SECRET, and GITHUB_JWT_SECRET"
            )
        from src.auth.providers.github import GitHubAuthProvider

        return GitHubAuthProvider(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": os.getenv(
                    "GITHUB_REDIRECT_URI", "http://localhost:3002/auth/callback"
                ),
                "jwt_secret": jwt_secret,
                "users_yaml_url": os.getenv(
                    "GITHUB_USERS_YAML_URL",
                    "https://raw.githubusercontent.com/geneontology/go-site/master/metadata/users.yaml",
                ),
            }
        )

    if provider_type == "dev":
        raise ValueError("AUTH_PROVIDER=dev requires DEV_MODE=true")

    raise ValueError(f"Unknown AUTH_PROVIDER: {provider_type}")
