"""
Rebuild Magento Product Map entries from Magento (read-only).

Uses GET /products/{sku} only — never creates or updates Magento catalog data.
Only writes ERPNext-side map rows and Item custom fields.
"""

import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.background_jobs import is_job_enqueued
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_product_map.magento_product_map import upsert_map
from connector.connector.doctype.magento_sync_log.magento_sync_log import create_log
from connector.sync.product_sync import _get_allowed_item_groups

# Cache key: test must pass before full rebuild is allowed (per user, 24 h).
TEST_PASSED_CACHE_KEY = "magento_product_map_rebuild_test_passed"
TEST_PASSED_TTL_SEC = 86400

FULL_REBUILD_JOB_PREFIX = "magento_full_product_map_rebuild"
REBUILD_ACTIVE_RUN_KEY = "magento_product_map_rebuild_active_run"
REBUILD_PROGRESS_KEY_PREFIX = "magento_product_map_rebuild_progress"
REBUILD_PROGRESS_TTL_SEC = 604800  # 7 days

FULL_REBUILD_CHUNK_SIZE = 50
FULL_REBUILD_CHUNK_TIMEOUT = 1800
STALE_RUN_MINUTES = 45

DEFAULT_TEST_SAMPLE_SIZE = 5
MAX_TEST_SAMPLE_SIZE = 20


def _require_system_manager():
    if "System Manager" not in frappe.get_roles():
        frappe.throw(
            "Only System Manager can rebuild product maps.",
            frappe.PermissionError,
        )


def _progress_cache_key(run_id):
    return f"{REBUILD_PROGRESS_KEY_PREFIX}:{run_id}"


def _get_eligible_items():
    """
    Items with sync_to_magento enabled and not disabled, respecting Magento
    Settings item groups. Templates (has_variants) are listed before variants.
    """
    filters = {"sync_to_magento": 1, "disabled": 0}
    allowed_groups = _get_allowed_item_groups()
    if allowed_groups:
        filters["item_group"] = ["in", list(allowed_groups)]

    items = frappe.get_all(
        "Item",
        filters=filters,
        fields=["item_code", "has_variants", "variant_of", "magento_product_id"],
        order_by="item_code asc",
    )
    items.sort(key=lambda row: (0 if row.get("has_variants") else 1, row["item_code"]))
    return items


def _get_map_state(item_code):
    return frappe.db.get_value(
        "Magento Product Map",
        item_code,
        ["magento_product_id", "sync_status", "magento_sku"],
        as_dict=True,
    )


def _is_synced_map(map_row):
    return bool(
        map_row
        and map_row.get("magento_product_id")
        and map_row.get("sync_status") == "Synced"
    )


def _is_pending_not_in_magento(map_row):
    """Set during full rebuild when Magento has no matching SKU."""
    return bool(
        map_row
        and map_row.get("sync_status") == "Pending"
        and not map_row.get("magento_product_id")
    )


def _items_needing_rebuild(*, include_pending=False):
    """
    Return ordered item codes that still need a map rebuild.
    Excludes Pending rows (checked, not in Magento) unless include_pending=True.
    """
    items = _get_eligible_items()
    ordered = []
    for row in items:
        item_code = row["item_code"]
        map_row = _get_map_state(item_code)
        if _is_synced_map(map_row):
            continue
        if not include_pending and _is_pending_not_in_magento(map_row):
            continue
        ordered.append(item_code)
    return ordered


def get_rebuild_preview():
    """Return counts and sample rows without calling Magento per item."""
    _require_system_manager()

    items = _get_eligible_items()
    mapped_ok = 0
    pending_not_in_magento = 0
    needs_rebuild = []
    id_on_item_only = 0

    for row in items:
        item_code = row["item_code"]
        map_row = _get_map_state(item_code)
        if _is_synced_map(map_row):
            mapped_ok += 1
        elif _is_pending_not_in_magento(map_row):
            pending_not_in_magento += 1
        else:
            needs_rebuild.append(item_code)
            if row.get("magento_product_id") and not map_row:
                id_on_item_only += 1

    progress = get_rebuild_progress()

    return {
        "eligible_total": len(items),
        "already_mapped": mapped_ok,
        "pending_not_in_magento": pending_not_in_magento,
        "needs_rebuild": len(needs_rebuild),
        "item_id_without_map": id_on_item_only,
        "sample_needs_rebuild": needs_rebuild[:10],
        "allowed_item_groups": list(_get_allowed_item_groups()) or None,
        "active_rebuild": progress if progress.get("status") == "running" else None,
    }


