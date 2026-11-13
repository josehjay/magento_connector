"""
ERPNext Product Sync: Local ERPNext Item → Remote ERPNext Sites

Triggered by:
  - Item.after_insert / Item.on_update  (real-time, via hooks.py)
  - tasks.erpnext_product_sync()        (hourly catch-up, scheduled)

Pushes items flagged with sync_to_erpnext_sites=1 to all enabled
Remote ERPNext Site entries via the Frappe REST API.
"""

import frappe
from connector.api.erpnext_client import ERPNextClient, ERPNextAPIError
from connector.security.permissions import require_item_write
from connector.connector.doctype.remote_site_product_map.remote_site_product_map import (
    get_remote_item_code,
    upsert_map,
)
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)
from connector.utils import is_erpnext_site_sync_enabled


# Failed remote pushes are retried by the catch-up job at most this often.
FAILED_RETRY_MINUTES = 60


# Kept as a module-level name: tests patch it.
_is_erpnext_site_sync_enabled = is_erpnext_site_sync_enabled


def _get_enabled_sites():
    """Return list of Remote ERPNext Site names with enable_sync=1."""
    return frappe.get_all(
        "Remote ERPNext Site",
        filters={"enable_sync": 1},
        pluck="name",
    )


def _get_item_price(item_code, price_list):
    """Fetch the selling price for an item from a specific price list."""
    if not price_list:
        return None
    price = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        "price_list_rate",
    )
    return float(price) if price else None


def _build_item_payload(doc, price_list=None):
    """
    Build a Frappe Item API payload from a local Item doc.
    Only includes core fields that are safe to push to a remote ERPNext site.
    """
    payload = {
        "item_code": doc.item_code,
        "item_name": doc.item_name,
        "item_group": doc.item_group,
        "description": doc.description or doc.item_name or "",
        "stock_uom": doc.stock_uom or "Nos",
        "is_stock_item": doc.is_stock_item,
        "is_sales_item": doc.is_sales_item,
        "is_purchase_item": doc.is_purchase_item,
        "disabled": doc.disabled,
    }

    price = _get_item_price(doc.item_code, price_list)
    if price is not None:
        payload["standard_rate"] = price

    if doc.get("weight_per_unit"):
        payload["weight_per_unit"] = float(doc.weight_per_unit)
    if doc.get("weight_uom"):
        payload["weight_uom"] = doc.weight_uom

    if doc.get("brand"):
        payload["brand"] = doc.brand
    # Item has no "barcode" field — barcodes live in the "barcodes" child table.
    barcodes = [b for b in (doc.get("barcodes") or []) if b.get("barcode")]
    if barcodes:
        payload["barcodes"] = [
            {"barcode": b.get("barcode"), "barcode_type": b.get("barcode_type") or ""}
            for b in barcodes
        ]

    return payload


def on_item_save(doc, method):
    """
    Hook called on Item after_insert and on_update.
    Pushes the item to all enabled remote ERPNext sites if conditions are met.

    Wrapped in a broad try/except: a failure here must NOT abort the user's
    Item save. Remote-site sync is best-effort.
    """
    try:
        if not _is_erpnext_site_sync_enabled():
            return
        if not doc.get("sync_to_erpnext_sites"):
            return

        item_code = (doc.get("item_code") or doc.get("name") or "").strip()
        if not item_code:
            return

        sites = _get_enabled_sites()
        if not sites:
            return

        for site_name in sites:
            frappe.enqueue(
                "connector.sync.erpnext_product_sync.push_item_to_site",
                queue="default",
                timeout=120,
                job_id=f"erpnext_push_{site_name}_{item_code}",
                deduplicate=True,
                enqueue_after_commit=True,
                item_code=item_code,
                remote_site=site_name,
            )
    except Exception:
        try:
            frappe.log_error(
                frappe.get_traceback(),
                "Connector Hook Failed: Item.erpnext_product_sync.on_item_save",
            )
        except Exception:
            pass


