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
            job_id="connector_manual_full_product_sync",
            deduplicate=True,
        )
        frappe.msgprint("Full product sync has been queued.", indicator="blue")

    @frappe.whitelist()
    def trigger_order_sync(self):
        """Manually enqueue an order sync."""
        frappe.enqueue(
            "connector.tasks.sync_orders",
            queue="default",
            timeout=600,
            job_id="connector_manual_order_sync",
            deduplicate=True,
        )
        frappe.msgprint("Order sync has been queued.", indicator="blue")

    @frappe.whitelist()
    def trigger_image_sync(self):
        """Run image sync directly (synchronous) so the user can see results immediately."""
        from connector.sync.image_sync import sync_images
        result = sync_images()
        if result and result.get("status") == "skipped":
            frappe.msgprint(
                f"Image sync skipped: {result.get('reason', 'unknown')}",
                indicator="orange",
            )
        elif result:
            frappe.msgprint(
                f"Image sync complete: {result.get('updated', 0)} images updated, "
                f"{result.get('no_media_in_magento', 0)} products have no image in Magento, "
                f"{result.get('failed', 0)} errors.",
                indicator="green" if not result.get("failed") else "orange",
            )
        else:
            frappe.msgprint("Image sync finished.", indicator="blue")

    @frappe.whitelist()
    def trigger_order_sync_now(self):
        """Run order sync directly (synchronous) so the user can see results immediately."""
        from connector.sync.order_sync import sync_orders
        result = sync_orders()
        if result and result.get("status") == "skipped":
            frappe.msgprint(
                f"Order sync skipped: {result.get('reason', 'unknown')}",
                indicator="orange",
            )
        elif result:
            fetched = result.get("total_fetched") or result.get("orders_fetched", 0)
            frappe.msgprint(
                f"Order sync complete: {fetched} orders from Magento, "
                f"{result.get('imported', 0)} imported, "
                f"{result.get('updated', 0)} updated, "
                f"{result.get('failed', 0)} errors.",
                indicator="green" if not result.get("failed") else "orange",
            )
        else:
            frappe.msgprint("Order sync finished.", indicator="blue")