def _record_not_in_magento(item_code):
    """Mark item as checked; no matching SKU in Magento (does not modify Magento)."""
    upsert_map(
        item_code,
        0,
        item_code,
        status="Pending",
        retry_count=0,
        last_failed_at=None,
    )
    if frappe.db.exists("Item", item_code):
        frappe.db.set_value(
            "Item",
            item_code,
            {
                "magento_product_id": None,
                "magento_sync_error": "No Magento product with SKU matching item_code (map rebuild)",
            },
        )


def rebuild_map_for_item(
    client,
    item_code,
    *,
    dry_run=False,
    skip_existing=True,
    mark_not_in_magento=False,
):
    """
    Restore one map entry by SKU lookup in Magento (GET only).
    Returns a result dict with status: mapped | skipped_existing |
    skipped_not_in_magento | failed | dry_run_ok
    """
    result = {
        "item_code": item_code,
        "status": "failed",
        "magento_product_id": None,
        "message": "",
    }

    if skip_existing:
        map_row = _get_map_state(item_code)
        if _is_synced_map(map_row):
            result.update(
                status="skipped_existing",
                magento_product_id=map_row.magento_product_id,
                message="Already mapped — skipped (no Magento API call)",
            )
            return result

    try:
        if not client.product_exists(item_code):
            if mark_not_in_magento and not dry_run:
                _record_not_in_magento(item_code)
            result.update(
                status="skipped_not_in_magento",
                message="No Magento product with SKU matching this item_code",
            )
            return result

        product = client.get_product(item_code)
        magento_id = product.get("id")
        if not magento_id:
            result["message"] = "Magento product found but returned no ID"
            return result

        if dry_run:
            result.update(
                status="dry_run_ok",
                magento_product_id=magento_id,
                message="Would restore map (dry run — no ERPNext changes written)",
            )
            return result

        upsert_map(
            item_code,
            magento_id,
            item_code,
            status="Synced",
            retry_count=0,
            last_failed_at=None,
        )
        frappe.db.set_value(
            "Item",
            item_code,
            {
                "magento_product_id": magento_id,
                "magento_last_synced_on": frappe.utils.now_datetime(),
                "magento_sync_error": "",
            },
            update_modified=False,
        )

        create_log(
            operation="Product Map Rebuild",
            status="Success",
            doctype_name="Item",
            document_name=item_code,
            magento_id=magento_id,
        )

        result.update(
            status="mapped",
            magento_product_id=magento_id,
            message="Product map restored from Magento",
        )
        return result

    except Exception as exc:
        result["message"] = str(exc)
        if not dry_run:
            create_log(
                operation="Product Map Rebuild",
                status="Failed",
                doctype_name="Item",
                document_name=item_code,
                error_message=str(exc)[:500],
            )
        return result


def run_rebuild_batch(
    item_codes,
    *,
    dry_run=False,
    skip_existing=True,
    require_role=True,
    mark_not_in_magento=False,
):
    """Process a list of item codes; commit once at end unless dry_run."""
    if require_role:
        _require_system_manager()

    if not item_codes:
        return {"results": [], "summary": _empty_summary()}

    try:
        client = MagentoClient()
    except Exception as exc:
        frappe.throw(f"Cannot connect to Magento: {exc}")

    results = []
    for item_code in item_codes:
        results.append(
            rebuild_map_for_item(
                client,
                item_code,
                dry_run=dry_run,
                skip_existing=skip_existing,
                mark_not_in_magento=mark_not_in_magento,
            )
        )

    if not dry_run:
        frappe.db.commit()

    summary = _summarize_results(results)
    summary["dry_run"] = dry_run
    return {"results": results, "summary": summary}


def _empty_summary():
    return {
        "total": 0,
        "mapped": 0,
        "skipped_existing": 0,
        "skipped_not_in_magento": 0,
        "dry_run_ok": 0,
        "failed": 0,
    }


def _summarize_results(results):
    summary = _empty_summary()
    summary["total"] = len(results)
    for row in results:
        status = row.get("status")
        if status in summary:
            summary[status] += 1
        elif status == "failed":
            summary["failed"] += 1
    return summary


def _merge_summary(progress, summary):
    for key in ("mapped", "skipped_existing", "skipped_not_in_magento", "failed"):
        progress[key] = (progress.get(key) or 0) + summary.get(key, 0)


