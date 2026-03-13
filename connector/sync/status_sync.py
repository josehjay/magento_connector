"""
Status Sync: ERPNext document events → Magento order lifecycle

ERPNext lifecycle → Magento actions:

  Sales Order.on_submit  → Magento "processing"  (order confirmed in ERP)
  Sales Order.on_cancel  → Magento cancel

  Delivery Note.on_submit → Magento shipment created
  Delivery Note.on_cancel → status comment added to Magento order

  Sales Invoice.on_submit → Magento invoice created
  Sales Invoice.on_cancel → status comment added to Magento order

  When both DN and SI are fully submitted and the SO status becomes
  "Completed" → Magento "complete"
"""

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_sync_log.magento_sync_log import create_log


# ── Guards ─────────────────────────────────────────────────────────────────


def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def _is_sync_enabled():
    if not _is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


# ── Sales Order hooks ──────────────────────────────────────────────────────


def on_sales_order_submit(doc, method):
    """
    Hook: Sales Order.on_submit
    The user has reviewed and confirmed the draft order → tell Magento it's processing.
    """
    if not _is_sync_enabled():
        return

    magento_order_id = doc.get("magento_order_id")
    if not magento_order_id:
        return

    frappe.enqueue(
        "connector.sync.status_sync.push_order_status_to_magento",
        queue="short",
        timeout=60,
        job_id=f"magento_order_status_{magento_order_id}_processing",
        deduplicate=True,
        sales_order=doc.name,
        magento_order_id=magento_order_id,
        magento_status="processing",
        comment=f"ERPNext Sales Order {doc.name} has been confirmed and submitted.",
    )


def on_sales_order_cancel(doc, method):
    """
    Hook: Sales Order.on_cancel
    Cancels the corresponding Magento order.
    """
    if not _is_sync_enabled():
        return

    magento_order_id = doc.get("magento_order_id")
    if not magento_order_id:
        return

    frappe.enqueue(
        "connector.sync.status_sync.cancel_magento_order",
        queue="short",
        timeout=60,
        job_id=f"magento_order_cancel_{magento_order_id}",
        deduplicate=True,
        sales_order=doc.name,
        magento_order_id=magento_order_id,
    )


# ── Delivery Note hooks ────────────────────────────────────────────────────


def on_delivery_note_submit(doc, method):
    """
    Hook: Delivery Note.on_submit
    For every Magento-linked Sales Order on this DN, create a Magento shipment.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_dn(doc):
        frappe.enqueue(
            "connector.sync.status_sync.create_magento_shipment",
            queue="short",
            timeout=120,
            job_id=f"magento_shipment_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            dn_name=doc.name,
        )


def on_delivery_note_cancel(doc, method):
    """
    Hook: Delivery Note.on_cancel
    Magento doesn't support shipment deletion, but we add a comment to the order.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_dn(doc):
        frappe.enqueue(
            "connector.sync.status_sync.push_order_status_to_magento",
            queue="short",
            timeout=60,
            job_id=f"magento_dn_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            magento_status=None,
            comment=(
                f"Delivery Note {doc.name} was cancelled in ERPNext. "
                "Please review shipment status in Magento manually."
            ),
        )


# ── Sales Invoice hooks ────────────────────────────────────────────────────


def on_sales_invoice_submit(doc, method):
    """
    Hook: Sales Invoice.on_submit
    For every Magento-linked Sales Order on this invoice, create a Magento invoice.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        frappe.enqueue(
            "connector.sync.status_sync.create_magento_invoice",
            queue="short",
            timeout=120,
            job_id=f"magento_invoice_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            si_name=doc.name,
        )


def on_sales_invoice_cancel(doc, method):
    """
    Hook: Sales Invoice.on_cancel
    Magento invoices can't be deleted, but we add an order comment.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        frappe.enqueue(
            "connector.sync.status_sync.push_order_status_to_magento",
            queue="short",
            timeout=60,
            job_id=f"magento_si_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            magento_status=None,
            comment=(
                f"Sales Invoice {doc.name} was cancelled in ERPNext. "
                "Please review invoice status in Magento manually."
            ),
        )


# ── Background jobs ────────────────────────────────────────────────────────


