"""Google integration connectors package."""

from src.integrations.connectors.google.connector import GoogleWorkspaceConnector
from src.integrations.connectors.google.oauth import GOOGLE_WORKSPACE_SCOPES, GoogleOAuthClient

__all__ = [
    "GOOGLE_WORKSPACE_SCOPES",
    "GoogleOAuthClient",
    "GoogleWorkspaceConnector",
]

