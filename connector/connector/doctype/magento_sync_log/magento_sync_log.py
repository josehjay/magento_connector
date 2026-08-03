import json

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime


# Successful Product Push logs often embed full Magento product payloads —
# without limits the log table grows to hundreds of MB quickly.
MAX_PAYLOAD_CHARS = 8000
MAX_ERROR_CHARS = 2000

# Defaults when Magento Settings fields are missing / unset.
DEFAULT_SUCCESS_RETENTION_DAYS = 7
DEFAULT_ALL_RETENTION_DAYS = 30
DEFAULT_STORE_SUCCESS_PAYLOADS = 0

# Batch size for DELETE/UPDATE so cleanup never holds long table locks.
CLEANUP_BATCH_SIZE = 1000
CLEANUP_MAX_ROUNDS = 50


class MagentoSyncLog(Document):
    pass


def _get_log_settings():
    """Read retention / payload preferences from Magento Settings (safe defaults)."""
    success_days = DEFAULT_SUCCESS_RETENTION_DAYS
    all_days = DEFAULT_ALL_RETENTION_DAYS
    store_success_payloads = bool(DEFAULT_STORE_SUCCESS_PAYLOADS)

    try:
        raw_success = frappe.db.get_single_value(
            "Magento Settings", "success_log_retention_days"
        )
        raw_all = frappe.db.get_single_value(
            "Magento Settings", "sync_log_retention_days"
        )
        raw_store = frappe.db.get_single_value(
            "Magento Settings", "store_success_payloads"
        )
        if raw_success is not None and str(raw_success).strip() != "":
            success_days = max(1, int(raw_success))
        if raw_all is not None and str(raw_all).strip() != "":
            all_days = max(1, int(raw_all))
        if raw_store is not None:
            store_success_payloads = bool(int(raw_store))
    except Exception:
        pass

    # Success logs must not outlive the absolute retention floor.
    if success_days > all_days:
        success_days = all_days

    return {
        "success_retention_days": success_days,
        "all_retention_days": all_days,
        "store_success_payloads": store_success_payloads,
    }


def _truncate(value, limit):
    if not value:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[truncated]"


def _serialize_payload(payload, *, allow=True):
    if not allow or payload is None or payload == "":
        return ""
    if isinstance(payload, (dict, list)):
        try:
            text = json.dumps(payload, indent=2, default=str)
        except Exception:
            text = str(payload)
    else:
        text = str(payload)
    return _truncate(text, MAX_PAYLOAD_CHARS)


def create_log(
    operation,
    status,
    doctype_name=None,
    document_name=None,
    magento_id=None,
    error_message=None,
    request_payload=None,
    response_payload=None,
):
    """
    Create a Magento Sync Log entry without raising exceptions.

    Success rows omit request/response payloads by default (configurable in
    Magento Settings) — those fields are the main source of log table bloat.
    Failed rows still keep truncated payloads for debugging.
    """
    try:
        settings = _get_log_settings()
        status_value = (status or "").strip() or "Failed"
        store_payloads = status_value != "Success" or settings["store_success_payloads"]

        log = frappe.new_doc("Magento Sync Log")
        log.operation = operation
        log.status = status_value
        log.doctype_name = doctype_name or ""
        log.document_name = str(document_name) if document_name else ""
        log.magento_id = str(magento_id) if magento_id else ""
        log.error_message = _truncate(error_message or "", MAX_ERROR_CHARS)
        log.request_payload = _serialize_payload(request_payload, allow=store_payloads)
        log.response_payload = _serialize_payload(response_payload, allow=store_payloads)
        log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        # Never let logging break the main flow
        pass