def _format_results_report(results, summary, *, title):
    lines = [f"=== {title} ===", ""]
    lines.append(
        f"Total: {summary['total']}  |  Mapped: {summary.get('mapped', 0)}  |  "
        f"Skipped (existing): {summary.get('skipped_existing', 0)}  |  "
        f"Skipped (not in Magento): {summary.get('skipped_not_in_magento', 0)}  |  "
        f"Failed: {summary.get('failed', 0)}"
    )
    if summary.get("dry_run"):
        lines.append("Mode: DRY RUN — no ERPNext records were modified.")
    lines.append("")
    for row in results:
        icon = {
            "mapped": "✓",
            "dry_run_ok": "○",
            "skipped_existing": "·",
            "skipped_not_in_magento": "—",
            "failed": "✗",
        }.get(row.get("status"), "?")
        mid = row.get("magento_product_id") or "—"
        lines.append(f"  {icon} {row['item_code']:<30}  ID: {mid}  {row.get('message') or ''}")
    return "\n".join(lines)


def _save_progress(progress):
    progress["last_updated"] = str(now_datetime())
    frappe.cache().set_value(
        _progress_cache_key(progress["run_id"]),
        progress,
        expires_in_sec=REBUILD_PROGRESS_TTL_SEC,
    )
    if progress.get("status") == "running":
        frappe.cache().set_value(
            REBUILD_ACTIVE_RUN_KEY,
            progress["run_id"],
            expires_in_sec=REBUILD_PROGRESS_TTL_SEC,
        )
    elif progress.get("status") in ("complete", "failed", "stopped"):
        frappe.cache().delete_value(REBUILD_ACTIVE_RUN_KEY)


def get_rebuild_progress(run_id=None):
    """Return progress for the active or specified rebuild run."""
    if not run_id:
        run_id = frappe.cache().get_value(REBUILD_ACTIVE_RUN_KEY)
    if not run_id:
        return {"status": "idle"}
    progress = frappe.cache().get_value(_progress_cache_key(run_id)) or {}
    if not progress:
        return {"status": "idle", "run_id": run_id}
    progress.setdefault("run_id", run_id)
    if progress.get("status") == "running" and _is_progress_stale(progress):
        progress["status"] = "stale"
        progress["message"] = (
            "No progress update recently — the background worker may have stopped. "
            "Use Resume Rebuild to continue."
        )
    total = progress.get("total") or 0
    processed = progress.get("processed") or 0
    progress["remaining"] = max(0, total - processed)
    progress["percent"] = round((processed / total) * 100, 1) if total else 0
    return progress


def _is_progress_stale(progress):
    last_updated = progress.get("last_updated")
    if not last_updated:
        return True
    try:
        cutoff = add_to_date(now_datetime(), minutes=-STALE_RUN_MINUTES)
        return frappe.utils.get_datetime(last_updated) < cutoff
    except Exception:
        return False


def _chunk_job_id(run_id, chunk_no):
    return f"{FULL_REBUILD_JOB_PREFIX}_{run_id}_{chunk_no}"


def _is_rebuild_job_running(run_id=None):
    """True if any chunk job for the active run is queued or running."""
    if not run_id:
        run_id = frappe.cache().get_value(REBUILD_ACTIVE_RUN_KEY)
    if not run_id:
        return False
    progress = frappe.cache().get_value(_progress_cache_key(run_id)) or {}
    chunk_no = progress.get("next_chunk") or 0
    # Current or next chunk may be in queue
    for n in range(max(0, chunk_no - 1), chunk_no + 2):
        if is_job_enqueued(_chunk_job_id(run_id, n)):
            return True
    return False


