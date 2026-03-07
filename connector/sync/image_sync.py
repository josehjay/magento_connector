"""
Image Sync: Magento Base Product Image URL → ERPNext Item.item_image

Scheduled every 30 minutes via tasks.py.
Fetches the base image for each synced product and saves its URL
into the ERPNext Item's item_image field (no file download — URL only).
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)


def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def sync_images():
    """
    Scheduled entry point.
    For all items with a Magento product map, fetch the base image URL and
    store it in ERPNext Item.item_image.
    """
    if not _is_magento_enabled():
        return

    sync_enabled = frappe.db.get_single_value("Magento Settings", "sync_enabled")
    if not sync_enabled:
        return

    magento_url = frappe.db.get_single_value("Magento Settings", "magento_url").rstrip("/")

    mapped = frappe.get_all(
        "Magento Product Map",
        filters={"sync_status": "Synced"},
        fields=["item_code", "magento_sku"],
    )

    if not mapped:
        return

    try:
        client = MagentoClient()
    except Exception as e:
        frappe.log_error(str(e), "Magento Image Sync: Client Init Failed")
        return

    updated = 0
    skipped = 0
    failed = 0

    for row in mapped:
        item_code = row["item_code"]
        sku = row["magento_sku"] or item_code

        try:
            media_entries = client.get_product_media(sku)
        except MagentoAPIError as e:
            failed += 1
            frappe.logger("connector").warning(
                f"Image sync failed for {item_code}: {e}"
            )
            continue
        except Exception as e:
            failed += 1
            frappe.log_error(str(e), f"Magento Image Sync Error: {item_code}")
            continue

        base_image_url = _extract_base_image_url(media_entries, magento_url)

        if not base_image_url:
            skipped += 1
            continue

        current_image = frappe.db.get_value("Item", item_code, "item_image")
        if current_image == base_image_url:
            skipped += 1
            continue

        frappe.db.set_value("Item", item_code, "item_image", base_image_url)
        updated += 1

    if updated:
        frappe.db.commit()

    frappe.logger("connector").info(
        f"sync_images: {updated} updated, {skipped} skipped, {failed} failed."
    )

    create_log(
        operation="Image Sync",
        status="Success" if not failed else "Failed",
        response_payload={
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        },
    )


def _extract_base_image_url(media_entries, magento_url):
    """
    Given a list of Magento media gallery entries, find the one with type 'image'
    (base image) and construct its full URL.

    Magento media path format: /catalog/product/x/x/filename.jpg
    Full URL: {magento_url}/media/catalog/product{file_path}
    """
    if not media_entries:
        return None

    for entry in media_entries:
        types = entry.get("types") or []
        if "image" in types:
            file_path = entry.get("file") or ""
            if file_path:
                return f"{magento_url}/media/catalog/product{file_path}"

    first = media_entries[0] if media_entries else None
    if first:
        file_path = first.get("file") or ""
        if file_path:
            return f"{magento_url}/media/catalog/product{file_path}"

    return None
