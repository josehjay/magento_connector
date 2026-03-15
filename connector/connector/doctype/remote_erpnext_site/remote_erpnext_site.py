import frappe
from frappe.model.document import Document
from urllib.parse import urlparse


def _validate_secure_url(url: str, label: str) -> str:
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return normalized

    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        frappe.throw(f"{label} must be a valid absolute URL (for example: https://example.com).")

    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not is_local:
        frappe.throw(f"{label} must use HTTPS in production.")

    return normalized


class RemoteERPNextSite(Document):
    def validate(self):
        if self.site_url:
            self.site_url = _validate_secure_url(self.site_url, "Remote ERPNext Site URL")

    @frappe.whitelist()
    def test_connection(self):
        """Test connectivity to the remote ERPNext site."""
        from connector.api.erpnext_client import ERPNextClient, ERPNextAPIError
        try:
            client = ERPNextClient(self.name)
            info = client.get_logged_user()
            frappe.msgprint(
                f"Connection successful! Authenticated as: {info}",
                title="Remote Site Connected",
                indicator="green",
            )
        except ERPNextAPIError as e:
            frappe.throw(f"Connection failed: {e}", title="Remote Site Connection Error")
        except Exception as e:
            frappe.throw(f"Connection failed: {e}", title="Remote Site Connection Error")
