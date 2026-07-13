import frappe
from frappe.model.document import Document


class MagentoAttributeMapping(Document):
    pass


def get_magento_attribute_code(item_attribute):
    """Return the mapped Magento attribute_code for an ERPNext Item Attribute, or None if unmapped."""
    return frappe.db.get_value("Magento Attribute Mapping", item_attribute, "magento_attribute_code")


def get_map(item_attribute):
    """Return the full mapping row for an ERPNext Item Attribute as a dict, or None."""
    return frappe.db.get_value(
        "Magento Attribute Mapping",
        item_attribute,
        [
            "magento_attribute_code",
            "magento_attribute_id",
            "frontend_label",
            "mapping_type",
            "status",
            "last_synced_on",
            "sync_error",
        ],
        as_dict=True,
    )


def upsert_map(
    item_attribute,
    magento_attribute_code,
    magento_attribute_id=None,
    frontend_label=None,
    mapping_type=None,
    status="Mapped",
    sync_error="",
):
    """
    Create or update the attribute mapping entry.

    Only fields explicitly passed are updated — callers doing an options-only
    sync typically only pass `status`/`sync_error` and leave the rest as-is.
    """
    fields = {
        "magento_attribute_code": magento_attribute_code,
        "last_synced_on": frappe.utils.now_datetime(),
        "status": status,
        "sync_error": sync_error or "",
    }
    if magento_attribute_id is not None:
        fields["magento_attribute_id"] = magento_attribute_id
    if frontend_label is not None:
        fields["frontend_label"] = frontend_label
    if mapping_type is not None:
        fields["mapping_type"] = mapping_type

    if frappe.db.exists("Magento Attribute Mapping", item_attribute):
        frappe.db.set_value("Magento Attribute Mapping", item_attribute, fields)
    else:
        doc = frappe.new_doc("Magento Attribute Mapping")
        doc.item_attribute = item_attribute
        doc.update(fields)
        doc.insert(ignore_permissions=True)
    frappe.db.commit()


def delete_map(item_attribute):
    """Remove the attribute mapping entry (unmap), e.g. so it can be recreated."""
    if frappe.db.exists("Magento Attribute Mapping", item_attribute):
        frappe.delete_doc("Magento Attribute Mapping", item_attribute, ignore_permissions=True)
        frappe.db.commit()
