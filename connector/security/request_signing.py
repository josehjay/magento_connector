import hashlib
import hmac
import time

import frappe


HEADER_TIMESTAMP = "X-Connector-Timestamp"
HEADER_NONCE = "X-Connector-Nonce"
HEADER_SIGNATURE = "X-Connector-Signature"
HEADER_VERSION = "X-Connector-Signature-Version"
SIGNATURE_VERSION = "v1"
DIAG_PREFIX = "connector:request-signing:diag:"


def _get_header(name: str):
    value = frappe.get_request_header(name)
    return (value or "").strip()


def _is_enforce_mode_enabled(settings) -> bool:
    return bool(getattr(settings, "enforce_signed_push_verification", 0))


def _deny_or_warn(message: str, enforce: bool) -> bool:
    logger = frappe.logger("connector")
    if enforce:
        logger.warning(f"request_signing: denied - {message}")
        _record_diag("denied", message)
        frappe.throw("Unauthorized signed request.", frappe.PermissionError)
    logger.warning(f"request_signing: {message}")
    _record_diag("warning", message)
    return True


def _mark_and_check_replay(nonce: str, timestamp: int, ttl_seconds: int) -> bool:
    cache = frappe.cache()
    cache_key = f"connector:request-signature:{timestamp}:{nonce}"
    if cache.get_value(cache_key):
        return False
    ttl = max(ttl_seconds, 60)
    try:
        cache.set_value(cache_key, 1, expires_in_sec=ttl)
    except TypeError:
        # Backward compatibility for cache backends using a different TTL kwarg.
        cache.set_value(cache_key, 1, expires=ttl)
    return True


def _diag_key(name: str) -> str:
    return f"{DIAG_PREFIX}{name}"


def _diag_set(name: str, value):
    cache = frappe.cache()
    try:
        cache.set_value(_diag_key(name), value)
    except TypeError:
        cache.set_value(_diag_key(name), value, expires=0)


def _diag_get_int(name: str) -> int:
    value = frappe.cache().get_value(_diag_key(name))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _diag_incr(name: str):
    _diag_set(name, _diag_get_int(name) + 1)


def _record_diag(result: str, detail: str):
    now_str = str(frappe.utils.now_datetime())
    _diag_set("last_result", result)
    _diag_set("last_detail", detail[:500])
    _diag_set("last_seen_on", now_str)
    _diag_incr("total_checks")
    if result == "ok":
        _diag_incr("success_count")
    elif result == "warning":
        _diag_incr("warning_count")
    elif result == "denied":
        _diag_incr("denied_count")


def get_signature_diagnostics() -> dict:
    """Return cached signature verification counters and last outcome."""
    cache = frappe.cache()
    return {
        "last_result": cache.get_value(_diag_key("last_result")) or "unknown",
        "last_detail": cache.get_value(_diag_key("last_detail")) or "No signature verification checks recorded yet.",
        "last_seen_on": cache.get_value(_diag_key("last_seen_on")) or None,
        "total_checks": _diag_get_int("total_checks"),
        "success_count": _diag_get_int("success_count"),
        "warning_count": _diag_get_int("warning_count"),
        "denied_count": _diag_get_int("denied_count"),
    }


def reset_signature_diagnostics() -> None:
    """Clear cached signature verification diagnostics counters/state."""
    keys = [
        "last_result",
        "last_detail",
        "last_seen_on",
        "total_checks",
        "success_count",
        "warning_count",
        "denied_count",
    ]
    cache = frappe.cache()
    for key in keys:
        cache.delete_value(_diag_key(key))


def verify_incoming_signed_request(endpoint_name: str) -> bool:
    """
    Verify incoming Magento push request signature.

    Rollout behavior:
    - verify flag OFF    -> always allow.
    - verify flag ON     -> validate signed requests; in permissive mode allow failures but log.
    - enforce flag ON    -> reject missing/invalid signatures.
    """
    settings = frappe.get_single("Connector Settings")
    verify_enabled = bool(getattr(settings, "enable_signed_push_verification", 0))
    if not verify_enabled:
        _record_diag("warning", f"{endpoint_name}: verification disabled in Connector Settings")
        return True

    enforce = _is_enforce_mode_enabled(settings)
    secret = settings.get_password("signed_push_secret") if getattr(settings, "signed_push_secret", None) else ""
    if not secret:
        return _deny_or_warn(f"{endpoint_name}: missing shared signing secret in Connector Settings", enforce)

    timestamp_raw = _get_header(HEADER_TIMESTAMP)
    nonce = _get_header(HEADER_NONCE)
    signature = _get_header(HEADER_SIGNATURE)
    version = _get_header(HEADER_VERSION) or SIGNATURE_VERSION

    if not timestamp_raw or not nonce or not signature:
        return _deny_or_warn(f"{endpoint_name}: missing signature headers", enforce)

    if version != SIGNATURE_VERSION:
        return _deny_or_warn(f"{endpoint_name}: unsupported signature version '{version}'", enforce)

    try:
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError):
        return _deny_or_warn(f"{endpoint_name}: invalid timestamp header", enforce)

    tolerance = int(getattr(settings, "signature_tolerance_seconds", 300) or 300)
    tolerance = max(tolerance, 60)
    if abs(int(time.time()) - timestamp) > tolerance:
        return _deny_or_warn(f"{endpoint_name}: signature timestamp outside allowed window", enforce)

    if not _mark_and_check_replay(nonce, timestamp, tolerance + 60):
        return _deny_or_warn(f"{endpoint_name}: replayed nonce detected", enforce)

    method = (frappe.request.method or "").upper()
    path = frappe.request.path or ""
    body = frappe.request.get_data(as_text=True) or ""
    canonical = "\n".join([timestamp_raw, nonce, method, path, body])
    expected = hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return _deny_or_warn(f"{endpoint_name}: signature mismatch", enforce)

    _record_diag("ok", f"{endpoint_name}: signature verified")
    return True

