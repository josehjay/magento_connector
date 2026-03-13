"""
Payment Sync: Magento → ERPNext Payment Entry pre-fill

Exposes a whitelisted endpoint called from the Sales Invoice client script.
When a Sales Invoice was raised against a Magento-originated Sales Order, this
module fetches the Magento order's payment data and returns it in a shape that
the client-side dialog can use to pre-fill a Payment Entry.
"""

import frappe

from connector.api.magento_client import MagentoAPIError, MagentoClient

# ---------------------------------------------------------------------------
# Magento payment method code → ERPNext Mode of Payment name
# Extend this dict to suit your installed modes of payment.
# ---------------------------------------------------------------------------
_METHOD_MAP: dict[str, str] = {
    "checkmo":         "Cash",
    "free":            "Cash",
    "cashondelivery":  "Cash",
    "cod":             "Cash",
    "banktransfer":    "Bank Transfer",
    "bank_transfer":   "Bank Transfer",
    "neft":            "Bank Transfer",
    "stripe":          "Credit Card",
    "stripe_payments": "Credit Card",
    "braintree":       "Credit Card",
    "adyen":           "Credit Card",
    "paypal":          "PayPal",
    "paypal_express":  "PayPal",
    "mpesa":           "M-Pesa",
    "mpesa_express":   "M-Pesa",
    "safaricom_mpesa": "M-Pesa",
    "pesapal":         "M-Pesa",
}


# ---------------------------------------------------------------------------
# Public whitelisted API
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_magento_payment_details(sales_invoice: str) -> dict:
    """
    Fetch payment details from Magento for a Sales Invoice that was created
    from a Magento order.

    Returns a dict with:
      ok            (bool)
      reason        (str)  – populated when ok=False
      magento_order_id, increment_id
      method_code, method_label
      mode_of_payment  – mapped to an ERPNext Mode of Payment if possible
      paid_amount      – what Magento recorded as paid
      outstanding_amount – from the ERPNext Sales Invoice
      reference_no     – transaction ID, cheque number, or order #
      reference_date   – payment/order date  (YYYY-MM-DD)
      currency
      remarks
      sales_order      – linked ERPNext SO name
      customer         – SI customer name
    """
    si = frappe.get_doc("Sales Invoice", sales_invoice)

    # ── 1. Find a linked Sales Order ───────────────────────────────────────
    so_name = next(
        (item.sales_order for item in si.items if item.sales_order),
        None,
    )
    if not so_name:
        return {"ok": False, "reason": "No linked Sales Order found on this invoice."}

    so = frappe.get_doc("Sales Order", so_name)
    magento_order_id  = so.get("magento_order_id")
    magento_increment = so.get("magento_increment_id")

    if not magento_order_id:
        return {
            "ok": False,
            "reason": (
                "This invoice is not linked to a Magento order. "
                "The Sales Order has no Magento Order ID."
            ),
        }

    # ── 2. Fetch order from Magento ────────────────────────────────────────
    try:
        client = MagentoClient()
        order  = client.get_order(int(magento_order_id))
    except MagentoAPIError as exc:
        return {"ok": False, "reason": f"Magento API error: {exc}"}
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"get_magento_payment_details: {sales_invoice}",
        )
        return {"ok": False, "reason": "Unexpected error fetching order from Magento."}

    # ── 3. Extract payment object ──────────────────────────────────────────
    payment = order.get("payment") or {}

    method_code  = (payment.get("method") or "").lower()
    method_label = _extract_method_label(payment)

    amount_paid      = _f(payment.get("amount_paid") or payment.get("base_amount_paid") or order.get("grand_total"))
    amount_remaining = _f(payment.get("amount_remaining") or 0)

    transaction_ref = _extract_transaction_ref(payment)
    mode_of_payment = _map_to_erpnext_mop(method_code)

    created_at = (order.get("created_at") or "")
    ref_date   = created_at[:10] if created_at else frappe.utils.today()

    currency = order.get("order_currency_code") or si.currency

    outstanding = _f(si.outstanding_amount)

    increment_id = magento_increment or order.get("increment_id") or ""

    return {
        "ok":               True,
        "magento_order_id": magento_order_id,
        "increment_id":     str(increment_id),
        "method_code":      method_code,
        "method_label":     method_label or method_code,
        "mode_of_payment":  mode_of_payment,
        "paid_amount":      amount_paid,
        "outstanding_amount": outstanding,
        # Suggest the smaller of (amount_paid, outstanding) so partial payments are safe
        "suggested_amount": round(min(amount_paid, outstanding), 2) if outstanding > 0 else amount_paid,
        "reference_no":     transaction_ref or str(increment_id),
        "reference_date":   ref_date,
        "currency":         currency,
        "remarks":          (
            f"Magento Order #{increment_id} — Payment via {method_label or method_code}"
        ),
        "sales_order":      so_name,
        "customer":         si.customer,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _f(value) -> float:
    """Safe float conversion."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_method_label(payment: dict) -> str:
    """
    Return a human-readable payment method label.
    Magento stores it in the additional_information list.
    """
    info = payment.get("additional_information") or []
    if isinstance(info, list) and info:
        return str(info[0])
    if isinstance(info, str):
        return info
    return payment.get("method", "")


def _extract_transaction_ref(payment: dict) -> str:
    """Pull the best transaction reference from a Magento payment object."""
    for key in ("transaction_id", "cc_trans_id", "last_trans_id", "po_number", "check_number"):
        val = payment.get(key)
        if val:
            return str(val)
    return ""


def _map_to_erpnext_mop(method_code: str) -> str:
    """
    Map a Magento payment method code to an ERPNext Mode of Payment name.
    Verifies the candidate actually exists in the ERPNext database.
    Falls back to a partial-match scan, then returns "" so the user picks manually.
    """
    # Direct map
    candidate = _METHOD_MAP.get(method_code)
    if candidate and frappe.db.exists("Mode of Payment", candidate):
        return candidate

    # Partial match against all installed Modes of Payment
    all_mops: list[str] = frappe.get_all("Mode of Payment", pluck="name")
    for mop in all_mops:
        if method_code and (method_code in mop.lower() or mop.lower() in method_code):
            return mop

    return ""
