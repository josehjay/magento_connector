import frappe
from frappe.model.document import Document


class MagentoSyncLog(Document):
    pass


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
    """Helper to create a sync log entry without raising exceptions."""
    try:
        import json

        log = frappe.new_doc("Magento Sync Log")
        log.operation = operation
        log.status = status
        log.doctype_name = doctype_name or ""
        log.document_name = str(document_name) if document_name else ""
        log.magento_id = str(magento_id) if magento_id else ""
        log.error_message = error_message or ""
        log.request_payload = (
            json.dumps(request_payload, indent=2, default=str)
            if isinstance(request_payload, (dict, list))
            else (request_payload or "")
        )
        log.response_payload = (
            json.dumps(response_payload, indent=2, default=str)
            if isinstance(response_payload, (dict, list))
            else (response_payload or "")
        )
        log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        # Never let logging break the main flow
        pass
