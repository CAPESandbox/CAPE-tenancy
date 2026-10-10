"""Django `TenancyMiddleware` implementing automatic per-request scoping and `process_view` gating.

Eliminates the ~45 inline `if not can_view_task(request.user, task): return 404`
checks scattered across `web/analysis/views.py`, `web/apiv2/views.py`, and
`web/guac/views.py`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from cape_tenancy.context import reset_current_viewer, set_current_viewer
from cape_tenancy.policy import (
    ResourceScope,
    ViewerContext,
    can_delete,
    can_read,
    can_toggle,
    multitenancy_config,
)

# URL / view-name keywords that require higher privilege than read-only access
_DELETE_KEYWORDS = ("delete", "remove", "purge")
_MANAGE_KEYWORDS = ("reprocess", "reboot", "visibility", "toggle", "reschedule")
_TASK_ID_KWARGS = ("task_id", "analysis_id")
_SAMPLE_HASH_KWARGS = ("sha256", "sha1", "md5", "sample_id")


def default_viewer_resolver(user: Any) -> ViewerContext:
    """Resolve a `ViewerContext` from a Django `request.user` object."""
    cfg = multitenancy_config()
    if not cfg.enabled:
        return ViewerContext(
            user_id=getattr(user, "id", None),
            tenant_id=None,
            is_tenant_admin=False,
            is_local_admin=True,
            is_authenticated=True,
        )

    if user is None or not getattr(user, "is_authenticated", False):
        return ViewerContext.anonymous()

    is_super = bool(getattr(user, "is_superuser", False))
    is_local_admin = is_super and bool(cfg.allow_superuser_all)

    tenant_id = None
    is_tenant_admin = False
    try:
        profile = getattr(user, "userprofile", None)
        if profile is not None:
            tenant_id = getattr(profile, "tenant_id", None)
            is_tenant_admin = bool(getattr(profile, "is_tenant_admin", False))
    except Exception:
        # Fail-closed on profile resolution error
        tenant_id = None
        is_tenant_admin = False

    return ViewerContext(
        user_id=getattr(user, "id", None),
        tenant_id=tenant_id,
        is_tenant_admin=is_tenant_admin,
        is_local_admin=is_local_admin,
        is_authenticated=True,
    )


def classify_view_action(request_path: str, view_func: Callable[..., Any]) -> str:
    """Classify a view invocation as `'read'`, `'manage'`, or `'delete'`."""
    view_name = getattr(view_func, "__name__", "").lower()
    path_lower = (request_path or "").lower()
    combined = f"{view_name} {path_lower}"

    if any(kw in combined for kw in _DELETE_KEYWORDS):
        return "delete"
    if any(kw in combined for kw in _MANAGE_KEYWORDS):
        return "manage"
    return "read"


class TenancyMiddleware:
    """Django middleware that binds `ViewerContext` and gates task/sample routes in `process_view`."""

    def __init__(
        self,
        get_response: Callable[[Any], Any],
        *,
        viewer_resolver: Callable[[Any], ViewerContext] = default_viewer_resolver,
        task_scope_resolver: Optional[Callable[[int], Optional[ResourceScope]]] = None,
        sample_access_checker: Optional[Callable[[ViewerContext, Dict[str, Any]], bool]] = None,
        deny_response_factory: Optional[Callable[[Any, str], Any]] = None,
    ) -> None:
        self.get_response = get_response
        self.viewer_resolver = viewer_resolver
        self.task_scope_resolver = task_scope_resolver
        self.sample_access_checker = sample_access_checker
        self.deny_response_factory = deny_response_factory or self._default_deny_response

    def __call__(self, request: Any) -> Any:
        viewer = self.viewer_resolver(getattr(request, "user", None))
        request.viewer = viewer
        token = set_current_viewer(viewer)
        try:
            from cape_tenancy.urls import match_visibility_task_id

            vis_task_id = match_visibility_task_id(getattr(request, "path", "") or "")
            if vis_task_id is not None:
                from cape_tenancy.views import tasks_set_visibility

                return tasks_set_visibility(request, vis_task_id, viewer_resolver=self.viewer_resolver)
            return self.get_response(request)
        finally:
            reset_current_viewer(token)

    def process_view(
        self,
        request: Any,
        view_func: Callable[..., Any],
        view_args: Sequence[Any],
        view_kwargs: Dict[str, Any],
    ) -> Optional[Any]:
        """Automatically enforce task and sample access before the view runs."""
        if getattr(view_func, "tenancy_exempt", False):
            return None

        viewer: Optional[ViewerContext] = getattr(request, "viewer", None)
        if viewer is None:
            viewer = self.viewer_resolver(getattr(request, "user", None))
            request.viewer = viewer

        if not viewer.see_all:
            # 1. Check task_id / analysis_id routes
            for key in _TASK_ID_KWARGS:
                if key in view_kwargs and view_kwargs[key] is not None:
                    try:
                        task_id = int(view_kwargs[key])
                    except (TypeError, ValueError):
                        return self.deny_response_factory(request, "Task not found")

                    if self.task_scope_resolver is not None:
                        scope = self.task_scope_resolver(task_id)
                        if scope is None:
                            return self.deny_response_factory(request, "Task not found")
                        action = classify_view_action(getattr(request, "path", ""), view_func)
                        allowed = self._check_action(viewer, scope, action)
                        if not allowed:
                            return self.deny_response_factory(request, "Task not found")
                    break

            # 2. Check sample hash / sample_id routes
            sample_Filter = {k: view_kwargs[k] for k in _SAMPLE_HASH_KWARGS if view_kwargs.get(k) is not None}
            if sample_Filter and self.sample_access_checker is not None:
                if not self.sample_access_checker(viewer, sample_Filter):
                    return self.deny_response_factory(request, "Sample not found in database")

        from cape_tenancy.views import dispatch_central_view

        return dispatch_central_view(request, view_func, view_args, view_kwargs)


    @staticmethod
    def _check_action(viewer: ViewerContext, scope: ResourceScope, action: str) -> bool:
        if action == "delete":
            return can_delete(viewer, scope)
        if action == "manage":
            return can_toggle(viewer, scope)
        return can_read(viewer, scope)

    @staticmethod
    def _default_deny_response(request: Any, message: str) -> Any:
        path = getattr(request, "path", "") or ""
        is_api = path.startswith("/apiv2/") or path.startswith("/api/")
        try:
            from django.conf import settings
            from django.http import HttpResponseNotFound, JsonResponse

            if settings.configured:
                if is_api:
                    return JsonResponse({"error": True, "error_value": message}, status=404)
                return HttpResponseNotFound(message)
        except Exception:
            pass
        return {"status_code": 404, "error": True, "error_value": message}


def tenancy_exempt(view_func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to mark a view as exempt from automatic `TenancyMiddleware` gating."""
    view_func.tenancy_exempt = True  # type: ignore[attr-defined]
    return view_func
