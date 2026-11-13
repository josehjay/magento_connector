"""
Role / permission guards for whitelisted connector endpoints.

`@frappe.whitelist()` (without allow_guest) lets ANY logged-in user call a
method, whatever their roles. Every module-level whitelisted function in this
app calls one of the guards below first.

Guards belong in the whitelisted wrappers only. Scheduler tasks, doc_events
hooks and enqueued background jobs call the unguarded internal functions, so
background sync never starts failing with PermissionError.
"""

import frappe


ADMINISTRATOR = "Administrator"
SYSTEM_MANAGER = "System Manager"

# Manual sync triggers, Magento writes and mapping changes.
ADMIN_ROLES = (SYSTEM_MANAGER,)

# Connector Settings field holding the optional role required on inbound push
# endpoints (receive_order, receive_order_status, receive_image_update).
PUSH_ENDPOINT_ROLE_FIELD = "push_endpoint_role"
PUSH_ROLE_DIAG_STATUS = "push_role_gate_status"
PUSH_ROLE_DIAG_DETAIL = "push_role_gate_detail"


def has_any_role(*roles, user=None) -> bool:
    """True when `user` (default: session user) is Administrator or holds any of `roles`."""
    user = user or frappe.session.user
    if user == ADMINISTRATOR:
        return True
    wanted = {r for r in roles if r}
    if not wanted:
        return False
    return bool(wanted.intersection(frappe.get_roles(user)))


def require_roles(*roles, message=None):
    """Throw frappe.PermissionError unless the session user has one of `roles`."""
    if has_any_role(*roles):
        return
    frappe.throw(
        message or f"Not permitted. Requires one of the roles: {', '.join(roles)}.",
        frappe.PermissionError,
    )


def require_admin(message=None):
    """System Manager (or Administrator) only."""
    require_roles(*ADMIN_ROLES, message=message)


def require_doc_permission(doctype, ptype="read", doc=None):
    """Throw frappe.PermissionError unless the session user has `ptype` on doctype/doc."""
    frappe.has_permission(doctype, ptype, doc=doc, throw=True)


def require_item_write(item_code):
    """
    Guard for Item-form buttons (Push to Magento / Push to ERPNext Sites):
    the user must be able to write Items in general and this Item in
    particular — the same users whose Item saves already queue these pushes.
    """
    require_doc_permission("Item", "write")
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(f"Item '{item_code}' not found.", frappe.DoesNotExistError)
    require_doc_permission("Item", "write", doc=item_code)


def _get_push_endpoint_role():
    try:
        return (frappe.db.get_single_value("Connector Settings", PUSH_ENDPOINT_ROLE_FIELD) or "").strip()
    except Exception:
        # Field not migrated yet: behave as "not configured".
        return ""


def _record_push_role_diag(status: str, detail: str):
    try:
        from connector.security.request_signing import _diag_set

        _diag_set(PUSH_ROLE_DIAG_STATUS, status)
        _diag_set(PUSH_ROLE_DIAG_DETAIL, detail[:500])
    except Exception:
        pass


def _get_user_type(user):
    """User.user_type ("System User" / "Website User"), or "" when unknown."""
    try:
        return (frappe.db.get_value("User", user, "user_type") or "").strip()
    except Exception:
        return ""


def require_push_endpoint_access(endpoint_name: str):
    """
    Role gate for inbound Magento push endpoints.

    - Administrator always passes.
    - Connector Settings → push_endpoint_role set: the calling user must hold
      that role (System Manager / Administrator always pass).
    - Not set: only System Users pass (with a warning recorded in the signature
      diagnostics so the site owner sees it). The Magento extension calls these
      endpoints with a System User's API key, so it keeps working; Guest and
      portal/website users (e.g. storefront customers who can log in to the
      ERPNext site) are denied — `@frappe.whitelist()` alone would let them in.
    """
    logger = frappe.logger("connector")
    role = _get_push_endpoint_role()
    user = frappe.session.user

    if user == ADMINISTRATOR:
        _record_push_role_diag("ok", f"{endpoint_name}: '{user}' allowed (Administrator).")
        return

    if not role:
        user_type = _get_user_type(user) if user and user != "Guest" else ""
        if user_type != "System User":
            detail = (
                f"{endpoint_name}: denied '{user}' — no 'Role allowed to call push endpoints' "
                f"is set in Connector Settings, so only System Users may call push endpoints "
                f"(user type: '{user_type or 'Guest'}')."
            )
            logger.warning(f"push_role_gate: {detail}")
            _record_push_role_diag("denied", detail)
            frappe.throw("Not permitted to call this push endpoint.", frappe.PermissionError)

        detail = (
            f"{endpoint_name}: called by '{user}' with no 'Role allowed to call push endpoints' "
            "set in Connector Settings — any System User can call push endpoints."
        )
        logger.warning(f"push_role_gate: {detail}")
        _record_push_role_diag("not_configured", detail)
        return

    if has_any_role(role, *ADMIN_ROLES):
        _record_push_role_diag("ok", f"{endpoint_name}: '{user}' allowed (role '{role}').")
        return

    detail = f"{endpoint_name}: denied '{user}' — missing role '{role}'."
    logger.warning(f"push_role_gate: {detail}")
    _record_push_role_diag("denied", detail)
    frappe.throw("Not permitted to call this push endpoint.", frappe.PermissionError)


def get_push_role_diagnostics() -> dict:
    """Last push-endpoint role gate outcome (for the diagnostics dialog)."""
    try:
        cache = frappe.cache()
        from connector.security.request_signing import _diag_key

        return {
            "configured_role": _get_push_endpoint_role() or None,
            "status": cache.get_value(_diag_key(PUSH_ROLE_DIAG_STATUS)) or "unknown",
            "detail": cache.get_value(_diag_key(PUSH_ROLE_DIAG_DETAIL)) or "No push endpoint calls recorded yet.",
        }
    except Exception:
        return {"configured_role": None, "status": "unknown", "detail": ""}
