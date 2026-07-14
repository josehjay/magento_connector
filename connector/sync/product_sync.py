"""
Product Sync: ERPNext Item → Magento Product

Triggered by:
  - Item.after_insert / Item.on_update  (real-time, deduplicated by job_name)
  - tasks.full_product_sync()           (daily catch-up, scheduled; also "Sync All Products Now")
  - tasks.retry_failed_product_sync()   (every 30 min, retries failed items with backoff)

full_product_sync() only pushes items that aren't already "Synced" in the
Magento Product Map (never synced, "Pending", "Failed"-and-not-exhausted, or
"Synced" but edited in ERPNext since) — it complements Rebuild Product Maps
rather than re-pushing everything that tool already restored.

Retry strategy (exponential backoff):
  retry_count 1 → wait  5 min before retry
  retry_count 2 → wait 10 min
  retry_count 3 → wait 20 min
  retry_count 4 → wait 40 min
  retry_count 5+ → wait 60 min (capped)
  retry_count > MAX_RETRIES → item is skipped until manually triggered or item is re-saved
"""

import time

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

# Number of items per batch job. Kept small so each job finishes within timeout
# (each item may do 2+ Magento API calls; slow responses can exceed 600s with 50 items).
BATCH_SIZE = 20

# Timeout in seconds for each batch job (long queue).
BATCH_JOB_TIMEOUT = 900


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _enqueue_product_job(method_path, item_code, timeout=120):
    """Enqueue a product sync job with consistent defaults."""
    job_prefix = "magento_remove_" if "remove_from_magento" in method_path else "magento_product_sync_"
    frappe.enqueue(
        method_path,
        queue="default",
        timeout=timeout,
        job_id=f"{job_prefix}{item_code}",
        deduplicate=True,
        enqueue_after_commit=True,
        item_code=item_code,
    )


def _enqueue_product_sync(item_code, mode):
    """
    Queue a product push or removal so it runs in a background worker
    AFTER the current transaction commits.

    Doing the Magento API calls inline (inside an `after_commit` callback)
    holds the user's save HTTP request open while Magento is contacted.
    If Magento is slow, unreachable, or any of the multiple intermediate
    `frappe.db.commit()` calls error out, the response can collapse into a
    Frappe website 404 page (the user sees "Item <name> not found").
    Always enqueueing keeps saves instant and isolates Magento failures
    from the user's request.
    """
    if mode not in ("push", "remove"):
        return

    method_path = (
        "connector.sync.product_sync.push_item_to_magento"
        if mode == "push"
        else "connector.sync.product_sync.remove_from_magento"
    )
    timeout = 120 if mode == "push" else 60

    _enqueue_product_job(method_path, item_code, timeout=timeout)


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


def _get_variant_attributes(item_code):
    """
    Return list of {attribute_code, value} for an Item variant from Item Variant Attribute.

    attribute_code is resolved from the Magento Attribute Mapping for the ERPNext
    Item Attribute (e.g. "Size" -> "size") when one exists, so it matches the
    actual Magento attribute created/mapped via the attribute sync tool. Falls
    back to a naive slug (lowercase, spaces to underscores) for attributes that
    haven't been mapped yet, so pushes don't fail outright — map the attribute
    in Magento Settings to get the real, usable configurable option.
    """
    if not frappe.db.table_exists("Item Variant Attribute"):
        return []
    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": item_code},
        fields=["attribute", "attribute_value"],
    )
    out = []
    for row in rows:
        item_attribute = (row.get("attribute") or "").strip()
        if not item_attribute:
            continue
        code = _resolve_magento_attribute_code(item_attribute)
        if not code:
            continue
        value = (row.get("attribute_value") or "").strip()
        out.append({"attribute_code": code, "value": value or ""})
    return out


def _resolve_magento_attribute_code(item_attribute):
    """
    Return the mapped Magento attribute_code for an ERPNext Item Attribute
    (via Magento Attribute Mapping), or a naive slug fallback if it hasn't
    been mapped/created in Magento yet.
    """
    from connector.connector.doctype.magento_attribute_mapping.magento_attribute_mapping import (
        get_magento_attribute_code,
    )

    mapped_code = get_magento_attribute_code(item_attribute)
    if mapped_code:
        return mapped_code
    return item_attribute.strip().lower().replace(" ", "_")


