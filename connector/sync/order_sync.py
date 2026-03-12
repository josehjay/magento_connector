"""
Order Sync: Magento Orders → ERPNext Sales Orders (Draft)

Scheduled every 10 minutes via tasks.py.
Pulls new and updated orders from Magento, creates/updates Draft Sales Orders.
Also syncs Magento-side status changes back to ERPNext.
"""

import frappe
from frappe.utils import add_days, nowdate, get_datetime
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_order_map.magento_order_map import (
    is_order_imported,
    create_map,
    update_status,
    get_sales_order_for_magento_order,
)
from connector.connector.doctype.magento_sync_log.magento_sync_log import (
    create_log,
)
from connector.sync.customer_sync import get_or_create_customer, get_or_create_address


MAGENTO_STATUS_NOTES = {
    "pending": "Awaiting payment confirmation",
    "pending_payment": "Awaiting payment confirmation",
    "payment_review": "Payment under review",
    "processing": "Payment confirmed, processing",
    "holded": "Order on hold in Magento",
    "complete": "Order fulfilled in Magento",
    "closed": "Order closed in Magento",
    "canceled": "Order cancelled in Magento",
}


def _is_sync_enabled():
    try:
        enabled = frappe.db.get_single_value("Connector Settings", "enable_magento_integration")
        if enabled is not None and not bool(enabled):
            return False
    except Exception:
        pass
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


@frappe.whitelist()
def run_order_sync_now():
    """
    Whitelisted entry point for running order sync directly (not enqueued).
    Use from Magento Settings "Sync Orders Now" button or bench execute.
    Runs synchronously in the current process.
    """
    frappe.logger("connector").info("run_order_sync_now: starting direct order sync.")
    result = sync_orders()
    frappe.logger("connector").info(f"run_order_sync_now: done. result={result}")
    return result


def sync_orders():
    """
    Main scheduled entry point.
    Pulls all orders updated since last sync time and processes them.
    Returns a summary dict.
    """
    logger = frappe.logger("connector")

    if not _is_sync_enabled():
        logger.info("sync_orders: skipped — sync is disabled.")
        return {"status": "skipped", "reason": "sync_disabled"}

    settings = frappe.get_single("Magento Settings")
    last_sync = settings.last_order_sync_time

    logger.info(f"sync_orders: fetching orders updated after {last_sync or 'ALL TIME (first run)'}.")

    try:
        client = MagentoClient()
        orders = client.get_all_new_orders(updated_after=last_sync)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Magento Order Sync: Failed to fetch orders")
        create_log(
            operation="Order Pull",
            status="Failed",
            error_message=str(e),
        )
        return {"status": "error", "reason": f"fetch_failed: {e}"}

    if not orders:
        logger.info("sync_orders: Magento returned 0 orders. Nothing to process.")
        frappe.db.set_single_value(
            "Magento Settings", "last_order_sync_time", frappe.utils.now_datetime()
        )
        frappe.db.commit()
        return {"status": "ok", "orders_fetched": 0}

    logger.info(f"sync_orders: fetched {len(orders)} orders from Magento.")

    imported = 0
    updated = 0
    skipped = 0
    failed = 0

    for order in orders:
        try:
            result = _process_order(order, client)
            if result == "imported":
                imported += 1
            elif result == "updated":
                updated += 1
            elif result == "skipped":
                skipped += 1
        except Exception as e:
            failed += 1
            frappe.log_error(
                frappe.get_traceback(),
                f"Magento Order Sync Error: order {order.get('increment_id')}",
            )
            create_log(
                operation="Order Pull",
                status="Failed",
                magento_id=str(order.get("entity_id")),
                error_message=str(e),
            )

    # Only advance the sync cursor if at least one order was processed successfully.
    # This ensures failed orders are retried on the next run.
    if imported or updated:
        frappe.db.set_single_value(
            "Magento Settings", "last_order_sync_time", frappe.utils.now_datetime()
        )
        frappe.db.commit()
    elif not failed:
        # All orders were skipped (cancelled, already imported, etc.) — still advance cursor
        frappe.db.set_single_value(
            "Magento Settings", "last_order_sync_time", frappe.utils.now_datetime()
        )
        frappe.db.commit()

    summary = {
        "total_fetched": len(orders),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
    }

    logger.info(f"sync_orders: done — {summary}")

    if imported or updated:
        create_log(
            operation="Order Pull",
            status="Success",
            response_payload=summary,
        )

    if failed and not imported and not updated:
        create_log(
            operation="Order Pull",
            status="Failed",
            error_message=f"All {failed} orders failed to import.",
            response_payload=summary,
        )

    return summary


