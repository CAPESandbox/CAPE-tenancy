"""Data-layer hooks for MongoDB, SQLAlchemy Tasking, Elasticsearch, and Artifact Storage.

When `cape_tenancy` is installed, these hooks automatically merge the active
request's `ViewerContext` filter into:
- MongoDB `"analysis"` queries (`mongo_find`, `mongo_find_one`, `mongo_aggregate`, `mongo_delete_data`)
- SQLAlchemy task list/count queries (`TaskAcl` join / filter)
- Elasticsearch search queries (`viewer_scope_es_filter`)
without requiring inline `scoped_analysis_query` or `visible_to=` calls in every view.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional

from cape_tenancy.acl import TaskAcl
from cape_tenancy.context import get_current_viewer
from cape_tenancy.policy import (
    PUBLIC,
    TENANT,
    ViewerContext,
    viewer_scope_es_filter,
    viewer_scope_match,
)
from sqlalchemy import or_

MongoFilterFn = Callable[[str, Dict[str, Any], Optional[ViewerContext]], Dict[str, Any]]
SqlFilterFn = Callable[[Any, Any, Optional[ViewerContext]], Any]
EsFilterFn = Callable[[Dict[str, Any], Optional[ViewerContext]], Dict[str, Any]]

_mongo_filters: List[MongoFilterFn] = []
_sql_filters: List[SqlFilterFn] = []
_es_filters: List[EsFilterFn] = []
_patched_modules: set[str] = set()


def default_mongo_analysis_filter(
    collection: str,
    query: Optional[Dict[str, Any]],
    viewer: Optional[ViewerContext] = None,
) -> Dict[str, Any]:
    """Automatically scope queries on the MongoDB `'analysis'` collection to `viewer`."""
    base_query = dict(query) if query else {}
    if collection != "analysis":
        return base_query

    active_viewer = viewer if viewer is not None else get_current_viewer()
    if active_viewer is None:
        return base_query

    scope_pred = viewer_scope_match(active_viewer)
    if scope_pred is None:
        return base_query

    if not base_query:
        return scope_pred
    return {"$and": [base_query, scope_pred]}


def default_es_query_filter(
    body: Dict[str, Any],
    viewer: Optional[ViewerContext] = None,
) -> Dict[str, Any]:
    """Automatically wrap an Elasticsearch query dict with `viewer_scope_es_filter`."""
    active_viewer = viewer if viewer is not None else get_current_viewer()
    if active_viewer is None:
        return body

    es_filter = viewer_scope_es_filter(active_viewer)
    if es_filter is None:
        return body

    existing_query = body.get("query", {"match_all": {}})
    scoped_body = dict(body)
    scoped_body["query"] = {
        "bool": {
            "must": [existing_query],
            "filter": [es_filter],
        }
    }
    return scoped_body


def default_sql_task_acl_clause(
    task_id_col: Any,
    task_user_id_col: Any,
    viewer: Optional[ViewerContext] = None,
) -> Optional[Any]:
    """Build a SQLAlchemy filter clause against `TaskAcl` for `viewer`.

    Returns `None` when the viewer has unrestricted (`see_all`) access.
    """
    active_viewer = viewer if viewer is not None else get_current_viewer()
    if active_viewer is None or active_viewer.see_all:
        return None
    if not active_viewer.is_authenticated:
        return TaskAcl.visibility == PUBLIC

    arms = [TaskAcl.visibility == PUBLIC]
    if active_viewer.user_id is not None:
        arms.append(TaskAcl.user_id == int(active_viewer.user_id))
        arms.append((TaskAcl.task_id.is_(None)) & (task_user_id_col == int(active_viewer.user_id)))
    if active_viewer.tenant_id is not None:
        arms.append((TaskAcl.tenant_id == int(active_viewer.tenant_id)) & (TaskAcl.visibility == TENANT))
    return or_(*arms)


def register_mongo_filter(fn: MongoFilterFn) -> None:
    if fn not in _mongo_filters:
        _mongo_filters.append(fn)


def register_sql_filter(fn: SqlFilterFn) -> None:
    if fn not in _sql_filters:
        _sql_filters.append(fn)


def register_es_filter(fn: EsFilterFn) -> None:
    if fn not in _es_filters:
        _es_filters.append(fn)


def apply_mongo_filters(
    collection: str,
    query: Optional[Dict[str, Any]],
    viewer: Optional[ViewerContext] = None,
) -> Dict[str, Any]:
    out = dict(query) if query else {}
    for fn in _mongo_filters:
        out = fn(collection, out, viewer)
    return out


def wrap_mongodb_module(mongodb_mod: Any) -> None:
    """Wrap `dev_utils.mongodb` functions in-place so `'analysis'` queries are auto-scoped."""
    mod_id = getattr(mongodb_mod, "__name__", str(id(mongodb_mod)))
    if mod_id in _patched_modules:
        return

    if not _mongo_filters:
        register_mongo_filter(default_mongo_analysis_filter)

    for fn_name in ("mongo_find", "mongo_find_one"):
        orig = getattr(mongodb_mod, fn_name, None)
        if orig is None or getattr(orig, "_cape_tenancy_wrapped", False):
            continue

        @functools.wraps(orig)
        def _wrapped(collection: str, query: Dict[str, Any], *args: Any, _orig=orig, **kwargs: Any):
            scoped_q = apply_mongo_filters(collection, query)
            return _orig(collection, scoped_q, *args, **kwargs)

        _wrapped._cape_tenancy_wrapped = True  # type: ignore[attr-defined]
        setattr(mongodb_mod, fn_name, _wrapped)

    orig_agg = getattr(mongodb_mod, "mongo_aggregate", None)
    if orig_agg is not None and not getattr(orig_agg, "_cape_tenancy_wrapped", False):

        @functools.wraps(orig_agg)
        def _wrapped_agg(collection: str, pipeline: List[Dict[str, Any]], *args: Any, _orig=orig_agg, **kwargs: Any):
            scoped_q = apply_mongo_filters(collection, {})
            if scoped_q:
                pipeline = [{"$match": scoped_q}] + list(pipeline)
            return _orig(collection, pipeline, *args, **kwargs)

        _wrapped_agg._cape_tenancy_wrapped = True  # type: ignore[attr-defined]
        setattr(mongodb_mod, "mongo_aggregate", _wrapped_agg)

    _patched_modules.add(mod_id)


def default_sql_task_query_filter(query: Any, viewer: Optional[ViewerContext] = None) -> Any:
    """Apply `TaskAcl` outer-join and visibility clause to a SQLAlchemy `Task` query."""
    active_viewer = viewer if viewer is not None else get_current_viewer()
    if active_viewer is None or active_viewer.see_all:
        return query
    try:
        from lib.cuckoo.core.data.task import Task
    except ImportError:
        return query
    clause = default_sql_task_acl_clause(Task.id, Task.user_id, active_viewer)
    if clause is None:
        return query
    return query.outerjoin(TaskAcl, TaskAcl.task_id == Task.id).filter(clause)


def default_report_stamp_hook(results: Dict[str, Any]) -> None:
    """Stamp `info.tenant_id`, `info.user_id`, `info.visibility`, and `info.visibility_seq` from `TaskAcl`."""
    from cape_tenancy.policy import multitenancy_config

    if not multitenancy_config().enabled:
        return
    info = results.get("info")
    if not isinstance(info, dict):
        return
    task_id = info.get("id")
    if task_id is None:
        return
    try:
        from lib.cuckoo.core.database import Database

        db = Database()
        with db.session.begin():
            row = db.session.get(TaskAcl, int(task_id))
            if row is not None:
                info["user_id"] = row.user_id
                info["tenant_id"] = row.tenant_id
                info["visibility"] = row.visibility
                info["visibility_seq"] = int(row.visibility_seq or 1)
            else:
                info.setdefault("visibility", "private")
                info.setdefault("visibility_seq", 0)
    except Exception:
        info.setdefault("visibility", "private")
        info.setdefault("visibility_seq", 0)


def register_core_hooks() -> bool:
    """Register `cape_tenancy` Mongo, SQL, ES, and Report hooks into `lib.cuckoo.common.hooks`."""
    try:
        from lib.cuckoo.common import hooks as core_hooks
    except ImportError:
        return False

    core_hooks.register_mongo_filter(lambda collection, query: default_mongo_analysis_filter(collection, query))
    core_hooks.register_sql_task_filter(default_sql_task_query_filter)
    core_hooks.register_es_filter(lambda body: default_es_query_filter(body))
    core_hooks.register_report_hook(default_report_stamp_hook)
    return True
