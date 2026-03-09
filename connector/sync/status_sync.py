"""
Status Sync: ERPNext Sales Order Events → Magento Order Status

Hooks:
  - Sales Order.on_submit  → set Magento status to 'processing'
  - Sales Order.on_cancel  → cancel the Magento order

Also called from order_sync.py to handle Magento-side status changes.
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


def on_sales_order_submit(doc, method):
    """
    Hook: Sales Order → on_submit
    Notifies Magento that the order is now being processed.
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
        job_name=f"magento_order_status_{magento_order_id}_processing",
        sales_order=doc.name,
        magento_order_id=magento_order_id,
        magento_status="processing",
        comment=f"Order {doc.name} confirmed and submitted in ERPNext.",
    )


def on_sales_order_cancel(doc, method):
    """
    Hook: Sales Order → on_cancel
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
        job_name=f"magento_order_cancel_{magento_order_id}",
        sales_order=doc.name,
        magento_order_id=magento_order_id,
    )


def push_order_status_to_magento(sales_order, magento_order_id, magento_status, comment=""):
    """
    Update Magento order status by adding a status history comment.
    Called asynchronously via frappe.enqueue.
    """
    try:
        client = MagentoClient()
        client.update_order_status(
            order_id=magento_order_id,
            status=magento_status,
            comment=comment,
        )
        _update_order_map_status(magento_order_id, magento_status)
        frappe.db.set_value("Sales Order", sales_order, "magento_order_status", magento_status)
        frappe.db.commit()

        create_log(
            operation="Status Sync",
            status="Success",
            doctype_name="Sales Order",
            document_name=sales_order,
            magento_id=str(magento_order_id),
            response_payload={"new_status": magento_status},
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
    """
    Cancel the Magento order via POST /V1/orders/{id}/cancel.
    Called asynchronously via frappe.enqueue.
    """
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


def _is_sync_enabled():
    if not _is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


def _update_order_map_status(magento_order_id, new_status):
    from connector.connector.doctype.magento_order_map.magento_order_map import (
        update_status,
    )
    update_status(magento_order_id, new_status)
