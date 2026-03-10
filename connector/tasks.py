"""
Scheduled task entry points for connector.
These functions are referenced in hooks.py scheduler_events.
Each function is a thin wrapper that imports and delegates
to the relevant sync module.
"""

import frappe
from frappe.utils.background_jobs import get_jobs


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


def _has_pending_jobs(prefix, queue_name="long"):
    """Return True if any queued/running job in the given queue has a job_name starting with prefix."""
    try:
        jobs = get_jobs(site=frappe.local.site) or {}
    except Exception:
        return False
    for job in jobs.get(queue_name, []):
        name = (job.get("job_name") or "")
        if name.startswith(prefix):
            return True
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
        return
    try:
        from connector.sync.order_sync import sync_orders as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_orders failed")


def sync_images():
    """Every 30 minutes: pull Magento base image URLs into ERPNext item_image."""
    if not _is_magento_enabled():
        return
    # Avoid hitting the scheduler's 300s timeout by enqueueing a separate
    # long-queue job with a higher timeout, and deduplicate via job_name.
    try:
        if _has_pending_jobs("connector_image_sync"):
            frappe.logger("connector").info(
                "sync_images: image sync job already queued/running; skipping this run.",
            )
            return
        frappe.enqueue(
            "connector.sync.image_sync.sync_images",
            queue="long",
            timeout=900,
            job_name="connector_image_sync",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_images failed")


def full_product_sync():
    """Every 10 minutes: push all stale/unsynced ERPNext items to Magento.

    Skips if there are still long-queue batch jobs for a previous full/retry
    run (job_name starts with \"magento_full_sync_batch\" or \"magento_retry_batch\").
    """
    if not _is_magento_enabled():
        return

    if _has_pending_jobs("magento_full_product_sync") or _has_pending_jobs("magento_retry_failed_sync"):
        frappe.logger("connector").info(
            "full_product_sync: existing Magento product sync job in queue; skipping this run.",
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
    """Every 10 minutes: push stale/unsynced ERPNext items to remote ERPNext sites.

    Skips if there are still long-queue ERPNext site sync jobs in the queue
    (job_name starts with \"erpnext_sync_\").
    """
    if not _is_erpnext_site_sync_enabled():
        return

    if _has_pending_jobs("erpnext_sync_"):
        frappe.logger("connector").info(
            "erpnext_product_sync: existing ERPNext site sync jobs in queue; skipping this run.",
        )
        return

    try:
        from connector.sync.erpnext_product_sync import full_erpnext_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: erpnext_product_sync failed")
