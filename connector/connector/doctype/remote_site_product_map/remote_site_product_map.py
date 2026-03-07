import frappe
from frappe.model.document import Document


class RemoteSiteProductMap(Document):
    pass


def get_map(remote_site, item_code):
    """Return the Remote Site Product Map name for a given site + item_code, or None."""
    return frappe.db.get_value(
        "Remote Site Product Map",
        {"remote_site": remote_site, "item_code": item_code},
        "name",
    )


def get_remote_item_code(remote_site, item_code):
    """Return the remote_item_code for a given site + local item_code, or None."""
    return frappe.db.get_value(
        "Remote Site Product Map",
        {"remote_site": remote_site, "item_code": item_code},
        "remote_item_code",
    )


def upsert_map(remote_site, item_code, remote_item_code=None, status="Synced", error=""):
    """Create or update the remote site product map entry."""
    existing = get_map(remote_site, item_code)

    if existing:
        frappe.db.set_value(
            "Remote Site Product Map",
            existing,
            {
                "remote_item_code": remote_item_code or item_code,
                "last_synced_on": frappe.utils.now_datetime(),
                "sync_status": status,
                "last_error": error[:500] if error else "",
            },
        )
    else:
        doc = frappe.new_doc("Remote Site Product Map")
        doc.remote_site = remote_site
        doc.item_code = item_code
        doc.remote_item_code = remote_item_code or item_code
        doc.last_synced_on = frappe.utils.now_datetime()
        doc.sync_status = status
        doc.last_error = error[:500] if error else ""
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