def test_rebuild_product_maps(sample_size=None, dry_run=False):
    """
    Pilot rebuild on a small sample (default 5 items).
    On success (zero failures), caches approval for full rebuild.
    """
    _require_system_manager()

    sample_size = int(sample_size or DEFAULT_TEST_SAMPLE_SIZE)
    if sample_size < 1 or sample_size > MAX_TEST_SAMPLE_SIZE:
        frappe.throw(f"Sample size must be between 1 and {MAX_TEST_SAMPLE_SIZE}.")

    try:
        MagentoClient()
    except Exception as exc:
        frappe.throw(f"Magento connection check failed: {exc}")

    preview = get_rebuild_preview()
    candidates = _items_needing_rebuild()
    if not candidates:
        frappe.throw(
            "No items need map rebuild. All eligible items already have synced map entries.",
            title="Nothing To Rebuild",
        )

    batch = candidates[:sample_size]
    outcome = run_rebuild_batch(batch, dry_run=bool(dry_run), skip_existing=not dry_run)
    results = outcome["results"]
    summary = outcome["summary"]

    test_passed = (
        summary.get("failed", 0) == 0
        and summary.get("total", 0) > 0
        and (
            summary.get("mapped", 0)
            + summary.get("dry_run_ok", 0)
            + summary.get("skipped_existing", 0)
        )
        > 0
    )
    if test_passed and not dry_run:
        _mark_test_passed(summary, len(batch))

    report = _format_results_report(
        results,
        summary,
        title=f"PRODUCT MAP REBUILD TEST ({len(batch)} items" + (", dry run" if dry_run else "") + ")",
    )

    return {
        "test_passed": test_passed,
        "dry_run": bool(dry_run),
        "preview": preview,
        "summary": summary,
        "report": report,
        "full_rebuild_allowed": _test_passed_for_current_user() if not dry_run else False,
    }


def _mark_test_passed(summary, sample_size):
    frappe.cache().set_value(
        TEST_PASSED_CACHE_KEY,
        {
            "user": frappe.session.user,
            "at": str(now_datetime()),
            "mapped": summary.get("mapped", 0),
            "sample_size": sample_size,
            "failed": summary.get("failed", 0),
        },
        expires_in_sec=TEST_PASSED_TTL_SEC,
    )


def _test_passed_for_current_user():
    data = frappe.cache().get_value(TEST_PASSED_CACHE_KEY)
    if not data:
        return False
    return data.get("user") == frappe.session.user and (data.get("failed") or 0) == 0


def get_test_passed_status():
    """Return cached test-pass state for the current user."""
    data = frappe.cache().get_value(TEST_PASSED_CACHE_KEY) or {}
    return {
        "passed": _test_passed_for_current_user(),
        "user": data.get("user"),
        "at": data.get("at"),
        "sample_size": data.get("sample_size"),
        "mapped": data.get("mapped"),
    }


def clear_test_passed():
    frappe.cache().delete_value(TEST_PASSED_CACHE_KEY)


def _init_rebuild_run(candidates):
    run_id = frappe.generate_hash(length=12)
    progress = {
        "run_id": run_id,
        "status": "running",
        "started_at": str(now_datetime()),
        "started_by": frappe.session.user,
        "total": len(candidates),
        "processed": 0,
        "offset": 0,
        "mapped": 0,
        "skipped_existing": 0,
        "skipped_not_in_magento": 0,
        "failed": 0,
        "chunks_completed": 0,
        "next_chunk": 0,
        "item_codes": candidates,
        "message": f"Queued — {len(candidates)} item(s) to process.",
        "last_error": None,
    }
    _save_progress(progress)
    return progress


def _enqueue_rebuild_chunk(run_id, chunk_no):
    frappe.enqueue(
        "connector.sync.product_map_rebuild.run_full_rebuild_chunk",
        queue="long",
        timeout=FULL_REBUILD_CHUNK_TIMEOUT,
        job_id=_chunk_job_id(run_id, chunk_no),
        deduplicate=False,
        enqueue_after_commit=True,
        run_id=run_id,
        chunk_no=chunk_no,
    )


