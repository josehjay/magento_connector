import frappe
from frappe.model.document import Document


class RemoteERPNextSite(Document):
    def validate(self):
        if self.site_url:
            self.site_url = self.site_url.rstrip("/")

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
