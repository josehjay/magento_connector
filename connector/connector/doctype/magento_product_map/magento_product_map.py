import frappe
from frappe.model.document import Document


class MagentoProductMap(Document):
    pass


def get_magento_product_id(item_code):
    """Return the Magento product ID for a given ERPNext item_code, or None."""
    return frappe.db.get_value("Magento Product Map", item_code, "magento_product_id")


def upsert_map(
    item_code,
    magento_product_id,
    magento_sku=None,
    status="Synced",
    retry_count=None,
    last_failed_at=None,
):
    """
    Create or update the product map entry.

    On success, pass status="Synced" and retry_count=0 to reset the failure counter.
    On failure, pass status="Failed", retry_count=<incremented value>, and last_failed_at=now.
    """
    fields = {
        "magento_product_id": magento_product_id,
        "magento_sku": magento_sku or item_code,
        "last_synced_on": frappe.utils.now_datetime(),
        "sync_status": status,
    }
    if retry_count is not None:
        fields["retry_count"] = retry_count
    if last_failed_at is not None:
        fields["last_failed_at"] = last_failed_at

    if frappe.db.exists("Magento Product Map", item_code):
        frappe.db.set_value("Magento Product Map", item_code, fields)
    else:
        doc = frappe.new_doc("Magento Product Map")
        doc.item_code = item_code
        doc.update(fields)
        doc.insert(ignore_permissions=True)
    frappe.db.commit()


def delete_map(item_code):
    """Remove the product map entry for the given item (when Sync to Magento is unchecked)."""
    if frappe.db.exists("Magento Product Map", item_code):
        frappe.delete_doc("Magento Product Map", item_code, ignore_permissions=True)
        frappe.db.commit()
