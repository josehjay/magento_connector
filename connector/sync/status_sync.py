"""
Status Sync: ERPNext document events → Magento order status comments

Simple, one-way rule: the Magento order stays at "processing" for the entire
ERPNext fulfilment workflow.  Each step adds an informational comment to the
Magento order history so the Magento admin can see what is happening.

  Sales Order.on_submit    → Magento "processing" + comment
  Sales Order.on_cancel    → Magento order cancelled

  Delivery Note.on_submit  → Magento "processing" comment (goods dispatched)
  Sales Invoice.on_submit  → Magento "processing" comment (invoice raised)

The Magento admin marks the order "complete" manually once physical delivery
is confirmed.  The ERPNext connector never auto-completes.
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
    Confirms the order in ERPNext and notifies Magento to set status → processing.
    """
    if not _is_sync_enabled():
        return

    magento_order_id = doc.get("magento_order_id")
    if not magento_order_id:
        return

    frappe.enqueue(
        "connector.sync.status_sync._push_processing_comment",
        queue="short",
        timeout=60,
        job_id=f"magento_so_submit_{magento_order_id}",
        deduplicate=True,
        sales_order=doc.name,
        magento_order_id=int(magento_order_id),
        comment=f"Order confirmed in ERPNext (Sales Order {doc.name}). Now being processed.",
    )


def on_sales_order_cancel(doc, method):
    """
    Hook: Sales Order.on_cancel → cancel the Magento order.
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
        magento_order_id=int(magento_order_id),
    )


# ── Delivery Note hooks ────────────────────────────────────────────────────


def on_delivery_note_submit(doc, method):
    """
    Hook: Delivery Note.on_submit
    Adds a "goods dispatched" comment to each linked Magento order.
    Status stays at processing — the admin completes in Magento on delivery.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_dn(doc):
        items_summary = ", ".join(
            f"{i.item_code} × {i.qty}"
            for i in (doc.get("items") or [])
            if i.get("against_sales_order") == so_name
        ) or "—"

        tracking = doc.get("lr_no") or ""
        comment = (
            f"Delivery Note {doc.name} submitted in ERPNext — goods dispatched. "
            f"Items: {items_summary}."
        )
        if tracking:
            comment += f" Tracking: {tracking}."

        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=60,
            job_id=f"magento_dn_submit_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=comment,
        )


def on_delivery_note_cancel(doc, method):
    """
    Hook: Delivery Note.on_cancel
    Adds an informational comment — no status change.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_dn(doc):
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=60,
            job_id=f"magento_dn_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=f"Delivery Note {doc.name} was cancelled in ERPNext.",
        )


# ── Sales Invoice hooks ────────────────────────────────────────────────────


def on_sales_invoice_submit(doc, method):
    """
    Hook: Sales Invoice.on_submit
    Adds an "invoice raised" comment to each linked Magento order.
    Status stays at processing.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        comment = (
            f"Sales Invoice {doc.name} raised in ERPNext. "
            f"Amount: {doc.currency} {doc.grand_total}."
        )
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=60,
            job_id=f"magento_si_submit_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=comment,
        )


def on_sales_invoice_cancel(doc, method):
    """
    Hook: Sales Invoice.on_cancel
    Adds an informational comment — no status change.
    """
    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=60,
            job_id=f"magento_si_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=f"Sales Invoice {doc.name} was cancelled in ERPNext.",
        )


# ── Background jobs ────────────────────────────────────────────────────────


def _push_processing_comment(sales_order, magento_order_id, comment):
    """
    Add a status-history comment to the Magento order, keeping status = processing.
    This is the single background job used for all non-cancel events.
    """
    logger = frappe.logger("connector")
    try:
        client = MagentoClient()
        client.update_order_status(
            order_id=magento_order_id,
            status="processing",
            comment=comment,
            notify_customer=False,
        )

        # Keep the ERPNext magento_order_status field in sync
        frappe.db.set_value("Sales Order", sales_order, "magento_order_status", "processing")
        _update_order_map_status(magento_order_id, "processing")
        frappe.db.commit()

        logger.info(
            f"_push_processing_comment: {sales_order} / Magento {magento_order_id} "
            f"→ processing. Comment: {comment[:80]}"
        )
        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"status": "processing", "comment": comment[:200]},
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


# Kept for backwards compatibility — called by on_sales_order_submit via old name
push_order_status_to_magento = _push_processing_comment


def cancel_magento_order(sales_order, magento_order_id):
    """POST /V1/orders/{id}/cancel"""
    logger = frappe.logger("connector")
    try:
        client = MagentoClient()
        client.cancel_order(magento_order_id)
        _update_order_map_status(magento_order_id, "canceled")
        frappe.db.set_value("Sales Order", sales_order, "magento_order_status", "canceled")
        frappe.db.commit()

        logger.info(f"cancel_magento_order: {sales_order} / Magento {magento_order_id} cancelled.")
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


# ── Private helpers ────────────────────────────────────────────────────────


def _update_order_map_status(magento_order_id, new_status):
    from connector.connector.doctype.magento_order_map.magento_order_map import update_status
    update_status(magento_order_id, new_status)


def _magento_orders_for_dn(dn):
    """Yield (so_name, magento_order_id) for every Magento-linked SO in this DN."""
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
    """Yield (so_name, magento_order_id) for every Magento-linked SO in this SI."""
    seen = set()
    for item in (si.get("items") or []):
        so_name = item.get("sales_order") or ""
        if not so_name or so_name in seen:
            continue
        seen.add(so_name)
        magento_order_id = frappe.db.get_value("Sales Order", so_name, "magento_order_id")
        if magento_order_id:
            yield so_name, int(magento_order_id)
