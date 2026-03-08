"""
Scheduled task entry points for connector.
These functions are referenced in hooks.py scheduler_events.
Each function is a thin wrapper that imports and delegates
to the relevant sync module.
"""

import frappe


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
    try:
        from connector.sync.image_sync import sync_images as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: sync_images failed")


def full_product_sync():
    """Every hour: push all stale/unsynced ERPNext items to Magento."""
    if not _is_magento_enabled():
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
    """Every hour: push stale/unsynced ERPNext items to remote ERPNext sites."""
    if not _is_erpnext_site_sync_enabled():
        return
    try:
        from connector.sync.erpnext_product_sync import full_erpnext_product_sync as _sync
        _sync()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector Scheduled: erpnext_product_sync failed")
