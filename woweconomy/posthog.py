"""Shared PostHog client for long-running FarmAnalysis pipeline commands."""

import atexit
import os
from pathlib import Path


def _load_environment() -> None:
    """Load the repository environment without requiring python-dotenv."""
    env_file = Path(__file__).resolve().parents[1] / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except OSError:
        pass


def _is_debug_environment() -> bool:
    return (
        os.getenv("POSTHOG_DEBUG", "").lower() == "true"
        or os.getenv("DEBUG", "").lower() == "true"
        or os.getenv("APP_ENV", "").lower() in {"development", "dev", "debug"}
    )


def _missing_configuration_error(variable: str) -> RuntimeError:
    return RuntimeError(
        f"{variable} variable required by PostHog is missing or un-configured, "
        f"this causes events to be silently missed. This error stops appearing "
        f"once {variable} is configured"
    )


def initialize_posthog():
    """Create the process-wide PostHog client, or no-op when unconfigured."""
    _load_environment()
    project_token = os.getenv("POSTHOG_PROJECT_TOKEN")
    host = os.getenv("POSTHOG_HOST")

    if not project_token:
        if _is_debug_environment():
            raise _missing_configuration_error("POSTHOG_PROJECT_TOKEN")
        return None
    if not host:
        if _is_debug_environment():
            raise _missing_configuration_error("POSTHOG_HOST")
        return None

    from posthog import Posthog

    client = Posthog(
        project_token,
        host=host,
        enable_exception_autocapture=True,
    )
    atexit.register(client.shutdown)
    return client


posthog_client = initialize_posthog()