def _delete_logs(filters, label):
    """
    Delete matching Magento Sync Log rows in batches.
    Returns total rows deleted.
    """
    total = 0
    for _ in range(CLEANUP_MAX_ROUNDS):
        names = frappe.get_all(
            "Magento Sync Log",
            filters=filters,
            pluck="name",
            limit_page_length=CLEANUP_BATCH_SIZE,
            order_by="synced_on asc",
        )
        if not names:
            break
        # Hard-delete without per-row document load — log is disposable telemetry.
        frappe.db.delete("Magento Sync Log", {"name": ["in", names]})
        frappe.db.commit()
        total += len(names)
        frappe.logger("connector").info(
            f"cleanup_sync_logs: deleted {len(names)} {label} row(s) "
            f"(batch total so far {total})."
        )
        if len(names) < CLEANUP_BATCH_SIZE:
            break
    return total


def scrub_success_payloads():
    """
    Clear request/response payloads from existing Success logs.
    Reclaims space without deleting the summary row. Runs in batches.
    """
    total = 0
    for _ in range(CLEANUP_MAX_ROUNDS):
        names = frappe.db.sql(
            """
            SELECT name FROM `tabMagento Sync Log`
            WHERE status = 'Success'
              AND (
                IFNULL(request_payload, '') != ''
                OR IFNULL(response_payload, '') != ''
              )
            ORDER BY synced_on ASC
            LIMIT %s
            """,
            (CLEANUP_BATCH_SIZE,),
            as_dict=True,
        )
        if not names:
            break
        name_list = [row["name"] for row in names]
        frappe.db.sql(
            """
            UPDATE `tabMagento Sync Log`
            SET request_payload = '', response_payload = ''
            WHERE name IN %(names)s
            """,
            {"names": name_list},
        )
        frappe.db.commit()
        total += len(name_list)
        if len(name_list) < CLEANUP_BATCH_SIZE:
            break
    return total


def cleanup_sync_logs(success_retention_days=None, all_retention_days=None):
    """
    Enforce Magento Sync Log retention:
      1. Scrub payloads from remaining Success rows (space reclaim).
      2. Delete Success/Skipped older than success retention.
      3. Delete any status older than absolute retention (Failed included).

    Returns a summary dict with counts and cutoffs used.
    """
    settings = _get_log_settings()
    success_days = (
        max(1, int(success_retention_days))
        if success_retention_days is not None
        else settings["success_retention_days"]
    )
    all_days = (
        max(1, int(all_retention_days))
        if all_retention_days is not None
        else settings["all_retention_days"]
    )
    if success_days > all_days:
        success_days = all_days

    now = now_datetime()
    success_cutoff = add_to_date(now, days=-success_days, as_datetime=True)
    all_cutoff = add_to_date(now, days=-all_days, as_datetime=True)

    scrubbed = scrub_success_payloads()
    deleted_success = _delete_logs(
        {
            "status": ["in", ["Success", "Skipped"]],
            "synced_on": ["<", success_cutoff],
        },
        "Success/Skipped",
    )
    deleted_old = _delete_logs(
        {"synced_on": ["<", all_cutoff]},
        "any-status",
    )

    summary = {
        "success_retention_days": success_days,
        "all_retention_days": all_days,
        "success_cutoff": str(success_cutoff),
        "all_cutoff": str(all_cutoff),
        "payloads_scrubbed": scrubbed,
        "deleted_success_or_skipped": deleted_success,
        "deleted_past_absolute_retention": deleted_old,
        "deleted_total": deleted_success + deleted_old,
    }
    frappe.logger("connector").info(f"cleanup_sync_logs: {summary}")
    return summary


def get_sync_log_stats():
    """Return lightweight counts for Magento Settings monitoring."""
    try:
        rows = frappe.db.sql(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'Skipped' THEN 1 ELSE 0 END) AS skipped_count,
                SUM(
                    CASE
                        WHEN IFNULL(request_payload, '') != ''
                          OR IFNULL(response_payload, '') != ''
                        THEN 1 ELSE 0
                    END
                ) AS rows_with_payload
            FROM `tabMagento Sync Log`
            """,
            as_dict=True,
        )
        return rows[0] if rows else {}
    except Exception:
        return {}
