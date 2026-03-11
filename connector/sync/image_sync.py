"""
Image Sync: Magento Base Product Image URL → ERPNext Item image field

Scheduled every 30 minutes via tasks.py.
Fetches the base image for each synced product and saves its URL
into the ERPNext Item's image field (no file download — URL only).
Uses 'image' or 'item_image' depending on what exists on Item.
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)

# Item image field: ERPNext standard is "image"; some setups use "item_image"
def _get_item_image_field():
    meta = frappe.get_meta("Item")
    if meta.has_field("item_image"):
        return "item_image"
    if meta.has_field("image"):
        return "image"
    return None


def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def sync_images():
    """
    Scheduled entry point.
    For all items with a Magento product map, fetch the base image URL and
    store it in ERPNext Item image field (image or item_image).
    """
    if not _is_magento_enabled():
        return

    sync_enabled = frappe.db.get_single_value("Magento Settings", "sync_enabled")
    if not sync_enabled:
        return

    image_field = _get_item_image_field()
    if not image_field:
        frappe.logger("connector").warning(
            "sync_images: Item has no 'image' or 'item_image' field; skipping."
        )
        return

    magento_url = frappe.db.get_single_value("Magento Settings", "magento_url")
    if not magento_url:
        return
    magento_url = magento_url.rstrip("/")

    # Order by last_synced_on ascending (nulls first) so items that haven't had
    # their image checked recently are processed first, cycling through all items
    # across successive runs instead of always re-checking the same first N.
    mapped = frappe.get_all(
        "Magento Product Map",
        filters={"sync_status": "Synced"},
        fields=["item_code", "magento_sku"],
        order_by="last_synced_on asc",
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
    no_media = 0
    failed = 0
    # Commit every N updates to avoid holding the DB connection for the whole run.
    COMMIT_EVERY = 25
    # Limit items per run. Order by last_synced_on so we rotate through all items
    # over successive runs rather than always processing the same first N.
    MAX_ITEMS_PER_RUN = 200

    logger = frappe.logger("connector")
    logger.info(f"sync_images: starting, {len(mapped)} mapped items (processing up to {MAX_ITEMS_PER_RUN}).")

    for row in mapped[:MAX_ITEMS_PER_RUN]:
        item_code = row["item_code"]
        sku = row["magento_sku"] or item_code

        try:
            media_entries = client.get_product_media(sku)
        except MagentoAPIError as e:
            failed += 1
            logger.warning(f"sync_images: Magento API error for {item_code} (sku={sku}): {e}")
            continue
        except Exception as e:
            failed += 1
            frappe.log_error(frappe.get_traceback(), f"Magento Image Sync Error: {item_code}")
            continue

        if not media_entries:
            no_media += 1
            logger.debug(f"sync_images: no media entries in Magento for {item_code} (sku={sku}).")
            continue

        base_image_url = _extract_base_image_url(media_entries, magento_url)

        if not base_image_url:
            no_media += 1
            logger.debug(f"sync_images: media entries found but no base image type for {item_code}.")
            continue

        try:
            current_image = frappe.db.get_value("Item", item_code, image_field)
        except Exception as e:
            failed += 1
            frappe.log_error(str(e), f"sync_images: get_value failed for {item_code}")
            continue

        if current_image == base_image_url:
            skipped += 1
            continue

        try:
            # update_modified=False: image sync must not mark the item as stale for
            # product sync (the image URL is not part of the Magento product payload).
            frappe.db.set_value(
                "Item", item_code, image_field, base_image_url,
                update_modified=False,
            )
            updated += 1
            logger.debug(f"sync_images: updated image for {item_code} → {base_image_url}")
        except Exception as e:
            failed += 1
            frappe.log_error(str(e), f"sync_images: set_value failed for {item_code} field={image_field}")
            continue

        if updated % COMMIT_EVERY == 0:
            frappe.db.commit()

    if updated:
        frappe.db.commit()

    logger.info(
        f"sync_images: done — {updated} updated, {skipped} already current, "
        f"{no_media} no media in Magento, {failed} errors."
    )

    create_log(
        operation="Image Sync",
        status="Success" if not failed else "Failed",
        response_payload={
            "updated": updated,
            "skipped_already_current": skipped,
            "no_media_in_magento": no_media,
            "failed": failed,
            "total_processed": min(len(mapped), MAX_ITEMS_PER_RUN),
        },
    )
    frappe.db.commit()


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
