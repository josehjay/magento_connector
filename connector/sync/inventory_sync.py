"""
Inventory Sync: ERPNext Bin (chosen warehouses) → Magento Stock

Scheduled every 15 minutes via tasks.py.

Pushes AVAILABLE stock — SUM(actual_qty - reserved_qty) over the warehouses
listed in Magento Settings → Stock Warehouses (all non-group warehouses when
none are chosen), floored at 0. Subtracting reserved_qty stops Magento from
selling units already promised to open Sales Orders.

Only changes are pushed: an item is sent when its qty differs from the
Magento Product Map's last_pushed_qty, when it was never pushed, or when the
last push is older than FULL_RESYNC_HOURS (safety net for edits made directly
in Magento or lost requests). One PUT per item is kept on purpose: Magento's
async bulk APIs need message-queue consumers we cannot verify are running.
"""

from datetime import timedelta

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)
from connector.utils import is_magento_enabled

# Kept as a module-level name: tests patch it.
_is_magento_enabled = is_magento_enabled

FULL_RESYNC_HOURS = 24
COMMIT_EVERY = 50
QTY_EPSILON = 1e-6


def _resolve_send_stock(per_item, send_stock_global):
    """
    True if stock should be sent to Magento for this item.
    Per-item magento_send_stock overrides: Yes = send, No = don't send, blank = use global.
    """
    if per_item == "No":
        return False
    if per_item == "Yes":
        return True
    return bool(send_stock_global)


def _should_send_stock_for_item(item_code, send_stock_global):
    """Single-item variant (kept for callers/tests); sync_inventory batches this lookup."""
    try:
        per_item = frappe.db.get_value("Item", item_code, "magento_send_stock")
    except Exception:
        return bool(send_stock_global)
    return _resolve_send_stock(per_item, send_stock_global)


def _get_stock_warehouses():
    """Warehouses chosen in Magento Settings → Stock Warehouses (empty = all)."""
    try:
        return [
            w for w in frappe.get_all(
                "Magento Stock Warehouse",
                filters={"parent": "Magento Settings", "parenttype": "Magento Settings"},
                pluck="warehouse",
            ) if w
        ]
    except Exception:
        # Child doctype not migrated yet → behave as "all warehouses".
        return []


def _get_enabled_items_send_flags(item_codes):
    """
    {item_code: magento_send_stock} for enabled Items, in one query
    (replaces the per-item get_value N+1). Falls back to blank flags when the
    custom field does not exist.
    """
    try:
        rows = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes], "disabled": 0},
            fields=["name", "magento_send_stock"],
        )
        return {r["name"]: r.get("magento_send_stock") for r in rows}
    except Exception:
        names = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes], "disabled": 0},
            pluck="name",
        )
        return {n: None for n in names}


def _get_available_qty(item_codes, warehouses):
    """
    {item_code: available qty} in one aggregate query.
    Available = SUM(actual_qty - reserved_qty), floored at 0.
    """
    if warehouses:
        warehouse_clause = "AND b.warehouse IN %(warehouses)s"
    else:
        warehouse_clause = "AND w.is_group = 0"
    rows = frappe.db.sql(
        f"""
        SELECT b.item_code,
               SUM(IFNULL(b.actual_qty, 0) - IFNULL(b.reserved_qty, 0)) AS available_qty
        FROM `tabBin` b
        INNER JOIN `tabWarehouse` w ON w.name = b.warehouse
        WHERE b.item_code IN %(item_codes)s
          {warehouse_clause}
        GROUP BY b.item_code
        """,
        {"item_codes": item_codes, "warehouses": warehouses or [""]},
        as_dict=True,
    )
    return {row["item_code"]: max(0.0, float(row["available_qty"] or 0)) for row in rows}


def _needs_push(qty, last_pushed_qty, last_stock_push_on, resync_before):
    if not last_stock_push_on or last_pushed_qty is None:
        return True
    if abs(float(qty) - float(last_pushed_qty)) > QTY_EPSILON:
        return True
    return frappe.utils.get_datetime(last_stock_push_on) < resync_before


def sync_inventory():
    """
    Main entry point called by the scheduler (enqueued by tasks.sync_inventory).
    Only items with a Synced Magento Product Map row, enabled in ERPNext and
    allowed by Send Stock (global + per-Item override) are considered; of
    those, only changed / stale ones are pushed.
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
        fields=["name", "item_code", "magento_sku", "last_pushed_qty", "last_stock_push_on"],
    )
    if not mapped_items:
        return

    mapped = {row["item_code"]: row for row in mapped_items}

    # Never push stock for disabled ERPNext Items; honour Send Stock flags.
    send_flags = _get_enabled_items_send_flags(list(mapped))
    item_codes = [
        code for code in mapped
        if code in send_flags and _resolve_send_stock(send_flags[code], send_stock_global)
    ]
    if not item_codes:
        return

    warehouses = _get_stock_warehouses()
    qty_map = _get_available_qty(item_codes, warehouses)

    now = frappe.utils.now_datetime()
    resync_before = now - timedelta(hours=FULL_RESYNC_HOURS)
    to_push = []
    for code in item_codes:
        qty = qty_map.get(code, 0.0)
        row = mapped[code]
        if _needs_push(qty, row.get("last_pushed_qty"), row.get("last_stock_push_on"), resync_before):
            to_push.append((code, qty))

    unchanged = len(item_codes) - len(to_push)
    if not to_push:
        frappe.logger("connector").info(
            f"sync_inventory: nothing to push ({unchanged} items unchanged)."
        )
        return

    try:
        client = MagentoClient()
    except Exception as e:
        frappe.log_error(str(e), "Magento Inventory Sync: Client Init Failed")
        return

    success_count = 0
    fail_count = 0

    for item_code, qty in to_push:
        row = mapped[item_code]
        sku = row.get("magento_sku") or item_code
        try:
            client.update_stock(sku, qty)
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
            continue
        except Exception:
            fail_count += 1
            frappe.log_error(frappe.get_traceback(), f"Magento Inventory Sync Error: {item_code}")
            continue

        success_count += 1
        try:
            frappe.db.set_value(
                "Magento Product Map",
                row.get("name") or item_code,
                {"last_pushed_qty": qty, "last_stock_push_on": frappe.utils.now_datetime()},
                update_modified=False,
            )
        except Exception:
            # Not fatal: the item is simply pushed again next run.
            frappe.log_error(frappe.get_traceback(), f"Magento Inventory Sync: map update failed {item_code}")
        if success_count % COMMIT_EVERY == 0:
            frappe.db.commit()

    if success_count:
        frappe.db.commit()

    frappe.logger("connector").info(
        f"sync_inventory: {success_count} updated, {fail_count} failed, "
        f"{unchanged} unchanged out of {len(item_codes)} items "
        f"(warehouses: {', '.join(warehouses) if warehouses else 'all'})."
    )

    if success_count > 0:
        create_log(
            operation="Inventory Push",
            status="Success",
            doctype_name="Bin",
            document_name="Bulk Sync",
            response_payload={
                "updated": success_count,
                "failed": fail_count,
                "unchanged": unchanged,
                "warehouses": warehouses or "all",
            },
        )