def trigger_full_product_map_rebuild(*, confirm_phrase="", force=False, resume=False):
    """
    Enqueue background rebuild for all items still missing maps.
    Requires a successful test run by the current user unless force=True.
    """
    _require_system_manager()

    if not resume and confirm_phrase != "REBUILD MAPS":
        frappe.throw(
            'Type exactly "REBUILD MAPS" in the confirmation field to proceed.',
            title="Confirmation Required",
        )

    if not resume and not force and not _test_passed_for_current_user():
        frappe.throw(
            "Run a successful test rebuild (5 items) before rebuilding all maps.",
            title="Test Required",
        )

    progress = get_rebuild_progress()
    if progress.get("status") == "running" and _is_rebuild_job_running(progress.get("run_id")):
        frappe.throw(
            "A full product map rebuild is already running. Check progress below.",
            title="Job Already Running",
        )

    if resume and progress.get("run_id") and progress.get("status") in ("running", "stale", "failed"):
        run_id = progress["run_id"]
        chunk_no = progress.get("next_chunk") or progress.get("chunks_completed") or 0
        progress["status"] = "running"
        progress["message"] = "Resuming rebuild…"
        progress["last_error"] = None
        _save_progress(progress)
        _enqueue_rebuild_chunk(run_id, chunk_no)
        return {
            "queued": True,
            "run_id": run_id,
            "resumed": True,
            "message": f"Resumed rebuild at item {progress.get('processed', 0)} of {progress.get('total', 0)}.",
            "progress": get_rebuild_progress(run_id),
        }

    preview = get_rebuild_preview()
    candidates = _items_needing_rebuild()
    if not candidates:
        frappe.msgprint(
            "All eligible items already have product map entries. Nothing to rebuild.",
            indicator="green",
            title="Already Complete",
        )
        return {"queued": False, "preview": preview}

    progress = _init_rebuild_run(candidates)
    _enqueue_rebuild_chunk(progress["run_id"], 0)

    return {
        "queued": True,
        "run_id": progress["run_id"],
        "preview": preview,
        "progress": get_rebuild_progress(progress["run_id"]),
        "message": (
            f"Queued rebuild for {len(candidates)} item(s) in background. "
            f"({preview.get('already_mapped', 0)} already mapped.)"
        ),
    }


def run_full_rebuild_chunk(run_id=None, chunk_no=0):
    """Process one chunk of the active rebuild run; enqueue the next chunk if more remain."""
    logger = frappe.logger("connector")

    if not run_id:
        run_id = frappe.cache().get_value(REBUILD_ACTIVE_RUN_KEY)
    if not run_id:
        logger.warning("run_full_rebuild_chunk: no active run_id.")
        return

    progress = frappe.cache().get_value(_progress_cache_key(run_id)) or {}
    if not progress or progress.get("status") not in ("running", None):
        logger.info(f"run_full_rebuild_chunk: run {run_id} is not active.")
        return

    item_codes = progress.get("item_codes") or []
    offset = progress.get("offset") or 0
    chunk = item_codes[offset : offset + FULL_REBUILD_CHUNK_SIZE]

    if not chunk:
        progress["status"] = "complete"
        progress["message"] = (
            f"Complete — {progress.get('mapped', 0)} mapped, "
            f"{progress.get('skipped_not_in_magento', 0)} not in Magento, "
            f"{progress.get('failed', 0)} failed."
        )
        _save_progress(progress)
        clear_test_passed()
        logger.info(f"run_full_rebuild_chunk: run {run_id} complete.")
        return

    remaining = len(item_codes) - offset - len(chunk)
    progress["message"] = (
        f"Processing items {offset + 1}–{offset + len(chunk)} of {len(item_codes)} "
        f"({remaining} remaining after this chunk)…"
    )
    _save_progress(progress)

    logger.info(
        f"run_full_rebuild_chunk: run={run_id} chunk={chunk_no} "
        f"items {offset + 1}-{offset + len(chunk)} of {len(item_codes)}."
    )

    try:
        outcome = run_rebuild_batch(
            chunk,
            dry_run=False,
            skip_existing=True,
            require_role=False,
            mark_not_in_magento=True,
        )
        summary = outcome["summary"]
        _merge_summary(progress, summary)
        progress["processed"] = (progress.get("processed") or 0) + len(chunk)
        progress["offset"] = offset + len(chunk)
        progress["chunks_completed"] = (progress.get("chunks_completed") or 0) + 1
        progress["next_chunk"] = chunk_no + 1
        progress["last_error"] = None
        _save_progress(progress)
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Product Map Rebuild Chunk Failed")
        progress["last_error"] = str(exc)[:500]
        progress["status"] = "failed"
        progress["message"] = f"Chunk {chunk_no} failed: {exc}"
        _save_progress(progress)
        raise

    if progress["offset"] < len(item_codes):
        _enqueue_rebuild_chunk(run_id, chunk_no + 1)
        logger.info(
            f"run_full_rebuild_chunk: enqueued chunk {chunk_no + 1} "
            f"({len(item_codes) - progress['offset']} items left)."
        )
    else:
        progress["status"] = "complete"
        progress["message"] = (
            f"Complete — {progress.get('mapped', 0)} mapped, "
            f"{progress.get('skipped_not_in_magento', 0)} not in Magento, "
            f"{progress.get('failed', 0)} failed."
        )
        _save_progress(progress)
        clear_test_passed()
        logger.info(f"run_full_rebuild_chunk: run {run_id} complete.")
