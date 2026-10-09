# CAPE-tenancy

> [!WARNING]
> **Experimental / Beta & Community-Maintained:**
> Core CAPEv2 developers do **not** use Multi-Tenancy (`[multitenancy]`) or Central Mode (`[central_mode]`) in their own deployments. This repository is extracted out-of-tree to keep CAPEv2 core clean and is **community-driven**. Ongoing testing, maintenance, and bug fixes depend on the organizations and contributors who run these features in production.

Out-of-tree Multi-Tenancy (`[multitenancy]`) and Central Mode (`[central_mode]` / `[centralstore]`) plugin for [CAPEv2](https://github.com/kevoreilly/CAPEv2).


Designed following the same decoupling pattern as `CAPE-mcp` and `CAPE-parsers` so single-tenant CAPEv2 deployments remain completely free of tenancy schema migrations, `Task.to_dict()` field stripping, and per-view authorization boilerplate, while multi-tenant deployments run inside the **exact same CAPEv2 WebGUI**.

## Architecture

1. **Zero Core `tasks` Table Pollution (`TaskAcl` Side Table):**
   - Stores `(task_id, user_id, tenant_id, visibility, visibility_seq, external_job_id)` in `task_acl` (`cape_tenancy/acl.py`) instead of altering CAPEv2's core `tasks` table.
2. **Lock-Free Cross-Store Visibility Synchronization (`visibility_seq` CAS):**
   - Replaces cross-datastore PostgreSQL `pg_advisory_lock` + `pg_is_in_recovery()` probes held across network MongoDB writes with a monotonic `visibility_seq` compare-and-swap (CAS) predicate (`toggle_task_visibility_cas` / `sync_stamp_to_mongo`). Works identically on PostgreSQL, MySQL/MariaDB, and SQLite.
3. **Single `process_view` Gate (`TenancyMiddleware`):**
   - `TenancyMiddleware.process_view()` automatically inspects `task_id`, `analysis_id`, `sample_id`, `sha256`, `sha1`, and `md5` URL kwargs, classifies the view action (`read`, `manage`, `delete`), and returns `404` before the view executes—replacing ~45 inline view checks.
4. **Automatic Query Scoping via `ContextVar` + Data Hooks:**
   - `TenancyMiddleware` binds the active `ViewerContext` into a `contextvars.ContextVar` for the duration of each request.
   - `wrap_mongodb_module()` automatically merges `viewer_scope_match(viewer)` into `mongo_find`, `mongo_find_one`, and `mongo_aggregate` on the `"analysis"` collection.

## Installation in CAPEv2 WebGUI

Install the package into the CAPEv2 virtualenv and add two lines to `web/local_settings.py`:

```python
import cape_tenancy

cape_tenancy.install(globals())
```

Optional template partial in CAPEv2 templates (no-op when `CAPE-tenancy` is not installed):

```html
{% include "cape_tenancy/visibility_control.html" ignore missing %}
```