def _build_product_payload(doc):
    """
    Convert an ERPNext Item doc into a Magento product payload dict.
    - Template (has_variants): type_id configurable.
    - Variant (variant_of): type_id simple, with variant attributes in custom_attributes.
    Attribute set from Magento Settings → Item Group. Stock via inventory_sync.

    ERPNext is the sole source of truth for name/price: this payload always
    carries ERPNext's current item_name/price and is pushed on every sync, so
    Magento's copy is kept in lockstep with ERPNext rather than the reverse.
    Nothing in this app ever pulls name/price back from Magento into ERPNext —
    keep it that way; if Magento and ERPNext disagree on name/price, the next
    push overwrites Magento with ERPNext's value.
    """
    settings = frappe.get_single("Magento Settings")
    price = _get_item_price(doc.item_code, settings.price_list)
    description = doc.description or doc.item_name or ""
    attribute_set_id = _get_attribute_set_for_item_group(doc.item_group or "")

    status = 2 if doc.get("disabled") else (1 if doc.is_sales_item else 2)

    # Template (has_variants) → configurable; variant (variant_of) or standalone → simple
    is_template = bool(doc.get("has_variants"))
    type_id = "configurable" if is_template else "simple"

    custom_attributes = [
        {"attribute_code": "description", "value": description},
        {"attribute_code": "short_description", "value": description[:255]},
    ]

    # Variant: add configurable-option attributes so Magento can link this simple to the configurable
    if doc.get("variant_of"):
        for attr in _get_variant_attributes(doc.item_code):
            custom_attributes.append(attr)

    payload = {
        "sku": doc.item_code,
        "name": doc.item_name,
        "price": price,
        "status": status,
        "visibility": 4,
        "type_id": type_id,
        "attribute_set_id": attribute_set_id,
        "custom_attributes": custom_attributes,
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


def _merge_preserving_magento_only_data(existing_product, payload):
    """
    Overlay our ERPNext-driven payload onto the product Magento already has,
    so fields Magento manages and ERPNext knows nothing about — category
    assignments, images/media gallery, and any custom attributes we don't
    explicitly set — are carried through untouched rather than wiped by a
    partial PUT.

    This matters because Magento's product REST API does not merge
    `extension_attributes`: a PUT that includes `extension_attributes` (e.g.
    just `stock_item`) can silently clear `category_links` and other
    extension data that isn't repeated in the request. `custom_attributes`
    is safer (Magento merges by attribute_code) but we still merge explicitly
    here rather than rely on that behavior.

    ERPNext-owned fields (name, price, status, visibility, type_id,
    attribute_set_id, weight, and the custom_attributes/extension_attributes
    keys we manage) always win. Everything else Magento already has for this
    product is preserved as-is.
    """
    if not existing_product:
        return payload

    merged = dict(existing_product)
    merged.update({
        k: v for k, v in payload.items()
        if k not in ("custom_attributes", "extension_attributes")
    })

    existing_custom = {
        attr.get("attribute_code"): attr
        for attr in (existing_product.get("custom_attributes") or [])
        if attr.get("attribute_code")
    }
    for attr in payload.get("custom_attributes") or []:
        existing_custom[attr["attribute_code"]] = attr
    merged["custom_attributes"] = list(existing_custom.values())

    existing_extension_attributes = dict(existing_product.get("extension_attributes") or {})
    existing_extension_attributes.update(payload.get("extension_attributes") or {})
    merged["extension_attributes"] = existing_extension_attributes

    return merged


# ---------------------------------------------------------------------------
# Doc event hook (real-time, called on every Item save)
# ---------------------------------------------------------------------------

def _safe_hook(label):
    """
    Decorator: never let a connector doc-event hook raise.
    A failure inside any of these hooks must not abort the user's save —
    we log the traceback and return cleanly. Magento sync is best-effort.
    """
    def _wrap(fn):
        def _inner(doc, method):
            try:
                return fn(doc, method)
            except Exception:
                try:
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Connector Hook Failed: {label}",
                    )
                except Exception:
                    # Logging itself must not propagate
                    pass
        _inner.__name__ = fn.__name__
        _inner.__doc__ = fn.__doc__
        return _inner
    return _wrap


