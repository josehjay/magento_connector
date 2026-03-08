"""
Whitelisted API for Magento options (attribute sets, categories, product attributes).
Used by the Item form to populate dropdowns in the Magento Config tab.
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError


@frappe.whitelist()
def get_magento_attribute_sets():
    """
    Return list of {attribute_set_id, attribute_set_name} for Magento product attribute sets.
    Used by Item form Magento Config tab.
    """
    try:
        client = MagentoClient()
        sets = client.get_attribute_sets()
        return {"ok": True, "items": sets}
    except MagentoAPIError as e:
        frappe.log_error(f"Magento attribute sets: {e}", "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}


@frappe.whitelist()
def get_magento_categories():
    """
    Return list of {id, name, path, level} for Magento categories.
    Used by Item form Magento Config tab.
    """
    try:
        client = MagentoClient()
        categories = client.get_categories()
        return {"ok": True, "items": categories}
    except MagentoAPIError as e:
        frappe.log_error(f"Magento categories: {e}", "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}


@frappe.whitelist()
def get_magento_product_attributes():
    """
    Return list of {attribute_code, frontend_label} for Magento product attributes.
    Used by Item form Magento Config tab (custom attributes).
    """
    try:
        client = MagentoClient()
        attrs = client.get_product_attributes()
        return {"ok": True, "items": attrs}
    except MagentoAPIError as e:
        frappe.log_error(f"Magento product attributes: {e}", "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Connector Magento Options")
        return {"ok": False, "error": str(e), "items": []}