def push_order_status_to_magento(sales_order, magento_order_id, magento_status, comment=""):
    """
    Add a status-history comment to the Magento order.
    When magento_status is None, only the comment is added (informational update).
    Called asynchronously via frappe.enqueue.
    """
    logger = frappe.logger("connector")
    try:
        client = MagentoClient()

        if magento_status:
            client.update_order_status(
                order_id=magento_order_id,
                status=magento_status,
                comment=comment,
            )
            _update_order_map_status(magento_order_id, magento_status)
            frappe.db.set_value("Sales Order", sales_order, "magento_order_status", magento_status)
            frappe.db.commit()
            logger.info(f"push_order_status_to_magento: {sales_order} → Magento {magento_status}")
        else:
            # Comment-only update (no status change)
            client.update_order_status(
                order_id=magento_order_id,
                status="",
                comment=comment,
            )

        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"new_status": magento_status or "comment_only", "comment": comment[:200]},
        )
    except MagentoAPIError as e:
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=str(e),
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Magento Status Sync Error: {sales_order}")
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=str(e),
        )


def cancel_magento_order(sales_order, magento_order_id):
    """POST /V1/orders/{id}/cancel — cancel the Magento order."""
    try:
        client = MagentoClient()
        client.cancel_order(magento_order_id)
        _update_order_map_status(magento_order_id, "canceled")
        frappe.db.set_value("Sales Order", sales_order, "magento_order_status", "canceled")
        frappe.db.commit()

        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"action": "order_cancelled"},
        )
    except MagentoAPIError as e:
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=str(e),
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Magento Cancel Order Error: {sales_order}")


def create_magento_shipment(sales_order, magento_order_id, dn_name):
    """
    Background job: create a Magento shipment matching this Delivery Note.

    Maps ERPNext item_codes to Magento order_item_ids by SKU.
    Skips items already fully shipped in Magento.
    After creating the shipment, checks if the SO is now Completed and marks
    the Magento order as 'complete' if so.
    """
    logger = frappe.logger("connector")

    # Idempotency: skip if already synced successfully
    if _already_synced("Delivery Note", dn_name, "Shipment Sync"):
        logger.info(f"create_magento_shipment: {dn_name} already synced — skipping.")
        return

    try:
        client = MagentoClient()
        magento_items = _get_magento_order_items(client, magento_order_id)
        if not magento_items:
            raise ValueError(f"Could not fetch items for Magento order {magento_order_id}.")

        dn = frappe.get_doc("Delivery Note", dn_name)

        # Build shipment items — match by SKU, cap by remaining unshipped qty
        ship_items = []
        for item in dn.items:
            mi = magento_items.get(item.item_code)
            if not mi:
                logger.info(
                    f"create_magento_shipment: item {item.item_code} not in Magento order "
                    f"{magento_order_id} — skipping."
                )
                continue
            qty_remaining = mi["qty_ordered"] - mi["qty_shipped"]
            qty = min(float(item.qty), qty_remaining)
            if qty > 0:
                ship_items.append({"order_item_id": mi["order_item_id"], "qty": qty})

        if not ship_items:
            logger.info(
                f"create_magento_shipment: no shippable items for Magento order "
                f"{magento_order_id} (already shipped or SKU mismatch)."
            )
            create_log(
                operation="Shipment Sync",
                status="Skipped",
                doctype_name="Delivery Note",
                document_name=dn_name,
                magento_id=str(magento_order_id),
                error_message="No shippable items — all already shipped or SKU not matched.",
            )
            return

        # Build tracking from ERPNext lr_no (lorry receipt / tracking number)
        tracks = []
        if dn.get("lr_no"):
            tracks.append({
                "track_number": dn.lr_no,
                "title": "Courier",
                "carrier_code": "custom",
            })

        result = client.create_shipment(
            order_id=magento_order_id,
            items=ship_items,
            tracks=tracks or None,
            notify=True,
        )

        shipment_id = result if isinstance(result, int) else (result or {}).get("id")

        # Add a status comment so admins can see the shipment reference
        comment = f"Shipment created via ERPNext Delivery Note {dn_name}."
        if dn.get("lr_no"):
            comment += f" Tracking: {dn.lr_no}."
        client.update_order_status(
            order_id=magento_order_id,
            status="processing",
            comment=comment,
        )

        create_log(
            operation="Shipment Sync",
            status="Success",
            doctype_name="Delivery Note",
            document_name=dn_name,
            magento_id=str(magento_order_id),
            response_payload={
                "magento_shipment_id": shipment_id,
                "items_shipped": len(ship_items),
            },
        )
        logger.info(
            f"create_magento_shipment: {dn_name} → Magento shipment {shipment_id} "
            f"({len(ship_items)} items) for order {magento_order_id}."
        )

        # Check if the SO is now fully Completed
        _maybe_complete_magento_order(sales_order, magento_order_id)

    except MagentoAPIError as e:
        err = str(e)
        # Magento returns 400/500 when order cannot be shipped (already shipped, wrong state)
        already_done = any(k in err.lower() for k in (
            "already been shipped", "can not ship", "cannot ship", "not in a valid state"
        ))
        if already_done:
            create_log(
                operation="Shipment Sync",
                status="Skipped",
                doctype_name="Delivery Note",
                document_name=dn_name,
                magento_id=str(magento_order_id),
                error_message=f"Magento rejected shipment (order already shipped?): {err[:300]}",
            )
        else:
            frappe.log_error(frappe.get_traceback(), f"Magento Shipment Sync Error: {dn_name}")
            create_log(
                operation="Shipment Sync",
                status="Failed",
                doctype_name="Delivery Note",
                document_name=dn_name,
                magento_id=str(magento_order_id),
                error_message=err[:500],
            )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Magento Shipment Sync Error: {dn_name}")
        create_log(
            operation="Shipment Sync",
            status="Failed",
            doctype_name="Delivery Note",
            document_name=dn_name,
            magento_id=str(magento_order_id),
            error_message=str(e)[:500],
        )


