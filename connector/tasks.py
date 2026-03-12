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
    """Every 15 minutes: push stock quantities to Magento."""
    if not _is_magento_enabled():
        return
    try:
        from connector.sync.inventory_sync import sync_inventory as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_inventory failed")


def sync_orders():
    """Every 10 minutes: pull new/updated Magento orders into ERPNext."""
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
    """Daily: push all stale/unsynced ERPNext items to Magento.

    Skips if a full-sync chunk or retry job is already running.
    """
    if not _is_magento_enabled():
        return

    if _is_job_running("magento_full_product_sync") or _is_job_running("magento_retry_failed_sync"):
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
    """Every 30 minutes: retry products that failed their last sync (exponential backoff)."""
    if not _is_magento_enabled():
        return
    try:
        from connector.sync.product_sync import retry_failed_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: retry_failed_product_sync failed")


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
