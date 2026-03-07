import frappe
from frappe.model.document import Document


class MagentoProductMap(Document):
    pass


def get_magento_product_id(item_code):
    """Return the Magento product ID for a given ERPNext item_code, or None."""
    return frappe.db.get_value("Magento Product Map", item_code, "magento_product_id")


def upsert_map(item_code, magento_product_id, magento_sku=None, status="Synced"):
    """Create or update the product map entry."""
    if frappe.db.exists("Magento Product Map", item_code):
        frappe.db.set_value(
            "Magento Product Map",
            item_code,
            {
                "magento_product_id": magento_product_id,
                "magento_sku": magento_sku or item_code,
                "last_synced_on": frappe.utils.now_datetime(),
                "sync_status": status,
            },
        )
    else:
        doc = frappe.new_doc("Magento Product Map")
        doc.item_code = item_code
        doc.magento_product_id = magento_product_id
        doc.magento_sku = magento_sku or item_code
        doc.last_synced_on = frappe.utils.now_datetime()
        doc.sync_status = status
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
