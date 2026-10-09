"""Django template context processor for CAPE-tenancy."""

from typing import Any, Dict

from cape_tenancy.middleware import default_viewer_resolver
from cape_tenancy.policy import VISIBILITIES, multitenancy_config


def tenancy_context(request: Any) -> Dict[str, Any]:
    """Provide `tenancy_enabled`, `viewer`, and `visibility_choices` to WebGUI templates."""
    cfg = multitenancy_config()
    viewer = getattr(request, "viewer", None)
    if viewer is None:
        viewer = default_viewer_resolver(getattr(request, "user", None))
    return {
        "tenancy_enabled": bool(cfg.enabled),
        "tenancy_viewer": viewer,
        "visibility_choices": list(VISIBILITIES),
    }
