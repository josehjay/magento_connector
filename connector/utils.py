"""
Shared switches for the connector. Every sync module used to carry its own
copy of these checks; import them from here instead.
"""

import frappe


def is_magento_enabled():
    """Connector Settings → Enable Magento Integration (True if the doctype is not migrated yet)."""
    try:
        enabled = frappe.db.get_single_value("Connector Settings", "enable_magento_integration")
    except Exception:
        return True
    return True if enabled is None else bool(enabled)


def is_magento_sync_enabled():
    """Magento integration enabled AND Magento Settings → Sync Enabled."""
    if not is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


def is_erpnext_site_sync_enabled():
    """Connector Settings → Enable ERPNext Site Sync (False if not migrated yet)."""
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_erpnext_site_sync"))
    except Exception:
        return False
