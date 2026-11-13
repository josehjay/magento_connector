"""
Order Sync: Magento Orders → ERPNext Sales Orders (Draft)

Two paths into ERPNext:
  1. Real-time PUSH  — the Magento Kitabu_ErpNextConnector extension calls
     receive_order() and receive_order_status() immediately on order events.
  2. Reconciliation PULL — sync_orders() runs every 4 hours as a safety net
     to catch any orders that were missed by the real-time push.

Both paths call _process_order(), which deduplicates via Magento Order Map.
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
from connector.security.request_signing import verify_incoming_signed_request
from connector.security.permissions import require_admin, require_push_endpoint_access
from connector.utils import is_magento_enabled


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


def _sync_disabled_reason():
    """Return None when order sync is enabled, otherwise which switch is off.
    Same checks as connector.utils.is_magento_sync_enabled(), but names the
    switch so the push endpoint can report it back to Magento."""
    if not is_magento_enabled():
        return "Connector Settings → Enable Magento Integration is off"
    if not frappe.db.get_single_value("Magento Settings", "sync_enabled"):
        return "Magento Settings → Sync Enabled is off"
    return None


def _is_sync_enabled():
    return _sync_disabled_reason() is None


DEFAULT_LEAD_TIME_DAYS = 3

# Totals within this much of Magento's grand_total are treated as equal
# (per-line rounding differences between the two systems).
GRAND_TOTAL_TOLERANCE = 0.01


def _lead_time_days(value):
    """Magento Settings → lead_time_days, honouring an explicit 0 (same-day
    delivery date); the default applies only when the setting is unset."""
    if value is None or value == "":
        return DEFAULT_LEAD_TIME_DAYS
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_LEAD_TIME_DAYS


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@frappe.whitelist()
def run_order_sync_now():
    """
    Whitelisted entry point for running order sync directly (not enqueued).
    System Manager only; scheduler/desk buttons call sync_orders() directly.
    """
    require_admin("Only System Manager can run order sync.")
    frappe.logger("connector").info("run_order_sync_now: starting direct order sync.")
    result = sync_orders()
    frappe.logger("connector").info(f"run_order_sync_now: done. result={result}")
    return result


@frappe.whitelist()
def receive_order(order_payload):
    """
    Push endpoint: the Magento Kitabu_ErpNextConnector module calls this
    immediately when a new order is placed, passing the Magento REST order
    object as JSON.  Processes the order synchronously and returns a result
    dict containing the created/found Sales Order name.
    """
    import json

    logger = frappe.logger("connector")
    require_push_endpoint_access("receive_order")
    verify_incoming_signed_request("receive_order")

    disabled_reason = _sync_disabled_reason()
    if disabled_reason:
        logger.warning(f"receive_order: rejected — {disabled_reason}.")
        create_log(
            operation="Order Pull",
            status="Failed",
            error_message=f"Order push from Magento rejected: {disabled_reason}",
        )
        return {"ok": False, "status": "failed", "reason": f"sync_disabled: {disabled_reason}"}

    try:
        order = json.loads(order_payload) if isinstance(order_payload, str) else order_payload
    except (TypeError, ValueError) as exc:
        return {"ok": False, "status": "failed", "reason": f"invalid_payload: {exc}"}
    if not isinstance(order, dict):
        return {"ok": False, "status": "failed", "reason": "invalid_payload: expected a JSON object"}

    increment_id = order.get("increment_id", "?")
    logger.info(f"receive_order: processing pushed order #{increment_id}")

    try:
        result = _process_order(order, client=None)
        frappe.db.commit()

        status      = result.get("status") if isinstance(result, dict) else str(result)
        so_name     = result.get("sales_order") if isinstance(result, dict) else None
        customer    = result.get("customer") if isinstance(result, dict) else None

        logger.info(
            f"receive_order: order #{increment_id} → status={status}"
            + (f", sales_order={so_name}" if so_name else "")
        )
        return {"ok": True, "status": status, "sales_order": so_name, "customer": customer}
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"receive_order: failed for order #{increment_id}")
        create_log(
            operation="Order Pull",
            status="Failed",
            magento_id=str(increment_id),
            error_message=f"Order #{increment_id}: {exc}",
        )
        frappe.db.commit()
        return {"ok": False, "status": "failed", "reason": str(exc)}


@frappe.whitelist()
def receive_order_status(entity_id, status):
    """
    Push endpoint: the Magento Kitabu_ErpNextConnector module calls this when
    an existing order's status changes in Magento.
    """
    logger = frappe.logger("connector")
    require_push_endpoint_access("receive_order_status")
    verify_incoming_signed_request("receive_order_status")

    if not _is_sync_enabled():
        logger.info("receive_order_status: skipped — sync is disabled.")
        return {"ok": False, "reason": "sync_disabled"}

    try:
        _sync_status_from_magento(int(entity_id), str(status))
        frappe.db.commit()
        logger.info(f"receive_order_status: synced entity_id={entity_id} status={status}")
        return {"ok": True}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"receive_order_status: failed entity_id={entity_id}")
        return {"ok": False, "reason": str(exc)}


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

    # On first run (no last_sync), default to the last 90 days to avoid fetching
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
        # Advance cursor by current server time — no Magento timestamps available.
        frappe.db.set_single_value(
            "Magento Settings", "last_order_sync_time",
            _safe_now()
        )
        frappe.db.commit()
        return {"status": "ok", "orders_fetched": 0}

    logger.info(f"sync_orders: fetched {len(orders)} orders from Magento.")

    imported = 0
    updated = 0
    skipped = 0
    failed = 0
    failed_orders = []

    for order in orders:
        increment_id = order.get("increment_id", "?")
        try:
            result = _process_order(order, client)
            status = result.get("status") if isinstance(result, dict) else str(result)
            if status == "imported":
                imported += 1
            elif status == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            failed_orders.append(order)
            frappe.db.rollback()
            tb = frappe.get_traceback()
            frappe.log_error(tb, f"Magento Order Sync Error: order #{increment_id}")
            create_log(
                operation="Order Pull",
                status="Failed",
                magento_id=str(order.get("entity_id", "")),
                error_message=f"Order #{increment_id}: {e}",
                response_payload={"traceback_logged": True},
            )

    # Advance the cursor using Magento's own updated_at timestamps (avoids
    # server-clock / timezone drift). Never move it past a failed order: if any
    # failed, stop just before the earliest failure so the next run re-fetches
    # it. Orders already imported are deduplicated by Magento Order Map.
    if not failed:
        new_cursor = _cursor_from_orders(orders)
    else:
        new_cursor = _cursor_before_orders(failed_orders)
    if new_cursor:
        frappe.db.set_single_value("Magento Settings", "last_order_sync_time", new_cursor)
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
    Returns a dict: {"status": "imported"|"updated"|"skipped", "sales_order": str|None, ...}
    Raises RuntimeError on unrecoverable error so the caller can log it.
    `client` may be None when called from the push endpoint (receive_order).

    Transaction: the buyer customer, addresses, Sales Order and Magento Order
    Map row are written with commit=False and committed ONCE at the end, after
    create_map. If anything raises in between, the caller (receive_order /
    sync_orders) rolls the whole order back, so a failed import never leaves an
    orphan customer or address. Nothing between the first write and that
    commit may call create_log (it commits). The skip paths (already imported,
    cancelled, no matching items) all return before the first write.
    """
    raw_entity_id        = order.get("entity_id")
    magento_order_id     = int(raw_entity_id) if raw_entity_id else None
    magento_increment_id = order.get("increment_id")
    magento_status       = order.get("status", "")
    logger               = frappe.logger("connector")

    # Guard: entity_id is only populated after the Magento order is written to
    # the DB.  The old sales_order_place_after event fired before the save, so
    # payloads from the retry queue may still carry entity_id=null.  Fail fast
    # with a clear message so the Magento retry queue knows to try again.
    if not magento_order_id:
        # Before failing, check if we already imported this increment_id so we
        # do not raise on a legitimate duplicate-push that had no entity_id.
        if magento_increment_id and is_order_imported(None, magento_increment_id):
            so_name = frappe.db.get_value(
                "Magento Order Map",
                {"magento_increment_id": str(magento_increment_id)},
                "sales_order",
            )
            return {"status": "updated", "sales_order": so_name}
        raise RuntimeError(
            f"Order #{magento_increment_id}: entity_id is null — order not yet committed "
            "to Magento DB. The Magento extension must be updated to push on "
            "sales_order_save_after instead of sales_order_place_after."
        )

    if is_order_imported(magento_order_id, magento_increment_id):
        _sync_status_from_magento(magento_order_id, magento_status)
        so_name = get_sales_order_for_magento_order(magento_order_id)
        return {"status": "updated", "sales_order": so_name}

    if magento_status in ("canceled",):
        create_log(
            operation="Order Pull",
            status="Skipped",
            magento_id=magento_increment_id,
            error_message=f"Skipped cancelled order #{magento_increment_id}",
        )
        return {"status": "skipped", "reason": "cancelled"}

    # ----- Line items -----
    # Resolved BEFORE any customer/address write so that an order with no
    # matching items is skipped without creating anything (the skip log below
    # commits, which would otherwise persist an orphan buyer record).
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
        return {"status": "skipped", "reason": "no_matching_items", "skus": skipped_skus}

    # ----- Customer -----
    # Always create/find the real buyer's customer record — we need it for the
    # shipping address even when a default billing customer is configured.
    # If a default "Web Sales"-style customer is configured, bill to that
    # customer; otherwise bill to the real buyer directly.
    settings_snap = frappe.get_single("Magento Settings")
    default_customer = (settings_snap.get("default_customer") or "").strip()

    # A savepoint lets the default-customer fallback discard whatever a failed
    # customer insert half-wrote, without losing the rest of this transaction.
    frappe.db.savepoint("magento_order_customer")
    try:
        real_customer_name = get_or_create_customer(order, commit=False)
    except Exception as e:
        frappe.db.rollback(save_point="magento_order_customer")
        frappe.log_error(frappe.get_traceback(), f"Order #{magento_increment_id}: customer creation failed")
        if not default_customer:
            raise RuntimeError(f"Customer creation failed: {e}") from e
        # The buyer record is optional when billing to the default customer:
        # buyer name/email/phone still go into the Sales Order remarks, and the
        # addresses are linked to the default customer instead.
        logger.warning(
            f"Order #{magento_increment_id}: buyer customer could not be created ({e}); "
            f"continuing with default customer '{default_customer}'."
        )
        real_customer_name = default_customer

    so_customer = default_customer if default_customer else real_customer_name

    # ----- Address -----
    # When billing to a default customer (e.g. "Web Sales"), the address must
    # also be linked to that customer to pass ERPNext's party-address validation.
    # Create new Magento-derived address docs per order when a default customer
    # is configured so names and displays never drift across orders.
    shipping_address_name = None
    billing_address_name = None
    frappe.db.savepoint("magento_order_address")
    try:
        shipping_address_name = get_or_create_address(
            order,
            real_customer_name,
            also_link_to=so_customer,
            always_create_new=bool(default_customer),
            address_kind="shipping",
            commit=False,
        )
        billing_address_name = get_or_create_address(
            order,
            real_customer_name,
            also_link_to=so_customer,
            always_create_new=bool(default_customer),
            address_kind="billing",
            commit=False,
        )
    except Exception as e:
        # Address is not mandatory — log and continue. Roll back to the
        # savepoint so a half-written address is not committed with the order.
        frappe.db.rollback(save_point="magento_order_address")
        shipping_address_name = billing_address_name = None
        logger.warning(f"Order #{magento_increment_id}: address creation failed (continuing): {e}")

    # ----- Taxes & charges -----
    taxes = _build_taxes_and_charges(order)

    # ----- Build Sales Order -----
    lead_time = _lead_time_days(settings_snap.lead_time_days)
    delivery_date = add_days(nowdate(), lead_time)

    company = _get_default_company()

    so = frappe.new_doc("Sales Order")
    # Explicitly set mandatory header fields before set_missing_values runs,
    # because background jobs has no user session to pull defaults from.
    if company:
        so.company = company
    so.customer = so_customer
    so.delivery_date = delivery_date
    so.order_type = "Sales"
    so.currency = _get_valid_currency(order.get("order_currency_code"))

    # Ensure the selling price list is set (mandatory in ERPNext)
    if not so.selling_price_list:
        default_pl = frappe.db.get_single_value("Selling Settings", "selling_price_list")
        if default_pl:
            so.selling_price_list = default_pl

    if shipping_address_name:
        try:
            so.shipping_address_name = shipping_address_name
            so.shipping_address = (
                frappe.db.get_value("Address", shipping_address_name, "address_display") or ""
            )
        except Exception:
            pass  # address display is cosmetic; continue
    if billing_address_name:
        try:
            so.customer_address = billing_address_name
            so.address_display = (
                frappe.db.get_value("Address", billing_address_name, "address_display") or ""
            )
        except Exception:
            pass  # address display is cosmetic; continue

    so.magento_order_id = magento_order_id
    so.magento_increment_id = magento_increment_id
    so.magento_order_status = magento_status

    note = MAGENTO_STATUS_NOTES.get(magento_status, f"Magento status: {magento_status}")
    so.po_no = str(magento_increment_id)

    # Build human-readable buyer summary for remarks
    billing = order.get("billing_address") or {}
    buyer_first = (billing.get("firstname") or order.get("customer_firstname") or "").strip()
    buyer_last  = (billing.get("lastname")  or order.get("customer_lastname")  or "").strip()
    buyer_name  = f"{buyer_first} {buyer_last}".strip()
    buyer_email = (order.get("customer_email") or "").strip()
    buyer_phone = (billing.get("telephone") or "").strip()

    buyer_parts = [p for p in [buyer_name, buyer_email, buyer_phone] if p]
    buyer_info  = f" | Buyer: {' | '.join(buyer_parts)}" if buyer_parts else ""

    so.remarks = f"Magento Order #{magento_increment_id}.{buyer_info} Status: {note}"

    # ----- Discount (coupon / cart price rule) -----
    # Lines are priced from Magento's `price` (excl. tax, BEFORE discount) and
    # Magento's tax and shipping are added as "Actual" tax rows, so without
    # this the SO total exceeds Magento's by the discount. Magento's
    # discount_amount is negative and already includes any shipping discount.
    # Applying it as an additional discount on "Net Total" lowers the net
    # total by exactly that amount while the Actual rows stay fixed (Actual
    # charges are not recomputed from the discounted net), so:
    #   SO grand_total = sum(price * qty) - discount + tax_amount + shipping_amount
    # which is how Magento builds grand_total. ("Grand Total" would instead
    # spread the discount across the tax rows and change the tax figures.)
    discount = abs(_to_float(order.get("discount_amount")))
    if discount > 0:
        so.apply_discount_on = "Net Total"
        so.additional_discount_percentage = 0
        so.discount_amount = discount
        discount_label = (order.get("discount_description") or "").strip() or "Magento discount"
        so.remarks += f" Discount: {discount_label} (-{discount:.2f})."

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

    # set_missing_values may override party-address fields based on defaults.
    # Re-apply Magento-derived addresses so both the displayed text and the
    # linked address names stay consistent with this order.
    if shipping_address_name:
        try:
            so.shipping_address_name = shipping_address_name
            so.shipping_address = (
                frappe.db.get_value("Address", shipping_address_name, "address_display") or ""
            )
        except Exception:
            pass
    if billing_address_name:
        try:
            so.customer_address = billing_address_name
            so.address_display = (
                frappe.db.get_value("Address", billing_address_name, "address_display") or ""
            )
        except Exception:
            pass

    # ERPNext rejects stock items without a source warehouse ("Source warehouse
    # required for stock item"). set_missing_values only fills it from Item
    # Defaults, so fall back to the Stock Settings / company default warehouse.
    _apply_default_warehouse(so)

    try:
        so.run_method("calculate_taxes_and_totals")
    except Exception as e:
        logger.warning(f"Order #{magento_increment_id}: calculate_taxes_and_totals warning (non-fatal): {e}")

    # Totals check: never fail the import over a mismatch (the order is real and
    # paid in Magento), but make it visible on the SO and in the sync log.
    totals_mismatch = _grand_total_mismatch(order, so)
    if totals_mismatch:
        logger.warning(f"Order #{magento_increment_id}: {totals_mismatch}")
        so.remarks += f" {totals_mismatch}"

    try:
        so.insert()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Order #{magento_increment_id}: Sales Order insert failed")
        raise RuntimeError(f"Sales Order insert failed: {e}") from e

    # Insert the map row in the SAME transaction as the Sales Order, then make
    # the one commit for the whole order (customer, addresses, SO, map).
    # magento_order_id is unique on the map, so if the push and the 4-hourly
    # pull race on one order, the loser fails here and its caller rolls back
    # its Sales Order and customer — instead of committing a duplicate SO.
    create_map(magento_order_id, magento_increment_id, magento_status, so.name, commit=False)
    frappe.db.commit()

    create_log(
        operation="Order Pull",
        status="Success",
        doctype_name="Sales Order",
        document_name=so.name,
        magento_id=magento_increment_id,
        error_message=totals_mismatch or None,
        response_payload={"sales_order": so.name, "customer": so_customer, "buyer": real_customer_name},
    )
    return {"status": "imported", "sales_order": so.name, "customer": so_customer, "real_customer": real_customer_name}