@_safe_hook("Item.on_item_save")
def on_item_save(doc, method):
    """
    Hook: Item after_insert / on_update.
    - Deselected sync_to_magento → enqueue removal from Magento and map.
    - Enabled sync_to_magento + allowed group → enqueue a push to Magento.

    The actual Magento API calls run in a background worker (deduplicated
    by item_code) AFTER the current ERPNext transaction commits. This keeps
    the user's save fast and prevents Magento outages or slow responses
    from breaking the Item form.
    """
    if not _is_sync_enabled():
        return

    item_code = (doc.get("item_code") or doc.get("name") or "").strip()
    if not item_code:
        return

    if not doc.get("sync_to_magento"):
        if get_magento_product_id(item_code):
            _enqueue_product_sync(item_code, mode="remove")
        return

    if not _is_item_group_allowed(doc.get("item_group")):
        return

    _enqueue_product_sync(item_code, mode="push")


@_safe_hook("Item.on_item_trash")
def on_item_trash(doc, method):
    """
    Hook: Item on_trash.
    When a synced ERPNext item is deleted, enqueue removal from Magento so
    the worker can call the Magento API after the delete commits.
    """
    if not _is_sync_enabled():
        return

    item_code = (doc.get("item_code") or doc.get("name") or "").strip()
    if not item_code:
        return

    has_map = bool(get_magento_product_id(item_code))

    # Only attempt Magento removal when this item was intended for Magento sync
    # or we still have an existing map entry to clean up.
    if not doc.get("sync_to_magento") and not has_map:
        return

    _enqueue_product_sync(item_code, mode="remove")


@_safe_hook("Item Price.on_item_price_change")
def on_item_price_change(doc, method):
    """
    Hook: Item Price after_insert / on_update / on_trash.
    When the configured Magento selling price list changes, enqueue a push
    of that item to Magento.
    """
    if not _is_sync_enabled():
        return

    item_code = (doc.get("item_code") or "").strip()
    if not item_code:
        return

    configured_price_list = frappe.db.get_single_value("Magento Settings", "price_list")
    if not configured_price_list:
        return

    if doc.get("price_list") != configured_price_list:
        return

    if not doc.get("selling"):
        return

    _enqueue_product_sync(item_code, mode="push")


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
    if frappe.db.exists("Item", item_code):
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

    if not item_code or not frappe.db.exists("Item", item_code):
        # Item was deleted between enqueue and execution, or item_code is bogus.
        # Don't raise — that would crash the worker and surface as a user error
        # if anything ever calls this synchronously.
        frappe.logger("connector").info(
            f"push_item_to_magento: Item '{item_code}' no longer exists; skipping."
        )
        return

    doc = frappe.get_doc("Item", item_code)

    if not doc.get("sync_to_magento"):
        return

    if not _is_item_group_allowed(doc.item_group):
        return

    payload = _build_product_payload(doc)

    try:
        client = MagentoClient()

        try:
            existing_product = client.get_product(item_code)
        except MagentoAPIError as e:
            if e.status_code != 404:
                raise
            existing_product = None

        if existing_product:
            payload = _merge_preserving_magento_only_data(existing_product, payload)
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

        # Magento's 'name' is store-view scoped, so a store-specific PUT only updates
        # that one store view. Push name + status to the 'all' scope so it is visible
        # across every store view (price is globally scoped and doesn't need this).
        client.update_product_global_scope(item_code, {
            "name": doc.item_name,
            "status": payload.get("status", 1),
        })

        # If this item is a variant, link it to the configurable product in Magento
        if doc.get("variant_of"):
            _link_variant_to_configurable(client, doc.variant_of, doc.item_code)

        # Pull image and other data back from Magento into ERPNext
        image_result = _pull_magento_data_for_item(client, item_code)

        return {
            "success": True,
            "magento_product_id": magento_product_id,
            "image": image_result,
        }

    except (MagentoAPIError, Exception) as e:
        _handle_push_failure(item_code, e, payload)
        return {"success": False, "error": str(e)}


