"""
Image Sync: Magento Base Product Image URL → ERPNext Item image field

Scheduled every 30 minutes via tasks.py (enqueued to long queue).
Fetches the base image for each synced product and saves its URL
into the ERPNext Item's image field (no file download — URL only).
Uses 'image' or 'item_image' depending on what exists on Item.
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError, MEDIA_BASE_URL_LAST_KEY
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)
from urllib.parse import urlparse
from connector.security.request_signing import verify_incoming_signed_request
from connector.security.permissions import require_admin, require_push_endpoint_access
from connector.utils import is_magento_sync_enabled


def _get_item_image_field():
    meta = frappe.get_meta("Item")
    if meta.has_field("item_image"):
        return "item_image"
    if meta.has_field("image"):
        return "image"
    return None


# Kept as a module-level name: tests patch it.
_is_sync_enabled = is_magento_sync_enabled


def _is_safe_image_url(image_url, expected_magento_url, media_base_url=None):
    """
    Accept only http(s) URLs (https unless localhost) whose host is the
    Magento host or, when media is served from a CDN / cloud storage, the
    host of Magento's media base URL.
    """
    image_url = (image_url or "").strip()
    if not image_url or len(image_url) > 2048:
        return False

    parsed = urlparse(image_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    allowed_hosts = set()
    for base in (expected_magento_url, media_base_url):
        host = urlparse((base or "").strip()).hostname
        if host:
            allowed_hosts.add(host.lower())
    if allowed_hosts and parsed.hostname:
        if parsed.hostname.lower() not in allowed_hosts:
            return False

    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1")
    return parsed.scheme == "https" or is_local


@frappe.whitelist()
def run_image_sync_now():
    """
    Whitelisted entry point for running image sync directly (not enqueued).
    Use from Magento Settings "Sync Images Now" button or bench execute.
    Runs synchronously in the current process. System Manager only.
    """
    require_admin("Only System Manager can run image sync.")
    frappe.logger("connector").info("run_image_sync_now: starting direct image sync.")
    result = sync_images()
    frappe.logger("connector").info(f"run_image_sync_now: done. result={result}")
    return result


@frappe.whitelist()
def receive_image_update(sku, image_url):
    """
    Push endpoint: the Magento Kitabu_ErpNextConnector module calls this
    whenever a product's base image is saved.  Updates the linked ERPNext
    Item image field immediately without a full image sync run.
    """
    logger = frappe.logger("connector")
    require_push_endpoint_access("receive_image_update")
    verify_incoming_signed_request("receive_image_update")

    if not _is_sync_enabled():
        logger.info("receive_image_update: skipped — sync is disabled.")
        return {"ok": False, "reason": "sync_disabled"}

    sku = (sku or "").strip()
    if not sku or len(sku) > 255:
        logger.warning("receive_image_update: invalid sku payload.")
        return {"ok": False, "reason": "invalid_sku"}

    magento_url = (frappe.db.get_single_value("Magento Settings", "magento_url") or "").strip().rstrip("/")
    if not _is_safe_image_url(image_url, magento_url, _get_media_base_url_for_validation(image_url, magento_url)):
        logger.warning(f"receive_image_update: rejected unsafe image_url for SKU '{sku}'.")
        return {"ok": False, "reason": "invalid_image_url"}

    image_field = _get_item_image_field()
    if not image_field:
        logger.warning("receive_image_update: no image field found on Item doctype.")
        return {"ok": False, "reason": "no_image_field"}

    item_code = frappe.db.get_value(
        "Magento Product Map", {"magento_sku": sku}, "item_code"
    )
    if not item_code:
        logger.warning(f"receive_image_update: SKU '{sku}' not found in Magento Product Map.")
        return {"ok": False, "reason": "sku_not_mapped"}

    current = frappe.db.get_value("Item", item_code, image_field)
    if current == image_url:
        logger.info(f"receive_image_update: [{sku}] image already current — no update needed.")
        return {"ok": True, "result": "already_current"}

    frappe.db.set_value("Item", item_code, image_field, image_url, update_modified=False)
    frappe.db.commit()
    logger.info(f"receive_image_update: [{sku}] image updated → {image_url}")
    return {"ok": True, "item_code": item_code}


def sync_images():
    """
    Main image sync logic. For all items with a Magento product map,
    fetch the base image URL from Magento and store it in the ERPNext
    Item's image field.

    Returns a dict summarising the run.
    """
    logger = frappe.logger("connector")

    if not _is_sync_enabled():
        logger.info("sync_images: skipped — sync is disabled.")
        return {"status": "skipped", "reason": "sync_disabled"}

    image_field = _get_item_image_field()
    if not image_field:
        logger.warning("sync_images: Item has no 'image' or 'item_image' field; skipping.")
        return {"status": "skipped", "reason": "no_image_field"}

    magento_url = frappe.db.get_single_value("Magento Settings", "magento_url")
    if not magento_url:
        logger.warning("sync_images: magento_url is not set in Magento Settings; skipping.")
        return {"status": "skipped", "reason": "no_magento_url"}
    magento_url = magento_url.rstrip("/")

    # Ordered by item_code and walked with a cursor: this job never updates
    # last_synced_on, so ordering by it re-processed the same first
    # MAX_ITEMS_PER_RUN items every run and the rest never got images.
    mapped = frappe.get_all(
        "Magento Product Map",
        filters={"sync_status": "Synced"},
        fields=["item_code", "magento_sku"],
        order_by="item_code asc",
    )

    if not mapped:
        logger.info("sync_images: no synced items in Magento Product Map; nothing to process.")
        return {"status": "skipped", "reason": "no_synced_items"}

    try:
        client = MagentoClient()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Magento Image Sync: Client Init Failed")
        return {"status": "error", "reason": f"client_init_failed: {e}"}

    # Resolved once per run (and cached 6h by the client); None → legacy
    # {magento_url}/media/ URLs.
    media_base_url = _safe_media_base_url(client)

    updated = 0
    skipped = 0
    no_media = 0
    failed = 0
    COMMIT_EVERY = 25
    MAX_ITEMS_PER_RUN = 200

    batch = _next_image_batch(mapped, MAX_ITEMS_PER_RUN)
    logger.info(
        f"sync_images: starting — {len(mapped)} mapped items, "
        f"processing {len(batch)} from cursor {batch[0]['item_code']!r}, image_field={image_field}."
    )

    for row in batch:
        item_code = row["item_code"]
        sku = row["magento_sku"] or item_code

        try:
            media_entries = client.get_product_media(sku)
        except MagentoAPIError as e:
            failed += 1
            if failed <= 5:
                logger.warning(f"sync_images: Magento API error for {item_code} (sku={sku}): {e}")
            continue
        except Exception as e:
            failed += 1
            frappe.log_error(frappe.get_traceback(), f"Magento Image Sync Error: {item_code}")
            continue

        if not media_entries:
            no_media += 1
            if no_media <= 3:
                logger.info(f"sync_images: no media entries for {item_code} (sku={sku}). "
                            "Check Magento admin → Catalog → Products → Images and Videos.")
            continue

        base_image_url = _extract_base_image_url(media_entries, magento_url, media_base_url)

        if not base_image_url:
            no_media += 1
            if no_media <= 3:
                logger.info(
                    f"sync_images: media entries exist but no base image for {item_code}. "
                    f"types found: {[e.get('types') for e in media_entries[:3]]}"
                )
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
            frappe.db.set_value(
                "Item", item_code, image_field, base_image_url,
                update_modified=False,
            )
            updated += 1
            if updated <= 3:
                logger.info(f"sync_images: updated {item_code} → {base_image_url}")
        except Exception as e:
            failed += 1
            frappe.log_error(str(e), f"sync_images: set_value failed for {item_code} field={image_field}")
            continue

        if updated % COMMIT_EVERY == 0:
            frappe.db.commit()

    if updated:
        frappe.db.commit()

    _save_image_cursor(batch[-1]["item_code"])

    summary = {
        "updated": updated,
        "skipped_already_current": skipped,
        "no_media_in_magento": no_media,
        "failed": failed,
        "total_processed": len(batch),
        "total_mapped": len(mapped),
    }

    logger.info(f"sync_images: done — {summary}")

    create_log(
        operation="Image Sync",
        status="Success" if not failed else "Failed",
        response_payload=summary,
    )
    frappe.db.commit()

    return summary


IMAGE_SYNC_CURSOR_KEY = "connector:image_sync:cursor"


def _next_image_batch(mapped, size):
    """
    Return up to `size` rows starting after the saved cursor (rows are sorted
    by item_code), wrapping around to the start so every mapped item is
    visited over successive runs.
    """
    if len(mapped) <= size:
        return list(mapped)
    try:
        cursor = frappe.cache().get_value(IMAGE_SYNC_CURSOR_KEY) or ""
    except Exception:
        cursor = ""
    start = 0
    if cursor:
        codes = [row["item_code"] for row in mapped]
        if cursor in codes:
            # Exact match first: DB collation order can differ from Python's.
            start = (codes.index(cursor) + 1) % len(codes)
        else:
            start = next((i for i, code in enumerate(codes) if code > cursor), 0)
    rotated = mapped[start:] + mapped[:start]
    return rotated[:size]


def _save_image_cursor(item_code):
    try:
        frappe.cache().set_value(IMAGE_SYNC_CURSOR_KEY, item_code)
    except Exception:
        pass


def _safe_media_base_url(client):
    """client.get_media_base_url(), never raising (None on any failure)."""
    try:
        return client.get_media_base_url()
    except Exception:
        return None


def _get_cached_media_base_url():
    """Last media base URL a MagentoClient resolved (cache only, no API call)."""
    try:
        return frappe.cache().get_value(MEDIA_BASE_URL_LAST_KEY) or None
    except Exception:
        return None


def _get_media_base_url_for_validation(image_url, magento_url):
    """
    Media base URL to trust in receive_image_update. Uses the cached value
    when available; only asks Magento when the pushed URL is on a foreign
    host (so the common same-host push costs no API call).
    """
    cached = _get_cached_media_base_url()
    if cached:
        return cached
    image_host = (urlparse((image_url or "").strip()).hostname or "").lower()
    magento_host = (urlparse(magento_url or "").hostname or "").lower()
    if not image_host or image_host == magento_host:
        return None
    try:
        return MagentoClient().get_media_base_url()
    except Exception:
        return None


def _build_media_url(file_path, magento_url, media_base_url):
    if media_base_url:
        return f"{media_base_url.rstrip('/')}/catalog/product/{file_path.lstrip('/')}"
    return f"{(magento_url or '').rstrip('/')}/media/catalog/product/{file_path.lstrip('/')}"


def _extract_base_image_url(media_entries, magento_url, media_base_url=None):
    """
    Given a list of Magento media gallery entries, find the one with type 'image'
    (base image) and construct its full URL.

    Magento media path format: /x/x/filename.jpg (relative to catalog/product)
    Full URL: {media_base_url}catalog/product{file_path}

    media_base_url comes from MagentoClient.get_media_base_url() (CDN / cloud
    storage aware). When the caller does not pass it, the last value any
    client resolved is taken from cache; failing that, the legacy
    {magento_url}/media/ base is used.
    """
    if not media_entries:
        return None

    if not media_base_url:
        media_base_url = _get_cached_media_base_url()

    for entry in media_entries:
        types = entry.get("types") or []
        if "image" in types:
            file_path = entry.get("file") or ""
            if file_path:
                return _build_media_url(file_path, magento_url, media_base_url)

    first = media_entries[0] if media_entries else None
    if first:
        file_path = first.get("file") or ""
        if file_path:
            return _build_media_url(file_path, magento_url, media_base_url)

    return None