def _process_order(order, client):
    """
    Process a single Magento order.
    Returns 'imported', 'updated', or 'skipped'.
    """
    magento_order_id = order.get("entity_id")
    magento_increment_id = order.get("increment_id")
    magento_status = order.get("status", "")

    if is_order_imported(magento_order_id):
        _sync_status_from_magento(magento_order_id, magento_status)
        return "updated"

    if magento_status in ("canceled",):
        create_log(
            operation="Order Pull",
            status="Skipped",
            magento_id=magento_increment_id,
            error_message=f"Skipped cancelled Magento order {magento_increment_id}",
        )
        return "skipped"

    customer_name = get_or_create_customer(order)
    address_name = get_or_create_address(order, customer_name)

    items = _build_order_items(order)
    if not items:
        frappe.logger("connector").warning(
            f"sync_orders: order {magento_increment_id} has no matching ERPNext items. "
            f"Magento line items: {[m.get('sku') for m in (order.get('items') or [])]}"
        )
        create_log(
            operation="Order Pull",
            status="Failed",
            magento_id=magento_increment_id,
            error_message=(
                f"No valid items found for order {magento_increment_id}. "
                f"SKUs from Magento: {[m.get('sku') for m in (order.get('items') or [])]}"
            ),
        )
        return "skipped"

    taxes = _build_taxes_and_charges(order)

    settings = frappe.get_single("Magento Settings")
    lead_time = int(settings.lead_time_days or 3)
    delivery_date = add_days(nowdate(), lead_time)

    so = frappe.new_doc("Sales Order")
    so.customer = customer_name
    so.delivery_date = delivery_date
    so.order_type = "Sales"
    so.currency = order.get("order_currency_code") or "USD"

    if address_name:
        so.shipping_address_name = address_name
        so.shipping_address = frappe.db.get_value("Address", address_name, "address_display")

    so.magento_order_id = magento_order_id
    so.magento_increment_id = magento_increment_id
    so.magento_order_status = magento_status

    note = MAGENTO_STATUS_NOTES.get(magento_status, f"Magento status: {magento_status}")
    so.po_no = magento_increment_id
    so.remarks = f"Imported from Magento. Order #{magento_increment_id}. Status: {note}"

    for item_row in items:
        so.append("items", item_row)

    for tax_row in taxes:
        so.append("taxes", tax_row)

    so.flags.ignore_permissions = True
    so.insert()
    frappe.db.commit()

    create_map(magento_order_id, magento_increment_id, magento_status, so.name)

    create_log(
        operation="Order Pull",
        status="Success",
        doctype_name="Sales Order",
        document_name=so.name,
        magento_id=magento_increment_id,
        response_payload={"sales_order": so.name, "customer": customer_name},
    )
    return "imported"


def _build_order_items(order):
    """
    Convert Magento order items to ERPNext Sales Order Items.
    Skips items whose SKU doesn't exist in ERPNext and logs a warning.
    """
    line_items = []
    for mitem in order.get("items") or []:
        if float(mitem.get("qty_ordered") or 0) <= 0:
            continue
        if mitem.get("product_type") in ("configurable", "bundle"):
            continue

        sku = mitem.get("sku") or ""
        if not frappe.db.exists("Item", sku):
            frappe.logger("connector").warning(
                f"Magento order {order.get('increment_id')}: SKU '{sku}' not found in ERPNext — skipping item."
            )
            continue

        item_row = {
            "item_code": sku,
            "item_name": mitem.get("name") or sku,
            "qty": float(mitem.get("qty_ordered") or 1),
            "rate": float(mitem.get("price") or 0),
            "uom": frappe.db.get_value("Item", sku, "stock_uom") or "Nos",
            "delivery_date": add_days(nowdate(), int(frappe.db.get_single_value("Magento Settings", "lead_time_days") or 3)),
        }
        line_items.append(item_row)

    return line_items


def _build_taxes_and_charges(order):
    """
    Map Magento tax and shipping charges to ERPNext Sales Taxes and Charges rows.
    """
    charges = []
    company = frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )

    tax_amount = float(order.get("tax_amount") or 0)
    shipping_amount = float(order.get("shipping_amount") or 0)
    shipping_tax = float(order.get("shipping_tax_amount") or 0)

    if tax_amount > 0:
        tax_account = _get_tax_account(company)
        if tax_account:
            charges.append({
                "charge_type": "Actual",
                "account_head": tax_account,
                "description": "Magento Tax",
                "tax_amount": tax_amount,
            })

    if shipping_amount > 0:
        freight_account = _get_freight_account(company)
        if freight_account:
            charges.append({
                "charge_type": "Actual",
                "account_head": freight_account,
                "description": f"Shipping: {order.get('shipping_description') or 'Freight'}",
                "tax_amount": shipping_amount + shipping_tax,
            })

    return charges


def _get_tax_account(company):
    """Return a tax payable account for the company, or None."""
    account = frappe.db.get_value(
        "Account",
        {"account_type": "Tax", "company": company, "disabled": 0},
        "name",
    )
    return account


def _get_freight_account(company):
    """Return a freight/shipping income account for the company, or None."""
    account = frappe.db.get_value(
        "Account",
        {
            "account_name": ["like", "%freight%"],
            "company": company,
            "disabled": 0,
        },
        "name",
    )
    if not account:
        account = frappe.db.get_value(
            "Account",
            {"account_type": "Income Account", "company": company, "disabled": 0},
            "name",
        )
    return account


def _sync_status_from_magento(magento_order_id, new_magento_status):
    """
    Called when a previously imported order is seen again with a different status.
    Updates the Magento Order Map and adds a comment to the Sales Order.
    """
    sales_order_name = get_sales_order_for_magento_order(magento_order_id)
    if not sales_order_name:
        return

    update_status(magento_order_id, new_magento_status)

    frappe.db.set_value("Sales Order", sales_order_name, "magento_order_status", new_magento_status)
    frappe.db.commit()

    note = MAGENTO_STATUS_NOTES.get(new_magento_status, f"Magento status changed to: {new_magento_status}")
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        so.add_comment("Comment", text=f"[Magento Sync] {note}")
    except Exception:
        pass
