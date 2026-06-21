"""
Rebuild Magento Product Map entries from Magento (read-only).

Uses GET /products/{sku} only — never creates or updates Magento catalog data.
Only writes ERPNext-side map rows and Item custom fields.
"""

import frappe
from frappe.utils.background_jobs import is_job_enqueued
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_product_map.magento_product_map import upsert_map
from connector.connector.doctype.magento_sync_log.magento_sync_log import create_log
from connector.sync.product_sync import _get_allowed_item_groups

# Cache key: test must pass before full rebuild is allowed (per user, 24 h).
TEST_PASSED_CACHE_KEY = "magento_product_map_rebuild_test_passed"
TEST_PASSED_TTL_SEC = 86400

FULL_REBUILD_JOB_NAME = "magento_full_product_map_rebuild"
FULL_REBUILD_CHUNK_SIZE = 50
FULL_REBUILD_CHUNK_TIMEOUT = 1800

DEFAULT_TEST_SAMPLE_SIZE = 5
MAX_TEST_SAMPLE_SIZE = 20


def _require_system_manager():
    if "System Manager" not in frappe.get_roles():
        frappe.throw(
            "Only System Manager can rebuild product maps.",
            frappe.PermissionError,
        )


def _get_eligible_items():
    """
    Items with sync_to_magento enabled, respecting Magento Settings item groups.
    Templates (has_variants) are listed before variants/simple items.
    """
    filters = {"sync_to_magento": 1}
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


def get_rebuild_preview():
    """Return counts and sample rows without calling Magento per item."""
    _require_system_manager()

    items = _get_eligible_items()
    mapped_ok = 0
    needs_rebuild = []
    id_on_item_only = 0

    for row in items:
        item_code = row["item_code"]
        map_row = _get_map_state(item_code)
        if map_row and map_row.get("magento_product_id") and map_row.get("sync_status") == "Synced":
            mapped_ok += 1
        else:
            needs_rebuild.append(item_code)
            if row.get("magento_product_id") and not map_row:
                id_on_item_only += 1

    return {
        "eligible_total": len(items),
        "already_mapped": mapped_ok,
        "needs_rebuild": len(needs_rebuild),
        "item_id_without_map": id_on_item_only,
        "sample_needs_rebuild": needs_rebuild[:10],
        "allowed_item_groups": list(_get_allowed_item_groups()) or None,
    }


def rebuild_map_for_item(client, item_code, *, dry_run=False, skip_existing=True):
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

    if skip_existing and not dry_run:
        map_row = _get_map_state(item_code)
        if map_row and map_row.get("magento_product_id") and map_row.get("sync_status") == "Synced":
            try:
                if client.product_exists(item_code):
                    product = client.get_product(item_code)
                    magento_id = product.get("id")
                    if magento_id and int(magento_id) == int(map_row.magento_product_id):
                        result.update(
                            status="skipped_existing",
                            magento_product_id=magento_id,
                            message="Already mapped with matching Magento product ID",
                        )
                        return result
                    result["message"] = (
                        f"Map ID {map_row.magento_product_id} differs from Magento ID {magento_id}; updating map"
                    )
            except MagentoAPIError as exc:
                result["message"] = str(exc)
                return result

    try:
        if not client.product_exists(item_code):
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


def _items_needing_rebuild():
    preview = get_rebuild_preview()
    items = _get_eligible_items()
    needs = set()
    for row in items:
        map_row = _get_map_state(row["item_code"])
        if not (map_row and map_row.get("magento_product_id") and map_row.get("sync_status") == "Synced"):
            needs.add(row["item_code"])
    ordered = [row["item_code"] for row in items if row["item_code"] in needs]
    return ordered, preview


def run_rebuild_batch(item_codes, *, dry_run=False, skip_existing=True):
    """Process a list of item codes; commit once at end unless dry_run."""
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
    candidates, _ = _items_needing_rebuild()
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
            "at": str(frappe.utils.now_datetime()),
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


def trigger_full_product_map_rebuild(*, confirm_phrase="", force=False):
    """
    Enqueue background rebuild for all items still missing maps.
    Requires a successful test run by the current user unless force=True (System Manager).
    """
    _require_system_manager()

    if confirm_phrase != "REBUILD MAPS":
        frappe.throw(
            'Type exactly "REBUILD MAPS" in the confirmation field to proceed.',
            title="Confirmation Required",
        )

    if not force and not _test_passed_for_current_user():
        frappe.throw(
            "Run a successful test rebuild (5 items) before rebuilding all maps, "
            "or contact an administrator.",
            title="Test Required",
        )

    preview = get_rebuild_preview()
    if preview["needs_rebuild"] == 0:
        frappe.msgprint(
            "All eligible items already have product map entries. Nothing to rebuild.",
            indicator="green",
            title="Already Complete",
        )
        return {"queued": False, "preview": preview}

    if is_job_enqueued(FULL_REBUILD_JOB_NAME):
        frappe.throw(
            "A full product map rebuild is already running. Wait for it to finish.",
            title="Job Already Running",
        )

    frappe.enqueue(
        "connector.sync.product_map_rebuild.run_full_rebuild_chunk",
        queue="long",
        timeout=FULL_REBUILD_CHUNK_TIMEOUT,
        job_id=FULL_REBUILD_JOB_NAME,
        deduplicate=True,
        enqueue_after_commit=True,
    )

    return {
        "queued": True,
        "preview": preview,
        "message": f"Queued rebuild for {preview['needs_rebuild']} item(s) in background.",
    }


def run_full_rebuild_chunk():
    """Process one chunk of unmapped items; re-enqueue if more remain."""
    candidates, _preview = _items_needing_rebuild()
    if not candidates:
        frappe.logger("connector").info("run_full_rebuild_chunk: nothing left to rebuild.")
        clear_test_passed()
        return

    chunk = candidates[:FULL_REBUILD_CHUNK_SIZE]
    remaining = len(candidates) - len(chunk)

    frappe.logger("connector").info(
        f"run_full_rebuild_chunk: processing {len(chunk)} items ({remaining} remaining)."
    )

    run_rebuild_batch(chunk, dry_run=False, skip_existing=True)

    if remaining > 0:
        frappe.enqueue(
            "connector.sync.product_map_rebuild.run_full_rebuild_chunk",
            queue="long",
            timeout=FULL_REBUILD_CHUNK_TIMEOUT,
            job_id=FULL_REBUILD_JOB_NAME,
            deduplicate=True,
            enqueue_after_commit=True,
        )
    else:
        clear_test_passed()
        frappe.logger("connector").info("run_full_rebuild_chunk: complete.")