def _pull_magento_data_for_item(client, item_code):
    """
    After a successful product push, pull image and other product data back
    from Magento into ERPNext. This runs inline (not enqueued) so the user
    sees the result immediately when clicking "Push to Magento".

    Returns a dict describing what was pulled.
    """
    from connector.sync.image_sync import _extract_base_image_url, _get_item_image_field

    result = {"image_updated": False, "image_url": None, "error": None}

    image_field = _get_item_image_field()
    if not image_field:
        result["error"] = "no_image_field_on_item"
        return result

    magento_url = frappe.db.get_single_value("Magento Settings", "magento_url")
    if not magento_url:
        result["error"] = "no_magento_url"
        return result
    magento_url = magento_url.rstrip("/")

    sku = item_code
    try:
        media_entries = client.get_product_media(sku)
    except Exception as e:
        result["error"] = f"media_fetch_failed: {e}"
        frappe.logger("connector").warning(
            f"_pull_magento_data_for_item: media fetch failed for {item_code}: {e}"
        )
        return result

    if not media_entries:
        result["error"] = "no_media_in_magento"
        return result

    base_image_url = _extract_base_image_url(media_entries, magento_url)
    if not base_image_url:
        result["error"] = "no_base_image_type"
        return result

    result["image_url"] = base_image_url

    try:
        current_image = frappe.db.get_value("Item", item_code, image_field)
        if current_image != base_image_url:
            frappe.db.set_value(
                "Item", item_code, image_field, base_image_url,
                update_modified=False,
            )
            frappe.db.commit()
            result["image_updated"] = True
            frappe.logger("connector").info(
                f"_pull_magento_data_for_item: image updated for {item_code} → {base_image_url}"
            )
    except Exception as e:
        result["error"] = f"image_save_failed: {e}"
        frappe.logger("connector").warning(
            f"_pull_magento_data_for_item: image save failed for {item_code}: {e}"
        )

    return result


def _link_variant_to_configurable(client, parent_item_code, variant_sku):
    """
    Link a simple product (variant) to its configurable parent in Magento.
    parent_item_code = ERPNext template Item code (= Magento configurable SKU).
    Skips if already linked; logs and continues on API errors (e.g. parent not synced yet).
    """
    try:
        children = client.get_configurable_children(parent_item_code)
        existing_skus = set()
        for c in children or []:
            if isinstance(c, dict) and c.get("sku"):
                existing_skus.add(c["sku"])
            elif isinstance(c, str):
                existing_skus.add(c)
        if variant_sku in existing_skus:
            return
        client.add_child_to_configurable(parent_item_code, variant_sku)
        frappe.logger("connector").info(
            f"Linked variant {variant_sku} to configurable {parent_item_code} in Magento."
        )
    except MagentoAPIError as e:
        # Parent may not exist yet, or already linked; don't fail the variant push
        frappe.logger("connector").warning(
            f"Could not link variant {variant_sku} to configurable {parent_item_code}: {e}"
        )
    except Exception as e:
        frappe.log_error(
            f"Link variant to configurable: {e}\n{frappe.get_traceback()}",
            "Connector: Link Variant to Configurable",
        )


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

def _run_batch_product_sync(item_codes, time_budget_seconds=None):
    """
    Process a list of item_codes sequentially within a single background job.
    Each item's failure is isolated — one bad item cannot stop the rest.

    If `time_budget_seconds` is given, a deadline is computed from `time.monotonic()`
    at the START of this call (in whichever worker process actually runs the
    job — never computed by the enqueuing process, since `time.monotonic()`
    is only guaranteed comparable within one machine/clock domain). Once that
    budget is used up, processing stops immediately and the untouched
    remainder is left for the caller to re-query and hand to the next chunk —
    this is what keeps a run of slow/hanging Magento responses from ever
    risking the job's hard RQ timeout (which would otherwise silently kill
    the whole batch mid-item instead of cleanly continuing on the next run).
    """
    logger = frappe.logger("connector")
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None
    success = failed = 0
    attempted = 0
    for item_code in item_codes:
        if deadline is not None and time.monotonic() > deadline:
            logger.info(
                f"_run_batch_product_sync: soft deadline reached after {attempted} item(s); "
                f"{len(item_codes) - attempted} item(s) deferred to the next chunk."
            )
            break
        attempted += 1
        try:
            push_item_to_magento(item_code)
            success += 1
        except Exception as e:
            failed += 1
            frappe.log_error(
                f"Batch sync failed for {item_code}: {e}",
                "Connector Product Sync Batch",
            )
    logger.info(
        f"_run_batch_product_sync: {success} ok, {failed} failed out of {attempted} attempted "
        f"({len(item_codes)} in batch)."
    )


