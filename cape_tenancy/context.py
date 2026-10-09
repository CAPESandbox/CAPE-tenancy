"""Per-request ContextVar storage for the active ViewerContext.

Allows TenancyMiddleware to bind the authenticated viewer once at the start of
an HTTP request so data-layer hooks (MongoDB, SQLAlchemy Tasking, Elasticsearch)
can automatically scope queries without threading `request` or `visible_to`
through CAPEv2 core views.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from cape_tenancy.policy import ViewerContext

_current_viewer: ContextVar[Optional[ViewerContext]] = ContextVar("cape_current_viewer", default=None)
_bypass_scoping: ContextVar[bool] = ContextVar("cape_bypass_scoping", default=False)


def get_current_viewer() -> Optional[ViewerContext]:
    """Return the active ViewerContext for the current execution context, or None."""
    if _bypass_scoping.get():
        return None
    return _current_viewer.get()


def set_current_viewer(viewer: Optional[ViewerContext]):
    """Bind `viewer` in the current context and return the reset token."""
    return _current_viewer.set(viewer)


def reset_current_viewer(token) -> None:
    """Restore `_current_viewer` to its state prior to `set_current_viewer`."""
    _current_viewer.reset(token)


@contextmanager
def viewer_scope_context(viewer: Optional[ViewerContext]) -> Iterator[Optional[ViewerContext]]:
    """Context manager that binds `viewer` for the duration of a `with` block."""
    token = _current_viewer.set(viewer)
    try:
        yield viewer
    finally:
        _current_viewer.reset(token)


@contextmanager
def unscoped_context() -> Iterator[None]:
    """Temporarily bypass automatic query scoping (e.g. for internal admin/system jobs)."""
    token = _bypass_scoping.set(True)
    try:
        yield
    finally:
        _bypass_scoping.reset(token)