def create_magento_invoice(sales_order, magento_order_id, si_name):
    """
    Background job: create a Magento invoice matching this Sales Invoice.

    Maps ERPNext item_codes to Magento order_item_ids by SKU.
    Skips items already fully invoiced in Magento.
    After creating the invoice, checks if the SO is now Completed and marks
    the Magento order as 'complete' if so.
    """
    logger = frappe.logger("connector")

    # Idempotency: skip if already synced successfully
    if _already_synced("Sales Invoice", si_name, "Invoice Sync"):
        logger.info(f"create_magento_invoice: {si_name} already synced — skipping.")
        return

    try:
        client = MagentoClient()
        magento_items = _get_magento_order_items(client, magento_order_id)
        if not magento_items:
            raise ValueError(f"Could not fetch items for Magento order {magento_order_id}.")

        si = frappe.get_doc("Sales Invoice", si_name)

        # Build invoice items — match by SKU, cap by remaining uninvoiced qty
        inv_items = []
        for item in si.items:
            item_code = item.item_code
            mi = magento_items.get(item_code)
            if not mi:
                logger.info(
                    f"create_magento_invoice: item {item_code} not in Magento order "
                    f"{magento_order_id} — skipping."
                )
                continue
            qty_remaining = mi["qty_ordered"] - mi["qty_invoiced"]
            qty = min(float(item.qty), qty_remaining)
            if qty > 0:
                inv_items.append({"order_item_id": mi["order_item_id"], "qty": qty})

        if not inv_items:
            logger.info(
                f"create_magento_invoice: no invoiceable items for Magento order "
                f"{magento_order_id} (already invoiced or SKU mismatch)."
            )
            create_log(
                operation="Invoice Sync",
                status="Skipped",
                doctype_name="Sales Invoice",
                document_name=si_name,
                magento_id=str(magento_order_id),
                error_message="No invoiceable items — all already invoiced or SKU not matched.",
            )
            return

        result = client.create_invoice(
            order_id=magento_order_id,
            items=inv_items,
            capture=False,
            notify=False,
        )

        invoice_id = result if isinstance(result, int) else (result or {}).get("id")

        client.update_order_status(
            order_id=magento_order_id,
            status="processing",
            comment=(
                f"Invoice created via ERPNext Sales Invoice {si_name}. "
                f"Total: {si.currency} {si.grand_total}."
            ),
        )

        create_log(
            operation="Invoice Sync",
            status="Success",
            doctype_name="Sales Invoice",
            document_name=si_name,
            magento_id=str(magento_order_id),
            response_payload={
                "magento_invoice_id": invoice_id,
                "items_invoiced": len(inv_items),
            },
        )
        logger.info(
            f"create_magento_invoice: {si_name} → Magento invoice {invoice_id} "
            f"({len(inv_items)} items) for order {magento_order_id}."
        )

        # Check if the SO is now fully Completed
        _maybe_complete_magento_order(sales_order, magento_order_id)

    except MagentoAPIError as e:
        err = str(e)
        already_done = any(k in err.lower() for k in (
            "already been invoiced", "can not invoice", "cannot invoice",
            "already invoiced", "not in a valid state"
        ))
        if already_done:
            create_log(
                operation="Invoice Sync",
                status="Skipped",
                doctype_name="Sales Invoice",
                document_name=si_name,
                magento_id=str(magento_order_id),
                error_message=f"Magento rejected invoice (order already invoiced?): {err[:300]}",
            )
        else:
            frappe.log_error(frappe.get_traceback(), f"Magento Invoice Sync Error: {si_name}")
            create_log(
                operation="Invoice Sync",
                status="Failed",
                doctype_name="Sales Invoice",
                document_name=si_name,
                magento_id=str(magento_order_id),
                error_message=err[:500],
            )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Magento Invoice Sync Error: {si_name}")
        create_log(
            operation="Invoice Sync",
            status="Failed",
            doctype_name="Sales Invoice",
            document_name=si_name,
            magento_id=str(magento_order_id),
            error_message=str(e)[:500],
        )


