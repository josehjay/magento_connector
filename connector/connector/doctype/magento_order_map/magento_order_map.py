import frappe
from frappe.model.document import Document


class MagentoOrderMap(Document):
    pass


def is_order_imported(magento_order_id):
    """Return True if this Magento order has already been imported."""
    return frappe.db.exists("Magento Order Map", {"magento_order_id": magento_order_id})


def get_sales_order_for_magento_order(magento_order_id):
    """Return the ERPNext Sales Order name for a given Magento order ID."""
    return frappe.db.get_value(
        "Magento Order Map",
        {"magento_order_id": magento_order_id},
        "sales_order",
    )


def create_map(magento_order_id, magento_increment_id, magento_status, sales_order):
    """Record a new Magento → ERPNext order mapping."""
    doc = frappe.new_doc("Magento Order Map")
    doc.magento_order_id = magento_order_id
    doc.magento_increment_id = magento_increment_id
    doc.magento_status = magento_status
    doc.sales_order = sales_order
    doc.imported_on = frappe.utils.now_datetime()
    doc.insert(ignore_permissions=True)
    frappe.db.commit()


def update_status(magento_order_id, new_status):
    """Update the stored Magento status for an existing order map entry."""
    name = frappe.db.get_value(
        "Magento Order Map", {"magento_order_id": magento_order_id}, "name"
    )
    if name:
        frappe.db.set_value(
            "Magento Order Map",
            name,
            {
                "magento_status": new_status,
                "last_status_sync": frappe.utils.now_datetime(),
            },
        )
        frappe.db.commit()
