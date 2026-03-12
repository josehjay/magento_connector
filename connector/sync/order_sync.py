"""
Order Sync: Magento Orders → ERPNext Sales Orders (Draft)

Scheduled every 10 minutes via tasks.py.
Pulls new and updated orders from Magento, creates/updates Draft Sales Orders.
Also syncs Magento-side status changes back to ERPNext.
"""

import frappe
from frappe.utils import add_days, nowdate
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

    # On first run (no last_sync), default to the last 30 days to avoid fetching
    # the entire order history which times out on large Magento catalogs.
    if not last_sync:
        from datetime import datetime, timedelta
        last_sync = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"sync_orders: first run — defaulting to last 90 days ({last_sync}).")
    else:
        # Normalise to YYYY-MM-DD HH:MM:SS — strip microseconds and any timezone suffix
        last_sync_str = str(last_sync).split(".")[0].split("+")[0].strip()
        logger.info(f"sync_orders: fetching orders updated after {last_sync_str}.")
        last_sync = last_sync_str

    try:
        client = MagentoClient()
        orders = client.get_all_new_orders(updated_after=last_sync)
    except Exception as e:
        # Do NOT advance the cursor on fetch failure — let the next run retry.
        frappe.log_error(frappe.get_traceback(), "Magento Order Sync: Failed to fetch orders")
        create_log(operation="Order Pull", status="Failed", error_message=str(e))
        return {"status": "error", "reason": f"fetch_failed: {e}"}

    if not orders:
        logger.info("sync_orders: Magento returned 0 orders. Nothing to process.")
        # Advance cursor only if the fetch itself succeeded (no exception above).
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
        increment_id = order.get("increment_id", "?")
        try:
            result = _process_order(order, client)
            if result == "imported":
                imported += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            tb = frappe.get_traceback()
            frappe.log_error(tb, f"Magento Order Sync Error: order #{increment_id}")
            create_log(
                operation="Order Pull",
                status="Failed",
                magento_id=str(order.get("entity_id", "")),
                error_message=f"Order #{increment_id}: {e}",
                response_payload={"traceback_logged": True},
            )

    # Advance cursor only when at least one order was successfully imported/updated,
    # or when all were legitimately skipped (cancelled, already imported).
    # If ALL orders failed, keep the cursor so they are retried next run.
    if not failed or imported or updated:
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
        create_log(operation="Order Pull", status="Success", response_payload=summary)

    if failed and not imported and not updated:
        create_log(
            operation="Order Pull",
            status="Failed",
            error_message=f"{failed} order(s) failed. See Error Log for details.",
            response_payload=summary,
        )

    return summary


def _process_order(order, client):
    """
    Process a single Magento order.
    Returns 'imported', 'updated', or 'skipped'.
    Raises on unrecoverable error so the caller can log it.
    """
    magento_order_id = order.get("entity_id")
    magento_increment_id = order.get("increment_id")
    magento_status = order.get("status", "")
    logger = frappe.logger("connector")

    if is_order_imported(magento_order_id):
        _sync_status_from_magento(magento_order_id, magento_status)
        return "updated"

    if magento_status in ("canceled",):
        create_log(
            operation="Order Pull",
            status="Skipped",
            magento_id=magento_increment_id,
            error_message=f"Skipped cancelled order #{magento_increment_id}",
        )
        return "skipped"

    # ----- Customer -----
    try:
        customer_name = get_or_create_customer(order)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Order #{magento_increment_id}: customer creation failed")
        raise RuntimeError(f"Customer creation failed: {e}") from e

    # ----- Address -----
    address_name = None
    try:
        address_name = get_or_create_address(order, customer_name)
    except Exception as e:
        # Address is not mandatory — log and continue
        logger.warning(f"Order #{magento_increment_id}: address creation failed (continuing): {e}")

    # ----- Line items -----
    items = _build_order_items(order)
    if not items:
        skipped_skus = [
            f"{m.get('sku')} (type={m.get('product_type')})"
            for m in (order.get("items") or [])
            if float(m.get("qty_ordered") or 0) > 0
        ]
        msg = (
            f"Order #{magento_increment_id}: no matching ERPNext items. "
            f"Magento SKUs: {skipped_skus}. "
            f"Ensure these item_codes exist in ERPNext."
        )
        logger.warning(f"sync_orders: {msg}")
        create_log(
            operation="Order Pull",
            status="Failed",
            magento_id=magento_increment_id,
            error_message=msg,
        )
        return "skipped"

    # ----- Taxes & charges -----
    taxes = _build_taxes_and_charges(order)

    # ----- Build Sales Order -----
    settings = frappe.get_single("Magento Settings")
    lead_time = int(settings.lead_time_days or 3)
    delivery_date = add_days(nowdate(), lead_time)

    company = _get_default_company()

    so = frappe.new_doc("Sales Order")
    # Explicitly set mandatory header fields before set_missing_values runs,
    # because background jobs have no user session to pull defaults from.
    if company:
        so.company = company
    so.customer = customer_name
    so.delivery_date = delivery_date
    so.order_type = "Sales"
    so.currency = _get_valid_currency(order.get("order_currency_code"))

    # Ensure the selling price list is set (mandatory in ERPNext)
    if not so.selling_price_list:
        default_pl = frappe.db.get_single_value("Selling Settings", "selling_price_list")
        if default_pl:
            so.selling_price_list = default_pl

    if address_name:
        try:
            so.shipping_address_name = address_name
            so.shipping_address = frappe.db.get_value("Address", address_name, "address_display") or ""
        except Exception:
            pass  # address display is cosmetic; continue

    so.magento_order_id = magento_order_id
    so.magento_increment_id = magento_increment_id
    so.magento_order_status = magento_status

    note = MAGENTO_STATUS_NOTES.get(magento_status, f"Magento status: {magento_status}")
    so.po_no = str(magento_increment_id)
    so.remarks = f"Imported from Magento. Order #{magento_increment_id}. Status: {note}"

    for item_row in items:
        so.append("items", item_row)

    for tax_row in taxes:
        so.append("taxes", tax_row)

    so.flags.ignore_permissions = True

    # Trigger ERPNext's own defaulting and calculation hooks
    try:
        so.run_method("set_missing_values")
    except Exception as e:
        logger.warning(f"Order #{magento_increment_id}: set_missing_values warning (non-fatal): {e}")

    try:
        so.run_method("calculate_taxes_and_totals")
    except Exception as e:
        logger.warning(f"Order #{magento_increment_id}: calculate_taxes_and_totals warning (non-fatal): {e}")

    try:
        so.insert()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Order #{magento_increment_id}: Sales Order insert failed")
        raise RuntimeError(f"Sales Order insert failed: {e}") from e

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