# ── Private helpers ────────────────────────────────────────────────────────


def _update_order_map_status(magento_order_id, new_status):
    from connector.connector.doctype.magento_order_map.magento_order_map import update_status
    update_status(magento_order_id, new_status)


def _already_synced(doctype, doc_name, operation):
    """Return True if this document was already successfully pushed to Magento."""
    return bool(frappe.db.exists(
        "Magento Sync Log",
        {"operation": operation, "doctype_name": doctype, "document_name": doc_name, "status": "Success"},
    ))


def _get_magento_order_items(client, magento_order_id):
    """
    Fetch the Magento order and return a dict  {sku: {order_item_id, qty_ordered,
    qty_shipped, qty_invoiced}}  for use when building shipment/invoice item lists.

    Skips configurable parent rows (they duplicate the child simple rows).
    """
    try:
        order = client.get_order(magento_order_id)
        result = {}
        for mi in (order.get("items") or []):
            if mi.get("product_type") == "configurable":
                continue
            sku = (mi.get("sku") or "").strip()
            if sku:
                result[sku] = {
                    "order_item_id": int(mi.get("item_id") or 0),
                    "qty_ordered":   float(mi.get("qty_ordered")  or 0),
                    "qty_shipped":   float(mi.get("qty_shipped")   or 0),
                    "qty_invoiced":  float(mi.get("qty_invoiced")  or 0),
                }
        return result
    except MagentoAPIError as e:
        frappe.logger("connector").warning(
            f"_get_magento_order_items: could not fetch order {magento_order_id}: {e}"
        )
        return {}


def _magento_orders_for_dn(dn):
    """
    Yield (so_name, magento_order_id) tuples for every Magento-linked SO
    referenced in a Delivery Note's items.
    """
    seen = set()
    for item in (dn.get("items") or []):
        so_name = item.get("against_sales_order") or ""
        if not so_name or so_name in seen:
            continue
        seen.add(so_name)
        magento_order_id = frappe.db.get_value("Sales Order", so_name, "magento_order_id")
        if magento_order_id:
            yield so_name, int(magento_order_id)


def _magento_orders_for_si(si):
    """
    Yield (so_name, magento_order_id) tuples for every Magento-linked SO
    referenced in a Sales Invoice's items.
    """
    seen = set()
    for item in (si.get("items") or []):
        so_name = item.get("sales_order") or ""
        if not so_name or so_name in seen:
            continue
        seen.add(so_name)
        magento_order_id = frappe.db.get_value("Sales Order", so_name, "magento_order_id")
        if magento_order_id:
            yield so_name, int(magento_order_id)


def _maybe_complete_magento_order(sales_order, magento_order_id):
    """
    Called after creating a Magento shipment or invoice.
    If the ERPNext Sales Order is now fully Completed (all items delivered AND billed),
    update Magento to 'complete'.
    """
    so_status = frappe.db.get_value("Sales Order", sales_order, "status")
    if so_status != "Completed":
        return

    # Guard: only push once — check if we've already marked it complete
    current_magento_status = frappe.db.get_value(
        "Sales Order", sales_order, "magento_order_status"
    ) or ""
    if current_magento_status in ("complete", "closed"):
        return

    frappe.logger("connector").info(
        f"_maybe_complete_magento_order: {sales_order} is Completed → pushing 'complete' to Magento."
    )

    push_order_status_to_magento(
        sales_order=sales_order,
        magento_order_id=magento_order_id,
        magento_status="complete",
        comment=(
            f"Order {sales_order} is fully delivered and invoiced in ERPNext. "
            "Marking complete in Magento."
        ),
    )