@frappe.whitelist()
def push_item_to_all_sites(item_code):
    """
    Manually push a single item to all enabled remote ERPNext sites.
    Can be called from the Item form button. Requires write access to the Item
    (the same users whose Item saves already queue this push).
    """
    require_item_write(item_code)
    if not _is_erpnext_site_sync_enabled():
        frappe.msgprint("ERPNext site sync is disabled in Connector Settings.", indicator="orange")
        return

    sites = _get_enabled_sites()
    if not sites:
        frappe.msgprint("No enabled Remote ERPNext Sites found.", indicator="orange")
        return

    for site_name in sites:
        frappe.enqueue(
            "connector.sync.erpnext_product_sync.push_item_to_site",
            queue="default",
            timeout=120,
            job_id=f"erpnext_push_{site_name}_{item_code}",
            deduplicate=True,
            item_code=item_code,
            remote_site=site_name,
        )


def push_item_to_site(item_code, remote_site):
    """
    Push a single ERPNext item to one remote ERPNext site.
    Called via frappe.enqueue.
    """
    if not _is_erpnext_site_sync_enabled():
        return

    if not item_code or not frappe.db.exists("Item", item_code):
        # Item gone between enqueue and execution; don't crash the worker.
        frappe.logger("connector").info(
            f"push_item_to_site: Item '{item_code}' no longer exists; skipping."
        )
        return

    doc = frappe.get_doc("Item", item_code)
    if not doc.get("sync_to_erpnext_sites"):
        return

    if not frappe.db.exists("Remote ERPNext Site", remote_site):
        frappe.logger("connector").info(
            f"push_item_to_site: Remote ERPNext Site '{remote_site}' missing; skipping."
        )
        return

    site_doc = frappe.get_doc("Remote ERPNext Site", remote_site)
    if not site_doc.enable_sync:
        return

    price_list = site_doc.price_list
    payload = _build_item_payload(doc, price_list=price_list)

    existing_remote_code = get_remote_item_code(remote_site, item_code)

    try:
        client = ERPNextClient(remote_site)

        if existing_remote_code:
            result = client.update_item(existing_remote_code, payload)
        else:
            if client.item_exists(item_code):
                result = client.update_item(item_code, payload)
            else:
                result = client.create_item(payload)

        remote_item_code = result.get("item_code") or result.get("name") or item_code

        upsert_map(remote_site, item_code, remote_item_code, "Synced")

        _push_item_price(client, remote_site, item_code, remote_item_code, price_list)

        create_log(
            operation="ERPNext Product Push",
            status="Success",
            doctype_name="Item",
            document_name=item_code,
            magento_id=f"{remote_site}:{remote_item_code}",
            request_payload=payload,
            response_payload=result,
        )

    except ERPNextAPIError as e:
        error_msg = str(e)
        upsert_map(remote_site, item_code, existing_remote_code or item_code, "Failed", error_msg)
        create_log(
            operation="ERPNext Product Push",
            status="Failed",
            doctype_name="Item",
            document_name=item_code,
            magento_id=f"{remote_site}",
            error_message=error_msg,
            request_payload=payload,
        )

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"ERPNext Product Sync Error: {item_code} → {remote_site}")
        upsert_map(remote_site, item_code, existing_remote_code or item_code, "Failed", str(e))
        create_log(
            operation="ERPNext Product Push",
            status="Failed",
            doctype_name="Item",
            document_name=item_code,
            magento_id=f"{remote_site}",
            error_message=str(e),
        )


def _push_item_price(client, remote_site, item_code, remote_item_code, price_list):
    """
    Upsert the remote selling Item Price after a successful item push.

    ERPNext only reads Item.standard_rate when the Item is first created, so
    without this the remote price never changes after the first push. A
    price failure is logged but never marks the item push as failed. The
    remote site must have a Price List with the same name as site.price_list.
    """
    if not price_list:
        return None
    rate = _get_item_price(item_code, price_list)
    if rate is None:
        return None
    try:
        return client.upsert_item_price(remote_item_code, price_list, rate)
    except Exception as e:
        create_log(
            operation="ERPNext Product Push",
            status="Failed",
            doctype_name="Item",
            document_name=item_code,
            magento_id=f"{remote_site}:{remote_item_code}",
            error_message=(
                f"Item pushed, but updating Item Price on '{remote_site}' "
                f"(price list '{price_list}', rate {rate}) failed: {e}. "
                "Check that the remote site has a Price List with the same name."
            ),
            request_payload={"item_code": remote_item_code, "price_list": price_list, "price_list_rate": rate},
        )
        return None


