"""
Inventory Sync: ERPNext Bin (all warehouses) → Magento Stock

Scheduled every 15 minutes via tasks.py.
Pushes the sum of actual_qty across all warehouses for each synced item.
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


def _should_send_stock_for_item(item_code, send_stock_global):
    """
    True if stock should be sent to Magento for this item.
    Per-item magento_send_stock overrides: Yes = send, No = don't send, blank = use global.
    """
    try:
        per_item = frappe.db.get_value("Item", item_code, "magento_send_stock")
    except Exception:
        return bool(send_stock_global)
    if per_item == "No":
        return False
    if per_item == "Yes":
        return True
    return bool(send_stock_global)


def sync_inventory():
    """
    Main entry point called by the scheduler.
    Reads all Bin records, groups by item_code, and pushes totals to Magento.
    Only items that have been synced to Magento (have a product map entry) are updated.
    Respects Magento Settings "Send available stock to Magento" and per-Item override.
    """
    if not _is_magento_enabled():
        return

    sync_enabled = frappe.db.get_single_value("Magento Settings", "sync_enabled")
    if not sync_enabled:
        return

    send_stock_global = frappe.db.get_single_value("Magento Settings", "send_stock_to_magento")
    if send_stock_global is None:
        send_stock_global = True

    mapped_items = frappe.get_all(
        "Magento Product Map",
        filters={"sync_status": "Synced"},
        fields=["item_code", "magento_sku"],
    )

    if not mapped_items:
        return

    mapped_dict = {row["item_code"]: row["magento_sku"] for row in mapped_items}
    item_codes = list(mapped_dict.keys())

    bin_data = frappe.db.sql(
        """
        SELECT item_code, SUM(actual_qty) AS total_qty
        FROM `tabBin`
        WHERE item_code IN %(item_codes)s
        GROUP BY item_code
        """,
        {"item_codes": item_codes},
        as_dict=True,
    )

    bin_map = {row["item_code"]: max(0, float(row["total_qty"] or 0)) for row in bin_data}

    for item_code in mapped_dict:
        if item_code not in bin_map:
            bin_map[item_code] = 0.0

    try:
        client = MagentoClient()
    except Exception as e:
        frappe.log_error(str(e), "Magento Inventory Sync: Client Init Failed")
        return

    success_count = 0
    fail_count = 0

    for item_code, qty in bin_map.items():
        if not _should_send_stock_for_item(item_code, send_stock_global):
            continue
        sku = mapped_dict.get(item_code, item_code)
        try:
            client.update_stock(sku, qty)
            success_count += 1
        except MagentoAPIError as e:
            fail_count += 1
            create_log(
                operation="Inventory Push",
                status="Failed",
                doctype_name="Item",
                document_name=item_code,
                magento_id=sku,
                error_message=str(e),
                request_payload={"sku": sku, "qty": qty},
            )
        except Exception as e:
            fail_count += 1
            frappe.log_error(frappe.get_traceback(), f"Magento Inventory Sync Error: {item_code}")

    frappe.logger("connector").info(
        f"sync_inventory: {success_count} updated, {fail_count} failed out of {len(bin_map)} items."
    )

    if success_count > 0:
        create_log(
            operation="Inventory Push",
            status="Success",
            doctype_name="Bin",
            document_name="Bulk Sync",
            response_payload={"updated": success_count, "failed": fail_count},
        )