def _get_default_company():
    """Return the default ERPNext company. Works in background jobs where there is no user session."""
    company = (
        frappe.defaults.get_defaults().get("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )
    if not company:
        # Final fallback: first company in the system
        company = frappe.db.get_value("Company", {}, "name")
    return company


def _get_valid_currency(currency_code):
    """
    Return currency_code if it exists in ERPNext, otherwise fall back to
    the company's default currency, then 'USD'.
    """
    if currency_code and frappe.db.exists("Currency", currency_code):
        return currency_code
    default = frappe.db.get_single_value("Global Defaults", "default_currency") or "USD"
    frappe.logger("connector").warning(
        f"Currency '{currency_code}' not found in ERPNext — using '{default}'."
    )
    return default


def _build_order_items(order):
    """
    Convert Magento order items to ERPNext Sales Order Items.

    Magento sends both the configurable parent AND the simple child in the
    items array.  The configurable row is skipped; only the simple/virtual/
    downloadable rows are used.  If a variant SKU isn't found in ERPNext,
    the code also tries the configurable parent's SKU as a fallback.
    """
    logger = frappe.logger("connector")
    increment_id = order.get("increment_id", "?")
    lead_time = int(frappe.db.get_single_value("Magento Settings", "lead_time_days") or 3)
    delivery_date = add_days(nowdate(), lead_time)

    # Build a map of parent_item_id → configurable SKU for fallback lookups
    configurable_sku_map = {}
    for mitem in order.get("items") or []:
        if mitem.get("product_type") == "configurable":
            configurable_sku_map[mitem.get("item_id")] = mitem.get("sku", "")

    line_items = []
    for mitem in order.get("items") or []:
        qty = float(mitem.get("qty_ordered") or 0)
        if qty <= 0:
            continue

        product_type = mitem.get("product_type", "")

        # Configurable and bundle parent rows are duplicates of the child rows
        if product_type in ("configurable", "bundle"):
            continue

        sku = (mitem.get("sku") or "").strip()
        item_code = None

        if sku and frappe.db.exists("Item", sku):
            item_code = sku
        else:
            # Fallback: try the configurable parent SKU for this row
            parent_id = mitem.get("parent_item_id")
            if parent_id:
                parent_sku = configurable_sku_map.get(parent_id, "")
                if parent_sku and frappe.db.exists("Item", parent_sku):
                    item_code = parent_sku
                    logger.info(
                        f"Order #{increment_id}: variant SKU '{sku}' not in ERPNext, "
                        f"using configurable parent '{parent_sku}'."
                    )

        if not item_code:
            logger.warning(
                f"Order #{increment_id}: SKU '{sku}' (type={product_type}) "
                f"not found in ERPNext — skipping line item."
            )
            continue

        line_items.append({
            "item_code": item_code,
            "item_name": mitem.get("name") or item_code,
            "qty": qty,
            "rate": float(mitem.get("price") or 0),
            "uom": frappe.db.get_value("Item", item_code, "stock_uom") or "Nos",
            "delivery_date": delivery_date,
        })

    return line_items


def _build_taxes_and_charges(order):
    """
    Map Magento tax and shipping charges to ERPNext Sales Taxes and Charges rows.
    Skips any charge whose account cannot be found to prevent insert failures.
    """
    charges = []
    company = (
        frappe.defaults.get_defaults().get("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
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
    """Return a tax account for the company, or None."""
    return frappe.db.get_value(
        "Account",
        {"account_type": "Tax", "company": company, "disabled": 0},
        "name",
    )


def _get_freight_account(company):
    """
    Return a suitable shipping/freight expense account.
    Only returns accounts that are valid for Sales Taxes and Charges
    (Expense Account or Income Account types). Returns None if not found
    rather than using an arbitrary account that may fail validation.
    """
    # Prefer an explicit freight/shipping account
    account = frappe.db.get_value(
        "Account",
        {
            "account_name": ["like", "%freight%"],
            "company": company,
            "account_type": ["in", ["Expense Account", "Income Account", "Tax"]],
            "disabled": 0,
        },
        "name",
    )
    if account:
        return account

    account = frappe.db.get_value(
        "Account",
        {
            "account_name": ["like", "%shipping%"],
            "company": company,
            "account_type": ["in", ["Expense Account", "Income Account", "Tax"]],
            "disabled": 0,
        },
        "name",
    )
    if account:
        return account

    # Final fallback: any Tax account (widely available in ERPNext)
    account = frappe.db.get_value(
        "Account",
        {"account_type": "Tax", "company": company, "disabled": 0},
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

    note = MAGENTO_STATUS_NOTES.get(new_magento_status, f"Magento status: {new_magento_status}")
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        so.add_comment("Comment", text=f"[Magento Sync] {note}")
    except Exception:
        pass
