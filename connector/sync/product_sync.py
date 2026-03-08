"""
Product Sync: ERPNext Item → Magento Product

Triggered by:
  - Item.after_insert / Item.on_update  (real-time, via hooks.py)
  - tasks.full_product_sync()           (hourly catch-up, scheduled)
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


def _is_magento_enabled():
    """Return True if Magento integration is enabled in Connector Settings."""
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def _is_sync_enabled():
    """Return True if the global sync switch is on and Magento is enabled."""
    if not _is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


def _get_allowed_item_groups():
    """
    Return the set of item groups configured in Magento Settings, or an empty
    set if no filter is configured (meaning all groups are allowed).
    """
    settings = frappe.get_single("Magento Settings")
    groups = {row.item_group for row in (settings.magento_item_groups or [])}
    return groups


def _is_item_group_allowed(item_group):
    """Return True if the item's group is in the allowed list (or no filter is set)."""
    allowed = _get_allowed_item_groups()
    if not allowed:
        return True
    return item_group in allowed


def _get_item_price(item_code, price_list):
    """Fetch the selling price for an item from the configured price list."""
    price = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        "price_list_rate",
    )
    return float(price) if price else 0.0


def _get_attribute_set_for_item_group(item_group):
    """
    Return Magento attribute_set_id for the given Item Group from Magento Settings.
    If the Item Group is in magento_item_groups with an attribute_set_id, use it; else 4.
    """
    settings = frappe.get_single("Magento Settings")
    for row in settings.magento_item_groups or []:
        if row.item_group == item_group and row.get("attribute_set_id"):
            try:
                return int(row.attribute_set_id)
            except (TypeError, ValueError):
                pass
    return 4


def _build_product_payload(doc):
    """
    Convert an ERPNext Item doc into a Magento product payload dict.
    Attribute set comes from Magento Settings → Item Group mapping.
    Does NOT set stock — inventory is managed by inventory_sync.py.
    When Item is disabled, status is set to 2 (Disabled) in Magento.
    """
    settings = frappe.get_single("Magento Settings")
    price_list = settings.price_list
    price = _get_item_price(doc.item_code, price_list)

    description = doc.description or doc.item_name or ""
    attribute_set_id = _get_attribute_set_for_item_group(doc.item_group or "")

    # Disabled items sync as status=2 (Disabled) in Magento
    if doc.get("disabled"):
        status = 2
    else:
        status = 1 if doc.is_sales_item else 2

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


def on_item_save(doc, method):
    """
    Hook called on Item after_insert and on_update.
    - If sync_to_magento is set: push to Magento (when enabled and group allowed).
    - If sync_to_magento is unchecked and item was synced: remove from Magento, map, and clear Item fields.
    Non-blocking — errors are logged but never raised to the user.
    """
    if not _is_sync_enabled():
        return

    if not doc.get("sync_to_magento"):
        if get_magento_product_id(doc.item_code):
            frappe.enqueue(
                "connector.sync.product_sync.remove_from_magento",
                queue="default",
                timeout=60,
                item_code=doc.item_code,
            )
        return
    if not _is_item_group_allowed(doc.item_group):
        return

    frappe.enqueue(
        "connector.sync.product_sync.push_item_to_magento",
        queue="default",
        timeout=120,
        item_code=doc.item_code,
    )


def remove_from_magento(item_code):
    """
    When user deselects Sync to Magento: disable product in Magento, delete map entry,
    clear magento_product_id and related fields on Item.
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
                pass
            else:
                client.update_product(item_code, {"status": 2})
    except Exception as e:
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


@frappe.whitelist()
def push_item_to_magento(item_code):
    """
    Push a single ERPNext item to Magento.
    Can be called directly or via frappe.enqueue.
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
            operation = "Product Push"
        else:
            if client.product_exists(item_code):
                result = client.update_product(item_code, payload)
            else:
                result = client.create_product(payload)
            operation = "Product Push"

        magento_product_id = result.get("id")

        upsert_map(item_code, magento_product_id, item_code, "Synced")

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
            operation=operation,
            status="Success",
            doctype_name="Item",
            document_name=item_code,
            magento_id=magento_product_id,
            request_payload=payload,
            response_payload=result,
        )

    except MagentoAPIError as e:
        error_msg = str(e)
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
        upsert_map(item_code, get_magento_product_id(item_code) or 0, item_code, "Failed")

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Magento Product Sync Error")
        create_log(
            operation="Product Push",
            status="Failed",
            doctype_name="Item",
            document_name=item_code,
            error_message=str(e),
        )


def full_product_sync():
    """
    Hourly catch-up: sync all Items that are stale or have never been synced.
    Enqueues each item individually to avoid a single long-running job.
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

    queued = 0
    for item in items:
        last_synced = item.get("magento_last_synced_on")
        modified = item.get("modified")
        if not last_synced or (modified and last_synced < modified):
            frappe.enqueue(
                "connector.sync.product_sync.push_item_to_magento",
                queue="long",
                timeout=120,
                item_code=item["item_code"],
                job_name=f"magento_product_sync_{item['item_code']}",
            )
            queued += 1

    frappe.logger("connector").info(
        f"full_product_sync: queued {queued} items out of {len(items)} total."
    )
