import frappe
from frappe.model.document import Document


class MagentoSettings(Document):
    def validate(self):
        if self.magento_url:
            self.magento_url = self.magento_url.rstrip("/")

    @frappe.whitelist()
    def test_connection(self):
        """Test connectivity to Magento and return a success/failure message."""
        from connector.api.magento_client import MagentoClient
        try:
            client = MagentoClient()
            result = client.get("/store/storeConfigs")
            frappe.msgprint(
                f"Connection successful! Store: {result[0].get('base_url', self.magento_url)}",
                title="Magento Connected",
                indicator="green",
            )
        except Exception as e:
            frappe.throw(f"Connection failed: {e}", title="Magento Connection Error")

    @frappe.whitelist()
    def trigger_full_product_sync(self):
        """Manually enqueue a full product sync."""
        frappe.enqueue(
            "connector.tasks.full_product_sync",
            queue="long",
            timeout=3600,
        )
        frappe.msgprint("Full product sync has been queued.", indicator="blue")

    @frappe.whitelist()
    def trigger_order_sync(self):
        """Manually enqueue an order sync."""
        frappe.enqueue(
            "connector.tasks.sync_orders",
            queue="default",
            timeout=600,
        )
        frappe.msgprint("Order sync has been queued.", indicator="blue")
