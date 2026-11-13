"""
Whitelisted API for Magento options (attribute sets, categories, product attributes).
Used by the Magento Settings form (Item Groups to Sync table) and the Item form.
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.security.permissions import require_doc_permission


ATTRIBUTE_SET_ACL_HINT = (
    "Grant the Magento integration the resource "
    "'Stores > Attributes > Attribute Set' (Magento_Catalog::sets) in "
    "Magento Admin > System > Extensions > Integrations, save, then "
    "Reauthorize the integration so the token picks up the new permission."
)


def _require_settings_read():
    # Callers are the Magento Settings form / its Item Groups rows, so the
    # user must already be able to read Magento Settings.
    require_doc_permission("Magento Settings", "read")


def describe_attribute_set_error(exc):
    """Turn a Magento error into a message that says what to fix."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return (
            f"Magento refused the attribute set request (HTTP {status}). "
            + ATTRIBUTE_SET_ACL_HINT
        )
    if status == 404:
        return (
            "Magento returned 404 for the attribute set endpoints. Check the "
            "Magento URL in Magento Settings (it must be the store's base URL)."
        )
    return f"Could not load attribute sets from Magento: {exc}"


@frappe.whitelist()
def get_magento_attribute_sets():
    """
    Return {ok, items: [{attribute_set_id, attribute_set_name}], error}.

    `ok` is False whenever Magento could not be queried, with `error` saying
    why — previously every failure was swallowed and reported as an empty
    list, which made a missing ACL resource look like "no attribute sets".
    """
    _require_settings_read()
    try:
        client = MagentoClient()
        sets = client.get_attribute_sets()
    except MagentoAPIError as e:
        frappe.log_error(f"Magento attribute sets: {e}", "Connector Magento Options")
        return {"ok": False, "error": describe_attribute_set_error(e), "items": []}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Connector Magento Options")
        return {"ok": False, "error": f"Could not load attribute sets: {e}", "items": []}

    if not sets:
        return {
            "ok": False,
            "error": "Magento answered but returned no product attribute sets.",
            "items": [],
        }
    return {"ok": True, "items": sets}


@frappe.whitelist()
def get_magento_categories():
    """
    Return list of {id, name, path, level} for Magento categories.
    Used by Item form Magento Config tab.
    """
    _require_settings_read()
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
    _require_settings_read()
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