def _grand_total_mismatch(order, so):
    """Return a note when the Sales Order grand total differs from Magento's
    grand_total (both in the order currency) by more than the tolerance,
    otherwise None. Orders without a grand_total are not checked."""
    if order.get("grand_total") in (None, ""):
        return None
    try:
        magento_total = float(order.get("grand_total"))
        so_total = float(so.grand_total or 0)
    except (TypeError, ValueError):
        return None
    difference = round(so_total - magento_total, 2)
    if abs(difference) <= GRAND_TOTAL_TOLERANCE:
        return None
    return (
        f"Totals differ from Magento by {difference:+.2f} "
        f"(ERPNext {so_total:.2f} vs Magento {magento_total:.2f})."
    )


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


def _get_default_warehouse(company):
    """Stock Settings default warehouse (if it belongs to the company), else the
    company's first enabled leaf warehouse."""
    default = frappe.db.get_single_value("Stock Settings", "default_warehouse")
    if default and frappe.db.get_value("Warehouse", default, "company") == company:
        return default
    return frappe.db.get_value(
        "Warehouse",
        {"company": company, "is_group": 0, "disabled": 0},
        "name",
        order_by="creation asc",
    )


def _apply_default_warehouse(so):
    missing = [
        row for row in so.get("items") or []
        if not row.get("warehouse")
        and frappe.db.get_value("Item", row.item_code, "is_stock_item")
    ]
    if not missing:
        return
    warehouse = _get_default_warehouse(so.company)
    if not warehouse:
        return  # ERPNext validation will report the missing warehouse
    for row in missing:
        row.warehouse = warehouse


