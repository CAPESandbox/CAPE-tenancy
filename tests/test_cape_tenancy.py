"""Unit tests for CAPE-tenancy plugin."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import Session

import cape_tenancy
from cape_tenancy import (
    PRIVATE,
    PUBLIC,
    TENANT,
    ResourceScope,
    TenancyMiddleware,
    ViewerContext,
    VisibilityConflictError,
    can_delete,
    can_read,
    can_toggle,
    default_es_query_filter,
    default_sql_task_acl_clause,
    ensure_acl_schema,
    get_current_viewer,
    install,
    resolve_task_scope,
    set_task_acl,
    sync_stamp_to_mongo,
    tenancy_exempt,
    toggle_task_visibility_cas,
    unscoped_context,
    viewer_scope_context,
    wrap_mongodb_module,
)
from cape_tenancy.central import LocalFSStore



def test_policy_predicates():
    alice = ViewerContext(user_id=10, tenant_id=1, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)
    bob_same_tenant = ViewerContext(user_id=11, tenant_id=1, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)
    tenant_admin = ViewerContext(user_id=12, tenant_id=1, is_tenant_admin=True, is_local_admin=False, is_authenticated=True)
    charlie_other = ViewerContext(user_id=20, tenant_id=2, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)

    private_scope = ResourceScope(owner_id=10, tenant_id=1, visibility=PRIVATE)
    tenant_scope = ResourceScope(owner_id=10, tenant_id=1, visibility=TENANT)
    public_scope = ResourceScope(owner_id=10, tenant_id=1, visibility=PUBLIC)

    assert can_read(alice, private_scope) is True
    assert can_read(bob_same_tenant, private_scope) is False
    assert can_read(tenant_admin, private_scope) is False

    assert can_read(bob_same_tenant, tenant_scope) is True
    assert can_read(charlie_other, tenant_scope) is False
    assert can_read(charlie_other, public_scope) is True

    assert can_toggle(alice, private_scope) is True
    assert can_toggle(tenant_admin, private_scope) is False
    assert can_toggle(tenant_admin, tenant_scope) is True
    assert can_delete(tenant_admin, tenant_scope) is True
    assert can_delete(bob_same_tenant, tenant_scope) is False


def test_task_acl_and_monotonic_visibility_cas():
    engine = create_engine("sqlite:///:memory:")
    ensure_acl_schema(engine)

    # Simulate an in-memory MongoDB 'analysis' document store
    mongo_docs = {
        42: {"info": {"id": 42, "visibility": PRIVATE, "visibility_seq": 0}},
    }

    def fake_mongo_update_one(collection: str, flt: dict, update: dict):
        assert collection == "analysis"
        and_clauses = flt["$and"]
        task_id = and_clauses[0]["info.id"]
        doc = mongo_docs[task_id]
        max_seq = and_clauses[1]["$or"][0]["info.visibility_seq"]["$lte"]
        cur_seq = doc["info"].get("visibility_seq")
        if cur_seq is None or cur_seq <= max_seq:
            doc["info"].update(
                {
                    "user_id": update["$set"]["info.user_id"],
                    "tenant_id": update["$set"]["info.tenant_id"],
                    "visibility": update["$set"]["info.visibility"],
                    "visibility_seq": update["$set"]["info.visibility_seq"],
                }
            )
            return True
        return False

    with Session(engine) as session:
        acl = set_task_acl(session, 42, user_id=10, tenant_id=1, visibility=TENANT)
        session.commit()
        assert acl.visibility_seq == 1
        assert resolve_task_scope(session, 42) == ResourceScope(owner_id=10, tenant_id=1, visibility=TENANT)

        # User toggles visibility from TENANT -> PRIVATE (seq becomes 2)
        stamp_v2 = toggle_task_visibility_cas(
            session,
            42,
            PRIVATE,
            expected_prior=TENANT,
            mongo_update_one=fake_mongo_update_one,
        )
        assert stamp_v2.visibility_seq == 2
        assert mongo_docs[42]["info"]["visibility"] == PRIVATE
        assert mongo_docs[42]["info"]["visibility_seq"] == 2

        # Simulate a lagging reporter attempting to reconcile with older stamp (seq=1, TENANT):
        # CAS filter rejects it because Mongo already has visibility_seq=2!
        stale_stamp = cape_tenancy.VisibilityStamp(
            task_id=42,
            user_id=10,
            tenant_id=1,
            visibility=TENANT,
            visibility_seq=1,
        )
        updated = sync_stamp_to_mongo(stale_stamp, mongo_update_one=fake_mongo_update_one)
        assert updated is False
        assert mongo_docs[42]["info"]["visibility"] == PRIVATE
        assert mongo_docs[42]["info"]["visibility_seq"] == 2

        # Conflict check when expected_prior does not match
        with pytest.raises(VisibilityConflictError):
            toggle_task_visibility_cas(session, 42, PUBLIC, expected_prior=TENANT)


def test_contextvar_and_mongodb_auto_scoping(monkeypatch):
    monkeypatch.setattr(
        "cape_tenancy.policy.multitenancy_config",
        lambda: cape_tenancy.MultitenancyConfig(
            enabled=True, mode="shared", default_visibility="public", local_admins_manage_all_tenants=True
        ),
    )
    captured_queries = []

    fake_mongo = types.ModuleType("fake_dev_utils_mongodb")

    def mongo_find(collection, query, *args, **kwargs):
        captured_queries.append(("find", collection, query))
        return []

    def mongo_find_one(collection, query, *args, **kwargs):
        captured_queries.append(("find_one", collection, query))
        return None

    def mongo_aggregate(collection, pipeline, *args, **kwargs):
        captured_queries.append(("aggregate", collection, pipeline))
        return []

    fake_mongo.mongo_find = mongo_find
    fake_mongo.mongo_find_one = mongo_find_one
    fake_mongo.mongo_aggregate = mongo_aggregate

    wrap_mongodb_module(fake_mongo)

    viewer = ViewerContext(user_id=7, tenant_id=3, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)

    with viewer_scope_context(viewer):
        assert get_current_viewer() == viewer
        fake_mongo.mongo_find_one("analysis", {"info.id": 99})
        fake_mongo.mongo_find("calls", {"_id": "abc"})
        fake_mongo.mongo_aggregate("analysis", [{"$project": {"info.id": 1}}])

        with unscoped_context():
            fake_mongo.mongo_find_one("analysis", {"info.id": 99})

    assert get_current_viewer() is None

    # 1. 'analysis' find_one was automatically scoped with viewer's $or filter
    kind, coll, q1 = captured_queries[0]
    assert (kind, coll) == ("find_one", "analysis")
    assert "$and" in q1
    assert q1["$and"][0] == {"info.id": 99}
    assert {"info.visibility": PUBLIC} in q1["$and"][1]["$or"]
    assert {"info.user_id": 7} in q1["$and"][1]["$or"]
    assert {"info.tenant_id": 3, "info.visibility": TENANT} in q1["$and"][1]["$or"]

    # 2. Non-'analysis' collection ('calls') is untouched
    assert captured_queries[1] == ("find", "calls", {"_id": "abc"})

    # 3. 'analysis' aggregate had $match prepended
    _, _, pipe = captured_queries[2]
    assert "$match" in pipe[0]

    # 4. Inside unscoped_context(), query is untouched
    assert captured_queries[3] == ("find_one", "analysis", {"info.id": 99})


def test_middleware_process_view_gates_tasks_and_samples():
    scopes = {
        100: ResourceScope(owner_id=10, tenant_id=1, visibility=PRIVATE),
        101: ResourceScope(owner_id=10, tenant_id=1, visibility=TENANT),
        102: ResourceScope(owner_id=10, tenant_id=1, visibility=PUBLIC),
    }

    viewer_bob = ViewerContext(user_id=11, tenant_id=1, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)

    mw = TenancyMiddleware(
        lambda req: {"status_code": 200, "viewer": get_current_viewer()},
        viewer_resolver=lambda user: viewer_bob,
        task_scope_resolver=lambda tid: scopes.get(tid),
        sample_access_checker=lambda v, flt: flt.get("sha256") == "allowed_sha256",
        deny_response_factory=lambda req, msg: {"status_code": 404, "error_value": msg},
    )

    def report_view(request, task_id):
        return {"status_code": 200}

    def delete_task_view(request, task_id):
        return {"status_code": 200}

    @tenancy_exempt
    def public_health_view(request, task_id):
        return {"status_code": 200}

    req = types.SimpleNamespace(path="/analysis/100/", user=object())
    # Bob cannot read Alice's private task 100
    res_private = mw.process_view(req, report_view, (), {"task_id": 100})
    assert res_private == {"status_code": 404, "error_value": "Task not found"}

    # Bob CAN read Alice's tenant-scoped task 101
    req.path = "/analysis/101/"
    assert mw.process_view(req, report_view, (), {"task_id": 101}) is None

    # Bob CANNOT delete Alice's tenant-scoped task 101 (not owner, not tenant_admin)
    req.path = "/apiv2/tasks/delete/101/"
    res_del = mw.process_view(req, delete_task_view, (), {"task_id": 101})
    assert res_del == {"status_code": 404, "error_value": "Task not found"}

    # Exempt view bypasses gate
    assert mw.process_view(req, public_health_view, (), {"task_id": 100}) is None

    # Sample hash gate
    req.path = "/apiv2/files/view/sha256/blocked_sha256/"
    res_sample = mw.process_view(req, report_view, (), {"sha256": "blocked_sha256"})
    assert res_sample == {"status_code": 404, "error_value": "Sample not found in database"}
    assert mw.process_view(req, report_view, (), {"sha256": "allowed_sha256"}) is None


def test_install_into_django_settings():
    settings_dict = {
        "INSTALLED_APPS": ["django.contrib.auth", "analysis"],
        "MIDDLEWARE": ["django.contrib.auth.middleware.AuthenticationMiddleware"],
        "TEMPLATES": [{"BACKEND": "django.template.backends.django.DjangoTemplates", "OPTIONS": {"context_processors": []}}],
    }
    install(settings_dict, wrap_mongo=False)
    assert "cape_tenancy.apps.CapeTenancyConfig" in settings_dict["INSTALLED_APPS"]
    assert "cape_tenancy.middleware.TenancyMiddleware" in settings_dict["MIDDLEWARE"]
    assert (
        "cape_tenancy.context_processors.tenancy_context"
        in settings_dict["TEMPLATES"][0]["OPTIONS"]["context_processors"]
    )


def test_es_and_sql_filter_helpers(monkeypatch):
    monkeypatch.setattr(
        "cape_tenancy.policy.multitenancy_config",
        lambda: cape_tenancy.MultitenancyConfig(
            enabled=True, mode="shared", default_visibility="public", local_admins_manage_all_tenants=True
        ),
    )
    viewer = ViewerContext(user_id=5, tenant_id=2, is_tenant_admin=False, is_local_admin=False, is_authenticated=True)
    with viewer_scope_context(viewer):
        es_body = default_es_query_filter({"query": {"term": {"target": "evil.exe"}}})
        assert "bool" in es_body["query"]
        assert es_body["query"]["bool"]["must"] == [{"term": {"target": "evil.exe"}}]
        assert len(es_body["query"]["bool"]["filter"]) == 1

        sql_clause = default_sql_task_acl_clause(Column("id", Integer), Column("user_id", Integer))
        assert sql_clause is not None



def test_local_storage_backend_roundtrip(tmp_path: Path):
    backend = LocalFSStore(str(tmp_path))
    src = tmp_path / "source.bin"
    src.write_bytes(b"MZ-test-payload")
    backend.put_file(str(src), "jobs/ui-10", "binary")
    assert backend.exists("jobs/ui-10", "binary") is True
    chunks, length = backend.stream("jobs/ui-10", "binary")
    assert b"".join(chunks) == b"MZ-test-payload"
    assert length == len(b"MZ-test-payload")


def test_batched_mongo_backfill():
    from cape_tenancy.backfill import backfill_from_task_acl_batched, backfill_legacy_uniform

    calls = []

    def fake_update_many(collection, query, update):
        calls.append((collection, query, update))
        return 100

    res = backfill_legacy_uniform(fake_update_many, default_visibility=PUBLIC)
    assert res == 100
    assert len(calls) == 1
    assert calls[0][2]["$set"]["info.visibility"] == PUBLIC

    engine = create_engine("sqlite:///:memory:")
    ensure_acl_schema(engine)
    bulk_batches = []

    def fake_bulk_updater(ops):
        bulk_batches.append(ops)
        return len(ops)

    with Session(engine) as session:
        set_task_acl(session, 1, user_id=10, tenant_id=2, visibility=TENANT)
        set_task_acl(session, 2, user_id=11, tenant_id=2, visibility=PRIVATE)
        session.commit()

        count = backfill_from_task_acl_batched(
            session,
            [1, 2, 3],
            fake_bulk_updater,
            chunk_size=2,
            fallback_visibility=PUBLIC,
        )
        assert count == 3
        assert len(bulk_batches) == 2
        assert bulk_batches[0][0][1]["$set"]["info.visibility"] == TENANT
        assert bulk_batches[0][1][1]["$set"]["info.visibility"] == PRIVATE
        assert bulk_batches[1][0][1]["$set"]["info.visibility"] == PUBLIC


