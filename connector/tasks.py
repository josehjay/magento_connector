"""
Scheduled task entry points for connector.
These functions are referenced in hooks.py scheduler_events.
Each function is a thin wrapper that imports and delegates
to the relevant sync module.
"""

import frappe
from frappe.utils.background_jobs import is_job_enqueued


def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def _is_erpnext_site_sync_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_erpnext_site_sync"))
    except Exception:
        return False


def _is_job_running(job_id):
    """Return True if a job with the exact job_id is queued or currently running."""
    try:
        return is_job_enqueued(job_id)
    except Exception:
        return False


def sync_inventory():
    """
    Every 15 minutes: push stock quantities to Magento.

    Runs in its own background job with a generous explicit timeout instead
    of executing inline in the scheduler tick. Without this, the job inherits
    the scheduler's short default timeout (~300s) — easily exceeded once you
    have more than a couple dozen SKUs, even with per-item error isolation,
    since a single slow Magento response can eat most of that budget alone.
    """
    if not _is_magento_enabled():
        return

    job_id = "connector_inventory_sync"
    if _is_job_running(job_id):
        frappe.logger("connector").info("sync_inventory: inventory sync job already running; skipping.")
        return

    try:
        frappe.enqueue(
            "connector.sync.inventory_sync.sync_inventory",
            queue="long",
            timeout=1800,
            job_id=job_id,
            deduplicate=True,
        )
        frappe.logger("connector").info("sync_inventory: enqueued inventory sync job.")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_inventory failed")


def sync_orders():
    """Every 4 hours: reconciliation sweep — pulls any orders missed by the real-time Magento push."""
    if not _is_magento_enabled():
        frappe.logger("connector").info("sync_orders: skipped — Magento integration disabled.")
        return
    try:
        from connector.sync.order_sync import sync_orders as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_orders failed")


def sync_images():
    """Every 30 minutes: pull Magento base image URLs into ERPNext Item image field."""
    if not _is_magento_enabled():
        frappe.logger("connector").info("sync_images: skipped — Magento integration disabled.")
        return

    job_id = "connector_image_sync"
    if _is_job_running(job_id):
        frappe.logger("connector").info("sync_images: image sync job already running; skipping.")
        return

    try:
        frappe.enqueue(
            "connector.sync.image_sync.sync_images",
            queue="long",
            timeout=900,
            job_id=job_id,
            deduplicate=True,
        )
        frappe.logger("connector").info("sync_images: enqueued image sync job.")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_images failed")


def full_product_sync():
    """Hourly: push Pending / Failed / unsynced ERPNext items to Magento in batches.

    Skips if a full-sync chunk or retry job is already running.
    """
    if not _is_magento_enabled():
        return

    from connector.sync.product_sync import is_full_product_sync_running

    if is_full_product_sync_running() or _is_job_running("magento_retry_failed_sync"):
        frappe.logger("connector").info(
            "full_product_sync: existing Magento product sync job running; skipping."
        )
        return

    try:
        from connector.sync.product_sync import full_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: full_product_sync failed")


def retry_failed_product_sync():
    """Every 30 minutes: batch-retry Pending/Failed Magento Product Map rows."""
    if not _is_magento_enabled():
        return
    try:
        from connector.sync.product_sync import retry_failed_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: retry_failed_product_sync failed")


def cleanup_disabled_products():
    """Every 30 minutes: remove Magento products for Items disabled in ERPNext."""
    if not _is_magento_enabled():
        return
    try:
        from connector.sync.product_sync import cleanup_disabled_items_from_magento as _cleanup
        _cleanup()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: cleanup_disabled_products failed")


def cleanup_sync_logs():
    """Daily: scrub bulky Success payloads and delete Magento Sync Logs past retention."""
    if not _is_magento_enabled():
        return

    job_id = "connector_sync_log_cleanup"
    if _is_job_running(job_id):
        frappe.logger("connector").info(
            "cleanup_sync_logs: cleanup job already running; skipping."
        )
        return

    try:
        frappe.enqueue(
            "connector.connector.doctype.magento_sync_log.magento_sync_log.cleanup_sync_logs",
            queue="long",
            timeout=1800,
            job_id=job_id,
            deduplicate=True,
        )
        frappe.logger("connector").info("cleanup_sync_logs: enqueued sync log cleanup job.")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: cleanup_sync_logs failed")


def sync_attribute_options():
    """Hourly: push newly-added values for already-mapped attributes (additive-only)."""
    if not _is_magento_enabled():
        return
    try:
        from connector.sync.attribute_sync import sync_all_mapped_attributes as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_attribute_options failed")


def erpnext_product_sync():
    """Every 10 minutes: push stale/unsynced ERPNext items to remote ERPNext sites."""
    if not _is_erpnext_site_sync_enabled():
        return

    if _is_job_running("erpnext_full_site_sync"):
        frappe.logger("connector").info(
            "erpnext_product_sync: existing ERPNext site sync job running; skipping."
        )
        return

    try:
        from connector.sync.erpnext_product_sync import full_erpnext_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: erpnext_product_sync failed")