def _safe_now():
    """Return current time as a clean YYYY-MM-DD HH:MM:SS string (no microseconds)."""
    return str(frappe.utils.now_datetime()).split(".")[0]


def _cursor_from_orders(orders):
    """
    Derive the next sync cursor from the MAX updated_at across the fetched orders.

    This avoids timezone drift: Magento stores updated_at in UTC.  ERPNext's
    now_datetime() uses the server's local clock (which may be UTC+3 on a Kenya
    server).  If we store a server-local timestamp and compare it against
    Magento's UTC timestamps the filter drifts by the UTC offset and new orders
    are silently excluded.

    By using Magento's own timestamp (+ 1 second buffer) as the cursor we stay
    in the same timezone as the data we're querying against.
    """
    from datetime import datetime, timedelta
    max_ts = None
    for o in orders:
        ts = (o.get("updated_at") or "").strip()[:19]  # strip microseconds
        if ts and (max_ts is None or ts > max_ts):
            max_ts = ts
    if max_ts:
        try:
            dt = datetime.strptime(max_ts, "%Y-%m-%d %H:%M:%S")
            return (dt + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return _safe_now()


def _cursor_before_orders(orders):
    """Cursor one second before the earliest updated_at among `orders` (the pull
    filter is `updated_at > cursor`, so those orders are fetched again).
    Returns None when no timestamp is available — the cursor is then left as is."""
    from datetime import datetime, timedelta
    stamps = [(o.get("updated_at") or "").strip()[:19] for o in orders]
    stamps = [s for s in stamps if s]
    if not stamps:
        return None
    try:
        dt = datetime.strptime(min(stamps), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (dt - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


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
    lead_time = _lead_time_days(frappe.db.get_single_value("Magento Settings", "lead_time_days"))
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
    # Same resolver as the Sales Order header, so accounts match so.company.
    company = _get_default_company()

    # Magento's order-level tax_amount is the TOTAL tax, shipping tax included,
    # so shipping is charged net here (adding shipping_tax_amount again would
    # double-count it).
    tax_amount = float(order.get("tax_amount") or 0)
    shipping_amount = float(order.get("shipping_amount") or 0)

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
                "tax_amount": shipping_amount,
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

    # Map row and Sales Order field change together: one commit for both.
    update_status(magento_order_id, new_magento_status, commit=False)
    frappe.db.set_value("Sales Order", sales_order_name, "magento_order_status", new_magento_status)
    frappe.db.commit()

    note = MAGENTO_STATUS_NOTES.get(new_magento_status, f"Magento status: {new_magento_status}")
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        so.add_comment("Comment", text=f"[Magento Sync] {note}")
    except Exception:
        pass