# ---------------------------------------------------------------------------
# Scheduled: full catch-up sync (chunked: one job per chunk, reschedules next chunk)
# ---------------------------------------------------------------------------

# Single job name so only one full-sync chunk runs at a time.
FULL_SYNC_JOB_NAME = "magento_full_product_sync"
# Items per chunk; each chunk runs in one job and stays within timeout.
FULL_SYNC_CHUNK_SIZE = 50
# Hard timeout per chunk job (seconds).
FULL_SYNC_CHUNK_TIMEOUT = 1800
# Stop taking new items this many seconds before the hard timeout, so a run of
# slow items causes an early, clean handoff to the next chunk instead of a
# mid-item kill by RQ's job timeout.
FULL_SYNC_SOFT_DEADLINE_MARGIN = 300
# Failed items with more consecutive failures than this are left to the
# dedicated retry_failed_product_sync() backoff schedule instead of being
# retried on every full-sync run.
FULL_SYNC_MAX_RETRIES = MAX_RETRIES


def _get_items_needing_full_sync():
    """
    Return sorted list of item codes that still need a bulk push, restricted
    to the Item Groups configured in "Item Groups to Sync" (Magento Settings).

    An item needs syncing if:
      - it has no Magento Product Map row yet (never synced), or
      - its map status is "Pending" (checked — not found in Magento), or
      - its map status is "Failed" and it hasn't exhausted retries yet
        (next attempt: if it now succeeds, its status becomes "Synced" and
        it's skipped on subsequent runs), or
      - its map status is "Synced" but the item was edited in ERPNext since
        the last successful sync (keeps ERPNext as the source of truth).

    Items already "Synced" with no changes since — whether that map entry
    came from a real push or from "Rebuild Product Maps" — are skipped, so
    this tool complements the rebuild tool instead of re-pushing everything
    it just restored.
    """
    filters = {"sync_to_magento": 1}
    allowed_groups = _get_allowed_item_groups()
    if allowed_groups:
        filters["item_group"] = ["in", list(allowed_groups)]

    items = frappe.get_all(
        "Item",
        filters=filters,
        fields=["item_code", "modified", "magento_last_synced_on", "has_variants", "variant_of"],
    )
    if not items:
        return []

    item_codes = [item["item_code"] for item in items]
    map_rows = {
        row["item_code"]: row
        for row in frappe.get_all(
            "Magento Product Map",
            filters={"item_code": ["in", item_codes]},
            fields=["item_code", "sync_status", "retry_count"],
        )
    }

    to_sync = []
    for item in items:
        item_code = item["item_code"]
        map_row = map_rows.get(item_code)
        status = (map_row or {}).get("sync_status")

        if status == "Failed":
            if (map_row.get("retry_count") or 0) > FULL_SYNC_MAX_RETRIES:
                continue  # exhausted — left to a manual retry or item re-save
            to_sync.append(item_code)
            continue

        if status != "Synced":
            # No map row yet, or "Pending" (checked, not found in Magento).
            to_sync.append(item_code)
            continue

        # Already Synced — only re-push if ERPNext data changed since then.
        if not item.get("magento_last_synced_on") or (
            item.get("modified") and item["magento_last_synced_on"] < item["modified"]
        ):
            to_sync.append(item_code)

    by_code = {item["item_code"]: item for item in items}
    to_sync.sort(key=lambda c: (0 if (by_code.get(c) or {}).get("has_variants") else 1, c))
    return to_sync


