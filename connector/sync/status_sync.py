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
    logger = frappe.logger("connector")
    logger.info(f"on_sales_order_submit: fired for {doc.name}")

    if not _is_sync_enabled():
        logger.info(f"on_sales_order_submit: sync disabled — skipping {doc.name}")
        return

    magento_order_id = doc.get("magento_order_id")
    if not magento_order_id:
        logger.info(f"on_sales_order_submit: {doc.name} has no magento_order_id — not a Magento order, skipping")
        return

    logger.info(f"on_sales_order_submit: enqueuing status push for {doc.name} (Magento #{magento_order_id})")

    frappe.enqueue(
        "connector.sync.status_sync._push_processing_comment",
        queue="short",
        timeout=120,
        job_id=f"magento_so_submit_{doc.name}",
        deduplicate=True,
        enqueue_after_commit=True,
        sales_order=doc.name,
        magento_order_id=int(magento_order_id),
        comment=f"Order confirmed in ERPNext (Sales Order {doc.name}). Now being processed.",
    )


def on_sales_order_cancel(doc, method):
    """
    Hook: Sales Order.on_cancel → cancel the Magento order.
    """
    logger = frappe.logger("connector")
    logger.info(f"on_sales_order_cancel: fired for {doc.name}")

    if not _is_sync_enabled():
        logger.info(f"on_sales_order_cancel: sync disabled — skipping {doc.name}")
        return

    magento_order_id = doc.get("magento_order_id")
    if not magento_order_id:
        logger.info(f"on_sales_order_cancel: {doc.name} has no magento_order_id — skipping")
        return

    logger.info(f"on_sales_order_cancel: enqueuing cancel for {doc.name} (Magento #{magento_order_id})")

    frappe.enqueue(
        "connector.sync.status_sync.cancel_magento_order",
        queue="short",
        timeout=120,
        job_id=f"magento_order_cancel_{doc.name}",
        deduplicate=True,
        enqueue_after_commit=True,
        sales_order=doc.name,
        magento_order_id=int(magento_order_id),
    )


# ── Delivery Note hooks ────────────────────────────────────────────────────


def on_delivery_note_submit(doc, method):
    """
    Hook: Delivery Note.on_submit
    Adds a "goods dispatched" comment to each linked Magento order.
    """
    logger = frappe.logger("connector")
    logger.info(f"on_delivery_note_submit: fired for {doc.name}")

    if not _is_sync_enabled():
        logger.info(f"on_delivery_note_submit: sync disabled — skipping")
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

        logger.info(
            f"on_delivery_note_submit: enqueuing comment for {so_name} (Magento #{magento_order_id})"
        )

        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=120,
            job_id=f"magento_dn_submit_{doc.name}_{magento_order_id}",
            deduplicate=True,
            enqueue_after_commit=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=comment,
        )


def on_delivery_note_cancel(doc, method):
    """Hook: Delivery Note.on_cancel — adds an informational comment."""
    logger = frappe.logger("connector")
    logger.info(f"on_delivery_note_cancel: fired for {doc.name}")

    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_dn(doc):
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=120,
            job_id=f"magento_dn_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            enqueue_after_commit=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=f"Delivery Note {doc.name} was cancelled in ERPNext.",
        )


# ── Sales Invoice hooks ────────────────────────────────────────────────────


def on_sales_invoice_submit(doc, method):
    """Hook: Sales Invoice.on_submit — adds an "invoice raised" comment."""
    logger = frappe.logger("connector")
    logger.info(f"on_sales_invoice_submit: fired for {doc.name}")

    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        comment = (
            f"Sales Invoice {doc.name} raised in ERPNext. "
            f"Amount: {doc.currency} {doc.grand_total}."
        )
        logger.info(
            f"on_sales_invoice_submit: enqueuing comment for {so_name} (Magento #{magento_order_id})"
        )
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=120,
            job_id=f"magento_si_submit_{doc.name}_{magento_order_id}",
            deduplicate=True,
            enqueue_after_commit=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=comment,
        )


def on_sales_invoice_cancel(doc, method):
    """Hook: Sales Invoice.on_cancel — adds an informational comment."""
    logger = frappe.logger("connector")
    logger.info(f"on_sales_invoice_cancel: fired for {doc.name}")

    if not _is_sync_enabled():
        return

    for so_name, magento_order_id in _magento_orders_for_si(doc):
        frappe.enqueue(
            "connector.sync.status_sync._push_processing_comment",
            queue="short",
            timeout=120,
            job_id=f"magento_si_cancel_{doc.name}_{magento_order_id}",
            deduplicate=True,
            enqueue_after_commit=True,
            sales_order=so_name,
            magento_order_id=magento_order_id,
            comment=f"Sales Invoice {doc.name} was cancelled in ERPNext.",
        )


