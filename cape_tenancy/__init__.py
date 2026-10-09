"""CAPE-tenancy: Out-of-tree Multi-Tenancy and Central Mode plugin for CAPEv2.

Usage in CAPEv2 `web/local_settings.py`:
    import cape_tenancy
    cape_tenancy.install(globals())
"""

from __future__ import annotations

from typing import Any, MutableMapping


from cape_tenancy.acl import (
    TaskAcl,
    VisibilityConflictError,
    VisibilityStamp,
    ensure_acl_schema,
    get_task_acl,
    resolve_task_scope,
    set_task_acl,
    sync_stamp_to_mongo,
    toggle_task_visibility_cas,
)
from cape_tenancy.context import (
    get_current_viewer,
    reset_current_viewer,
    set_current_viewer,
    unscoped_context,
    viewer_scope_context,
)
from cape_tenancy.hooks import (
    apply_mongo_filters,
    default_es_query_filter,
    default_mongo_analysis_filter,
    default_sql_task_acl_clause,
    register_es_filter,
    register_mongo_filter,
    register_sql_filter,
    wrap_mongodb_module,
)
from cape_tenancy.middleware import TenancyMiddleware, classify_view_action, default_viewer_resolver, tenancy_exempt
from cape_tenancy.policy import (
    PRIVATE,
    PUBLIC,
    TENANT,
    VISIBILITIES,
    MultitenancyConfig,
    ResourceScope,
    ViewerContext,
    can_delete,
    can_read,
    can_set_visibility,
    can_toggle,
    multitenancy_config,
    scope_match,
    viewer_scope_es_filter,
    viewer_scope_match,
)

__version__ = "0.1.0"

APP_NAME = "cape_tenancy.apps.CapeTenancyConfig"
MIDDLEWARE_PATH = "cape_tenancy.middleware.TenancyMiddleware"
CONTEXT_PROCESSOR_PATH = "cape_tenancy.context_processors.tenancy_context"


def install(settings_globals: MutableMapping[str, Any], *, wrap_mongo: bool = True) -> None:
    """Mount `cape_tenancy` into CAPEv2's Django `settings.py` / `local_settings.py` namespace.

    1. Appends `cape_tenancy.apps.CapeTenancyConfig` to `INSTALLED_APPS`.
    2. Appends `cape_tenancy.middleware.TenancyMiddleware` to `MIDDLEWARE` (after AuthenticationMiddleware).
    3. Appends `cape_tenancy.context_processors.tenancy_context` to `TEMPLATES[*]['OPTIONS']['context_processors']`.
    4. Registers the default MongoDB query filter hook (and wraps `dev_utils.mongodb` if imported/available).
    """
    installed_apps = list(settings_globals.get("INSTALLED_APPS", []))
    if APP_NAME not in installed_apps and "cape_tenancy" not in installed_apps:
        installed_apps.append(APP_NAME)
        settings_globals["INSTALLED_APPS"] = installed_apps

    middleware = list(settings_globals.get("MIDDLEWARE", []))
    if MIDDLEWARE_PATH not in middleware:
        middleware.append(MIDDLEWARE_PATH)
        settings_globals["MIDDLEWARE"] = middleware

    templates = settings_globals.get("TEMPLATES")
    if isinstance(templates, list):
        for tmpl in templates:
            opts = tmpl.setdefault("OPTIONS", {})
            processors = list(opts.get("context_processors", []))
            if CONTEXT_PROCESSOR_PATH not in processors:
                processors.append(CONTEXT_PROCESSOR_PATH)
                opts["context_processors"] = processors

    register_mongo_filter(default_mongo_analysis_filter)

    if wrap_mongo:
        try:
            import dev_utils.mongodb as mongodb_mod

            wrap_mongodb_module(mongodb_mod)
        except ImportError:
            pass


__all__ = [
    "__version__",
    "install",
    "PRIVATE",
    "PUBLIC",
    "TENANT",
    "VISIBILITIES",
    "MultitenancyConfig",
    "ViewerContext",
    "ResourceScope",
    "can_read",
    "can_toggle",
    "can_delete",
    "can_set_visibility",
    "scope_match",
    "viewer_scope_match",
    "viewer_scope_es_filter",
    "multitenancy_config",
    "TaskAcl",
    "VisibilityStamp",
    "VisibilityConflictError",
    "ensure_acl_schema",
    "get_task_acl",
    "resolve_task_scope",
    "set_task_acl",
    "toggle_task_visibility_cas",
    "sync_stamp_to_mongo",
    "get_current_viewer",
    "set_current_viewer",
    "reset_current_viewer",
    "viewer_scope_context",
    "unscoped_context",
    "TenancyMiddleware",
    "default_viewer_resolver",
    "classify_view_action",
    "tenancy_exempt",
    "register_mongo_filter",
    "register_sql_filter",
    "register_es_filter",
    "apply_mongo_filters",
    "default_mongo_analysis_filter",
    "default_es_query_filter",
    "default_sql_task_acl_clause",
    "wrap_mongodb_module",
]