def run_full_product_sync_chunk():
    """
    Process one chunk of items needing sync, then enqueue the next chunk if
    more remain. Re-queries the "needs sync" list before AND after processing
    so progress is always persisted — this naturally accounts for items that
    succeeded (now "Synced", excluded next time), items that failed (retried
    next chunk, up to FULL_SYNC_MAX_RETRIES), and items deferred by the soft
    deadline (still pending, retried next chunk).

    Only one job with FULL_SYNC_JOB_NAME runs at a time. An unexpected error
    in this chunk is logged but never stops the remaining backlog — the next
    chunk is still enqueued.
    """
    if not _is_sync_enabled():
        return

    to_sync = _get_items_needing_full_sync()
    if not to_sync:
        frappe.logger("connector").info("run_full_product_sync_chunk: nothing needs syncing.")
        return

    chunk = to_sync[:FULL_SYNC_CHUNK_SIZE]
    time_budget = FULL_SYNC_CHUNK_TIMEOUT - FULL_SYNC_SOFT_DEADLINE_MARGIN

    frappe.logger("connector").info(
        f"run_full_product_sync_chunk: processing up to {len(chunk)} item(s) "
        f"({len(to_sync) - len(chunk)} more queued after this chunk)."
    )
    try:
        _run_batch_product_sync(chunk, time_budget_seconds=time_budget)
    except Exception:
        # Never let one bad chunk silently stop the rest of the backlog.
        frappe.log_error(frappe.get_traceback(), "Connector Full Product Sync Chunk")

    remaining = len(_get_items_needing_full_sync())
    if remaining > 0:
        frappe.enqueue(
            "connector.sync.product_sync.run_full_product_sync_chunk",
            queue="long",
            timeout=FULL_SYNC_CHUNK_TIMEOUT,
            job_id=FULL_SYNC_JOB_NAME,
            deduplicate=True,
            enqueue_after_commit=True,
        )
        frappe.logger("connector").info(
            f"run_full_product_sync_chunk: enqueued next chunk ({remaining} item(s) left)."
        )
    else:
        frappe.logger("connector").info("run_full_product_sync_chunk: full sync complete.")


def full_product_sync():
    """
    Enqueue one chunk job to start the full sync. That job processes a fixed number
    of items, then enqueues the next chunk if more items remain. At most one
    chunk job runs at a time; avoids timeout on 10k+ items.
    """
    if not _is_sync_enabled():
        return

    to_sync = _get_items_needing_full_sync()
    if not to_sync:
        frappe.logger("connector").info("full_product_sync: nothing needs syncing.")
        return

    frappe.enqueue(
        "connector.sync.product_sync.run_full_product_sync_chunk",
        queue="long",
        timeout=FULL_SYNC_CHUNK_TIMEOUT,
        job_id=FULL_SYNC_JOB_NAME,
        deduplicate=True,
        enqueue_after_commit=True,
    )
    frappe.logger("connector").info(
        f"full_product_sync: enqueued 1 chunk job ({len(to_sync)} item(s) total need syncing)."
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

    # Only retry items that still want to be synced and are still within an
    # allowed Item Group — a group removed from "Item Groups to Sync" after
    # an item failed should stop being retried, not loop forever.
    valid_filters = {"item_code": ["in", due], "sync_to_magento": 1}
    allowed_groups = _get_allowed_item_groups()
    if allowed_groups:
        valid_filters["item_group"] = ["in", list(allowed_groups)]
    valid = set(frappe.get_all("Item", filters=valid_filters, pluck="item_code"))
    due = [c for c in due if c in valid]

    if not due:
        return

    # Cap the batch so a large backlog of failed items can't overload the
    # system in one job — remaining items are simply picked up on the next
    # 30-minute run (they're still "Failed" until retried successfully).
    due = due[:BATCH_SIZE]

    # Run retries in a single long-queue job so the scheduler task returns within
    # its 300s limit; the actual work uses BATCH_JOB_TIMEOUT (e.g. 900s). A soft
    # deadline (margin below the hard timeout) keeps a run of slow/hanging
    # Magento responses from risking a mid-item kill of the whole batch.
    frappe.enqueue(
        "connector.sync.product_sync._run_batch_product_sync",
        queue="long",
        timeout=BATCH_JOB_TIMEOUT,
        job_id="magento_retry_failed_sync",
        deduplicate=True,
        enqueue_after_commit=True,
        item_codes=due,
        time_budget_seconds=BATCH_JOB_TIMEOUT - 120,
    )
    frappe.logger("connector").info(
        f"retry_failed_product_sync: enqueued {len(due)} failed item(s) for retry."
    )


