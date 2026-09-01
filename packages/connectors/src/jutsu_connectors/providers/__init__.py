"""Live provider connectors: the same three read-only methods as `local`, over OAuth.

Each module implements the `Connector` protocol against one provider family's official
API. None of them touches the database or the credential store — a `TokenSource` is
injected by the worker, which owns decryption and refresh. That keeps this package
importable (and its tests runnable) with no secrets anywhere in scope.
"""

from jutsu_connectors.providers.atlassian import ConfluenceConnector, JiraConnector
from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderContext,
    ProviderHttp,
    TokenSource,
)
from jutsu_connectors.providers.github import GitHubConnector
from jutsu_connectors.providers.google import (
    GmailConnector,
    GoogleCalendarConnector,
    GoogleDriveConnector,
    GoogleMeetConnector,
)
from jutsu_connectors.providers.microsoft import (
    OneDriveConnector,
    SharePointConnector,
    TeamsConnector,
)
from jutsu_connectors.providers.slack import SlackConnector

__all__ = [
    "CONNECTOR_CLASSES",
    "ConfluenceConnector",
    "GitHubConnector",
    "GmailConnector",
    "GoogleCalendarConnector",
    "GoogleDriveConnector",
    "GoogleMeetConnector",
    "JiraConnector",
    "OneDriveConnector",
    "ProviderApiError",
    "ProviderAuthError",
    "ProviderContext",
    "ProviderHttp",
    "SharePointConnector",
    "SlackConnector",
    "TeamsConnector",
    "TokenSource",
]

#: One connector class per provider id, for the worker's registry. Providers sharing a
#: SourceSystem namespace still fetch differently — the provider id is the key.
CONNECTOR_CLASSES: "dict[str, type]" = {
    "gmail": GmailConnector,
    "google_drive": GoogleDriveConnector,
    "google_calendar": GoogleCalendarConnector,
    "google_meet": GoogleMeetConnector,
    "onedrive": OneDriveConnector,
    "teams": TeamsConnector,
    "sharepoint": SharePointConnector,
    "slack": SlackConnector,
    "github": GitHubConnector,
    "jira": JiraConnector,
    "confluence": ConfluenceConnector,
}