# ── Background jobs ────────────────────────────────────────────────────────


def _push_processing_comment(sales_order, magento_order_id, comment):
    """
    Add a status-history comment to the Magento order, explicitly setting
    the status to "processing".  This is the single background job used for
    all non-cancel ERPNext fulfilment events.
    """
    logger = frappe.logger("connector")
    logger.info(
        f"_push_processing_comment: starting — SO={sales_order}, "
        f"Magento order={magento_order_id}"
    )

    try:
        client = MagentoClient()

        # ── Step 1: add comment + set status via the status-history endpoint ──
        result = client.update_order_status(
            order_id=magento_order_id,
            status="processing",
            comment=comment,
            notify_customer=False,
        )
        logger.info(
            f"_push_processing_comment: comments endpoint returned {result!r} "
            f"for Magento order {magento_order_id}"
        )

        # ── Step 2: also patch the order entity to ensure state/status change ──
        # The comments endpoint sets the status label; patching the entity
        # ensures the underlying state is also updated and the change is visible
        # in the Magento admin order list.
        try:
            client.update_order_entity_status(magento_order_id, "processing")
            logger.info(
                f"_push_processing_comment: entity patch succeeded for "
                f"Magento order {magento_order_id}"
            )
        except MagentoAPIError as patch_err:
            # Non-fatal: the comment was already posted successfully.
            # The entity patch is best-effort.
            logger.warning(
                f"_push_processing_comment: entity patch failed (non-fatal) for "
                f"Magento order {magento_order_id}: {patch_err}"
            )

        # ── Step 3: mirror into ERPNext ───────────────────────────────────────
        frappe.db.set_value(
            "Sales Order", sales_order, "magento_order_status", "processing",
            update_modified=False,
        )
        _update_order_map_status(magento_order_id, "processing")
        frappe.db.commit()

        logger.info(
            f"_push_processing_comment: SUCCESS — {sales_order} / "
            f"Magento {magento_order_id} → processing"
        )
        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"status": "processing", "comment": comment[:200]},
        )

    except MagentoAPIError as exc:
        logger.error(
            f"_push_processing_comment: Magento API error for {sales_order} "
            f"/ Magento {magento_order_id}: {exc} "
            f"(HTTP {exc.status_code}) body={exc.response_body}"
        )
        frappe.log_error(
            f"SO: {sales_order}\nMagento order: {magento_order_id}\n"
            f"Comment: {comment}\n\nError: {exc}\n"
            f"HTTP status: {exc.status_code}\nBody: {exc.response_body}",
            "Magento Status Sync Error",
        )
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=f"[HTTP {exc.status_code}] {exc}",
        )

    except Exception as exc:
        logger.error(
            f"_push_processing_comment: unexpected error for {sales_order}: {exc}"
        )
        frappe.log_error(frappe.get_traceback(), f"Magento Status Sync Error: {sales_order}")
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=str(exc),
        )


# Kept for backwards compatibility
push_order_status_to_magento = _push_processing_comment


def cancel_magento_order(sales_order, magento_order_id):
    """POST /V1/orders/{id}/cancel"""
    logger = frappe.logger("connector")
    logger.info(f"cancel_magento_order: cancelling SO={sales_order}, Magento order={magento_order_id}")
    try:
        client = MagentoClient()
        client.cancel_order(magento_order_id)
        _update_order_map_status(magento_order_id, "canceled")
        frappe.db.set_value(
            "Sales Order", sales_order, "magento_order_status", "canceled",
            update_modified=False,
        )
        frappe.db.commit()

        logger.info(f"cancel_magento_order: SUCCESS — {sales_order} / Magento {magento_order_id}")
        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"action": "order_cancelled"},
        )
    except MagentoAPIError as exc:
        logger.error(
            f"cancel_magento_order: API error for {sales_order}: {exc} "
            f"(HTTP {exc.status_code})"
        )
        frappe.log_error(
            f"SO: {sales_order}\nMagento order: {magento_order_id}\n"
            f"Error: {exc}\nHTTP status: {exc.status_code}\nBody: {exc.response_body}",
            "Magento Cancel Order Error",
        )
        create_log(
            operation="Status Sync",
            status="Failed",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            error_message=f"[HTTP {exc.status_code}] {exc}",
        )
    except Exception as exc:
        logger.error(f"cancel_magento_order: unexpected error for {sales_order}: {exc}")
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
