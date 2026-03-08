"""
Product Sync: ERPNext Item → Magento Product

Triggered by:
  - Item.after_insert / Item.on_update  (real-time, deduplicated by job_name)
  - tasks.full_product_sync()           (hourly catch-up, scheduled)
  - tasks.retry_failed_product_sync()   (every 30 min, retries failed items with backoff)

Retry strategy (exponential backoff):
  retry_count 1 → wait  5 min before retry
  retry_count 2 → wait 10 min
  retry_count 3 → wait 20 min
  retry_count 4 → wait 40 min
  retry_count 5+ → wait 60 min (capped)
  retry_count > MAX_RETRIES → item is skipped until manually triggered or item is re-saved
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_product_map.magento_product_map import (
    get_magento_product_id,
    upsert_map,
    delete_map,
)
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)

# Items that have failed more than this many times are not retried automatically.
# They are only retried when the item is explicitly saved or manually triggered.
MAX_RETRIES = 10

# Number of items per batch job sent to the `long` queue.
BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def _is_sync_enabled():
    if not _is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


def _get_allowed_item_groups():
    settings = frappe.get_single("Magento Settings")
    return {row.item_group for row in (settings.magento_item_groups or [])}


def _is_item_group_allowed(item_group):
    allowed = _get_allowed_item_groups()
    if not allowed:
        return True
    return item_group in allowed


def _get_item_price(item_code, price_list):
    price = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        "price_list_rate",
    )
    return float(price) if price else 0.0


def _get_attribute_set_for_item_group(item_group):
    """
    Return Magento attribute_set_id for the given Item Group from Magento Settings.
    Falls back to 4 (Magento default) if not configured.
    """
    settings = frappe.get_single("Magento Settings")
    for row in settings.magento_item_groups or []:
        if row.item_group == item_group and row.get("attribute_set_id"):
            try:
                return int(row.attribute_set_id)
            except (TypeError, ValueError):
                pass
    return 4


def _backoff_minutes(retry_count):
    """Return minutes to wait before retrying. Capped at 60 minutes."""
    if retry_count <= 0:
        return 0
    return min(5 * (2 ** (retry_count - 1)), 60)


def _build_product_payload(doc):
    """
    Convert an ERPNext Item doc into a Magento product payload dict.
    Attribute set comes from Magento Settings → Item Group mapping.
    Stock is managed separately by inventory_sync.py.
    Disabled items sync as status=2 (Disabled) in Magento.
    """
    settings = frappe.get_single("Magento Settings")
    price = _get_item_price(doc.item_code, settings.price_list)
    description = doc.description or doc.item_name or ""
    attribute_set_id = _get_attribute_set_for_item_group(doc.item_group or "")

    status = 2 if doc.get("disabled") else (1 if doc.is_sales_item else 2)

    payload = {
        "sku": doc.item_code,
        "name": doc.item_name,
        "price": price,
        "status": status,
        "visibility": 4,
        "type_id": "simple",
        "attribute_set_id": attribute_set_id,
        "custom_attributes": [
            {"attribute_code": "description", "value": description},
            {"attribute_code": "short_description", "value": description[:255]},
        ],
        "extension_attributes": {
            "stock_item": {
                "manage_stock": True,
                "qty": 0,
                "is_in_stock": False,
            }
        },
    }

    if doc.get("weight_per_unit") and doc.weight_per_unit:
        payload["weight"] = float(doc.weight_per_unit)

    return payload


# ---------------------------------------------------------------------------
# Doc event hook (real-time, called on every Item save)
# ---------------------------------------------------------------------------

def on_item_save(doc, method):
    """
    Hook: Item after_insert / on_update.
    - Deselected sync_to_magento → remove from Magento and map.
    - Enabled sync_to_magento + allowed group → enqueue push (deduplicated).

    Uses enqueue_after_commit=True so the job always sees committed data,
    and job_name to prevent duplicate queue entries for the same item.
    """
    if not _is_sync_enabled():
        return

    if not doc.get("sync_to_magento"):
        if get_magento_product_id(doc.item_code):
            frappe.enqueue(
                "connector.sync.product_sync.remove_from_magento",
                queue="default",
                timeout=60,
                job_name=f"magento_remove_{doc.item_code}",
                enqueue_after_commit=True,
                item_code=doc.item_code,
            )
        return

    if not _is_item_group_allowed(doc.item_group):
        return

    frappe.enqueue(
        "connector.sync.product_sync.push_item_to_magento",
        queue="default",
        timeout=120,
        job_name=f"magento_product_sync_{doc.item_code}",
        enqueue_after_commit=True,
        item_code=doc.item_code,
    )


# ---------------------------------------------------------------------------
# Remove product from Magento
# ---------------------------------------------------------------------------

def remove_from_magento(item_code):
    """
    When user deselects Sync to Magento: disable the product in Magento,
    delete the map entry, and clear the Magento fields on the Item doc.
    """
    magento_id = get_magento_product_id(item_code)
    if not magento_id:
        return
    try:
        client = MagentoClient()
        try:
            client.delete_product(item_code)
        except MagentoAPIError as e:
            if e.status_code == 404:
                pass  # already gone
            else:
                client.update_product(item_code, {"status": 2})
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Connector: Remove from Magento")

    delete_map(item_code)
    frappe.db.set_value(
        "Item",
        item_code,
        {
            "magento_product_id": None,
            "magento_last_synced_on": None,
            "magento_sync_error": "",
        },
    )
    frappe.db.commit()
    create_log(
        operation="Remove from Magento",
        status="Success",
        doctype_name="Item",
        document_name=item_code,
        magento_id=magento_id,
    )


# ---------------------------------------------------------------------------
# Single-item push (called directly or from a batch job)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def push_item_to_magento(item_code):
    """
    Push a single ERPNext Item to Magento.
    On success: resets the retry counter.
    On failure: increments retry counter and records last_failed_at for backoff.
    """
    if not _is_sync_enabled():
        return

    doc = frappe.get_doc("Item", item_code)

    if not doc.get("sync_to_magento"):
        return

    if not _is_item_group_allowed(doc.item_group):
        return

    payload = _build_product_payload(doc)
    existing_magento_id = get_magento_product_id(item_code)

    try:
        client = MagentoClient()

        if existing_magento_id:
            result = client.update_product(item_code, payload)
        elif client.product_exists(item_code):
            result = client.update_product(item_code, payload)
        else:
            result = client.create_product(payload)

        magento_product_id = result.get("id")

        # Success — persist map entry with reset retry counter
        upsert_map(
            item_code,
            magento_product_id,
            item_code,
            status="Synced",
            retry_count=0,
            last_failed_at=None,
        )

        frappe.db.set_value(
            "Item",
            item_code,
            {
                "magento_product_id": magento_product_id,
                "magento_last_synced_on": frappe.utils.now_datetime(),
                "magento_sync_error": "",
            },
        )
        frappe.db.commit()

        create_log(
            operation="Product Push",
            status="Success",
            doctype_name="Item",
            document_name=item_code,
            magento_id=magento_product_id,
            request_payload=payload,
            response_payload=result,
        )

    except (MagentoAPIError, Exception) as e:
        _handle_push_failure(item_code, e, payload)


def _handle_push_failure(item_code, exc, payload=None):
    """
    Record a failed sync attempt. Increments retry_count and sets last_failed_at
    so the retry scheduler can calculate the correct backoff window.
    """
    error_msg = str(exc)
    is_api_error = isinstance(exc, MagentoAPIError)

    if not is_api_error:
        frappe.log_error(frappe.get_traceback(), "Magento Product Sync Error")

    # Read current retry count from map (may not exist yet for first-time failures)
    current = frappe.db.get_value(
        "Magento Product Map",
        item_code,
        ["retry_count", "magento_product_id"],
        as_dict=True,
    ) or {}
    new_retry_count = (current.get("retry_count") or 0) + 1
    now = frappe.utils.now_datetime()

    upsert_map(
        item_code,
        current.get("magento_product_id") or 0,
        item_code,
        status="Failed",
        retry_count=new_retry_count,
        last_failed_at=now,
    )

    frappe.db.set_value("Item", item_code, "magento_sync_error", error_msg[:500])
    frappe.db.commit()

    create_log(
        operation="Product Push",
        status="Failed",
        doctype_name="Item",
        document_name=item_code,
        error_message=error_msg,
        request_payload=payload,
    )


# ---------------------------------------------------------------------------
# Batch processor (called by both full_product_sync and retry_failed_product_sync)
# ---------------------------------------------------------------------------

def _run_batch_product_sync(item_codes):
    """
    Process a list of item_codes sequentially within a single background job.
    Each item failure is isolated — one bad item cannot stop the rest.
    """
    logger = frappe.logger("connector")
    success = failed = 0
    for item_code in item_codes:
        try:
            push_item_to_magento(item_code)
            success += 1
        except Exception as e:
            failed += 1
            frappe.log_error(
                f"Batch sync failed for {item_code}: {e}",
                "Connector Product Sync Batch",
            )
    logger.info(f"_run_batch_product_sync: {success} ok, {failed} failed out of {len(item_codes)}")


# ---------------------------------------------------------------------------
# Scheduled: full catch-up sync (hourly)
# ---------------------------------------------------------------------------

def full_product_sync():
    """
    Hourly catch-up: find all Items that are stale or have never been synced
    and dispatch them in BATCH_SIZE chunks to the `long` queue.

    Deduplication: each batch job uses a stable job_name so re-runs of
    full_product_sync cannot pile up duplicate jobs in the queue.
    """
    if not _is_sync_enabled():
        return

    filters = {"sync_to_magento": 1}
    allowed_groups = _get_allowed_item_groups()
    if allowed_groups:
        filters["item_group"] = ["in", list(allowed_groups)]

    items = frappe.get_all(
        "Item",
        filters=filters,
        fields=["item_code", "modified", "magento_last_synced_on"],
    )

    to_sync = [
        item["item_code"]
        for item in items
        if not item.get("magento_last_synced_on")
        or (item.get("modified") and item["magento_last_synced_on"] < item["modified"])
    ]

    if not to_sync:
        frappe.logger("connector").info("full_product_sync: nothing stale to sync.")
        return

    _dispatch_batches(to_sync, job_prefix="magento_full_sync_batch")
    frappe.logger("connector").info(
        f"full_product_sync: dispatched {len(to_sync)} items in batches of {BATCH_SIZE}."
    )


# ---------------------------------------------------------------------------
# Scheduled: retry failed products (every 30 minutes)
# ---------------------------------------------------------------------------

def retry_failed_product_sync():
    """
    Retry products that have a 'Failed' map entry and whose exponential backoff
    window has expired. Items that have exceeded MAX_RETRIES are skipped until
    they are explicitly re-saved or manually triggered.
    """
    if not _is_sync_enabled():
        return

    failed_maps = frappe.get_all(
        "Magento Product Map",
        filters={"sync_status": "Failed"},
        fields=["item_code", "retry_count", "last_failed_at"],
    )

    if not failed_maps:
        return

    now = frappe.utils.now_datetime()
    due = []

    for m in failed_maps:
        retry_count = m.get("retry_count") or 0

        if retry_count > MAX_RETRIES:
            continue  # exhausted — wait for a manual trigger

        last_failed = m.get("last_failed_at")
        if last_failed:
            wait = _backoff_minutes(retry_count)
            next_retry = frappe.utils.add_to_date(last_failed, minutes=wait)
            if now < next_retry:
                continue  # still within the backoff window

        due.append(m["item_code"])

    if not due:
        return

    # Only retry items that still want to be synced
    valid = set(
        frappe.get_all(
            "Item",
            filters={"item_code": ["in", due], "sync_to_magento": 1},
            pluck="item_code",
        )
    )
    due = [c for c in due if c in valid]

    if not due:
        return

    _dispatch_batches(due, job_prefix="magento_retry_batch")
    frappe.logger("connector").info(
        f"retry_failed_product_sync: retrying {len(due)} failed items."
    )


# ---------------------------------------------------------------------------
# Internal: enqueue batches with deduplication
# ---------------------------------------------------------------------------

def _dispatch_batches(item_codes, job_prefix):
    """
    Split item_codes into BATCH_SIZE chunks and enqueue each as a single
    background job on the `long` queue.

    job_name is deterministic per prefix+offset so that if full_product_sync
    fires again before the previous batch finishes, no duplicate job is added.
    """
    for start in range(0, len(item_codes), BATCH_SIZE):
        batch = item_codes[start: start + BATCH_SIZE]
        frappe.enqueue(
            "connector.sync.product_sync._run_batch_product_sync",
            queue="long",
            timeout=600,
            job_name=f"{job_prefix}_{start}",
            enqueue_after_commit=True,
            item_codes=batch,
        )