def on_item_price_change(doc, method=None):
    """
    Item Price hook: a price change does not touch Item.modified, so the
    catch-up job would never notice it. Re-push the item to every enabled
    site whose price_list is this Item Price's list. Best-effort — never
    blocks the Item Price save.
    """
    try:
        if not _is_erpnext_site_sync_enabled():
            return
        if not doc.get("selling") or not doc.get("item_code") or not doc.get("price_list"):
            return
        item_code = doc.get("item_code")
        if not frappe.db.get_value("Item", item_code, "sync_to_erpnext_sites"):
            return
        sites = frappe.get_all(
            "Remote ERPNext Site",
            filters={"enable_sync": 1, "price_list": doc.get("price_list")},
            pluck="name",
        )
        for site_name in sites:
            frappe.enqueue(
                "connector.sync.erpnext_product_sync.push_item_to_site",
                queue="default",
                timeout=120,
                job_id=f"erpnext_push_{site_name}_{item_code}",
                deduplicate=True,
                enqueue_after_commit=True,
                item_code=item_code,
                remote_site=site_name,
            )
    except Exception:
        try:
            frappe.log_error(
                frappe.get_traceback(),
                "Connector Hook Failed: Item Price.erpnext_product_sync.on_item_price_change",
            )
        except Exception:
            pass


def full_erpnext_product_sync():
    """
    Hourly catch-up: sync all Items flagged for ERPNext site sync that are
    stale or have never been synced.
    """
    if not _is_erpnext_site_sync_enabled():
        return

    sites = _get_enabled_sites()
    if not sites:
        return

    for site_name in sites:
        site_doc = frappe.get_doc("Remote ERPNext Site", site_name)

        filters = {"sync_to_erpnext_sites": 1, "disabled": 0}
        if site_doc.sync_item_group_filter:
            filters["item_group"] = site_doc.sync_item_group_filter

        items = frappe.get_all(
            "Item",
            filters=filters,
            fields=["item_code", "modified"],
        )

        # One query for the whole site instead of one per item.
        map_rows = {
            row["item_code"]: row
            for row in frappe.get_all(
                "Remote Site Product Map",
                filters={"remote_site": site_name},
                fields=["item_code", "last_synced_on", "sync_status"],
            )
        }

        retry_failed_before = frappe.utils.add_to_date(
            frappe.utils.now_datetime(), minutes=-FAILED_RETRY_MINUTES
        )
        queued = 0
        for item in items:
            map_row = map_rows.get(item["item_code"]) or {}
            last_synced = map_row.get("last_synced_on")
            # upsert_map stamps last_synced_on on failures too, so Failed rows
            # must be re-queued explicitly or they are never retried (at most
            # once per FAILED_RETRY_MINUTES so a permanent error can't flood
            # the remote site every 10 minutes).
            needs_push = (
                not last_synced
                or (map_row.get("sync_status") == "Failed" and last_synced < retry_failed_before)
                or (item["modified"] and last_synced < item["modified"])
            )

            if needs_push:
                frappe.enqueue(
                    "connector.sync.erpnext_product_sync.push_item_to_site",
                    queue="long",
                    timeout=120,
                    item_code=item["item_code"],
                    remote_site=site_name,
                    job_id=f"erpnext_sync_{site_name}_{item['item_code']}",
                    deduplicate=True,
                )
                queued += 1

        frappe.logger("connector").info(
            f"full_erpnext_product_sync [{site_name}]: queued {queued} items out of {len(items)} total."
        )

        frappe.db.set_value(
            "Remote ERPNext Site",
            site_name,
            "last_product_sync",
            frappe.utils.now_datetime(),
        )
        frappe.db.commit()
