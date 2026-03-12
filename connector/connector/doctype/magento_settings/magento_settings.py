import frappe
from frappe.model.document import Document


class MagentoSettings(Document):
    def validate(self):
        if self.magento_url:
            self.magento_url = self.magento_url.rstrip("/")

    @frappe.whitelist()
    def diagnose_sync(self):
        """
        Run a full diagnostic on image sync and order sync prerequisites.
        Reports exactly what is found at each step so the user can see where
        the chain breaks.
        """
        from connector.api.magento_client import MagentoClient, MagentoAPIError
        import json

        lines = []

        def add(msg):
            lines.append(msg)

        # ------------------------------------------------------------------
        # 1. Basic settings
        # ------------------------------------------------------------------
        add("=== BASIC SETTINGS ===")
        try:
            enabled = frappe.db.get_single_value("Connector Settings", "enable_magento_integration")
            add(f"Connector Settings → enable_magento_integration: {enabled!r} (bool={bool(enabled)})")
        except Exception as e:
            add(f"Connector Settings lookup failed (OK if doctype doesn't exist): {e}")

        sync_enabled = frappe.db.get_single_value("Magento Settings", "sync_enabled")
        add(f"Magento Settings → sync_enabled: {sync_enabled!r} (bool={bool(sync_enabled)})")

        magento_url = self.magento_url
        add(f"Magento URL: {magento_url or '(NOT SET)'}")
        add(f"Store code: {self.magento_store_code or 'default'}")

        # ------------------------------------------------------------------
        # 2. Magento API connection
        # ------------------------------------------------------------------
        add("\n=== MAGENTO API CONNECTION ===")
        client = None
        try:
            client = MagentoClient()
            add("MagentoClient initialized OK.")
            add(f"API base: {client.api_base}")
        except Exception as e:
            add(f"FAILED to create MagentoClient: {e}")

        # ------------------------------------------------------------------
        # 3. Magento Product Map
        # ------------------------------------------------------------------
        add("\n=== MAGENTO PRODUCT MAP ===")
        total_map = frappe.db.count("Magento Product Map")
        add(f"Total entries: {total_map}")

        if total_map > 0:
            for status in ["Synced", "Failed", "Pending"]:
                cnt = frappe.db.count("Magento Product Map", {"sync_status": status})
                add(f"  sync_status='{status}': {cnt}")

            sample = frappe.get_all(
                "Magento Product Map",
                filters={"sync_status": "Synced"},
                fields=["item_code", "magento_sku", "magento_product_id", "last_synced_on"],
                order_by="last_synced_on desc",
                limit=3,
            )
            if sample:
                add(f"Sample synced entries (newest first):")
                for s in sample:
                    add(f"  item={s.item_code}, sku={s.magento_sku}, magento_id={s.magento_product_id}, last_synced={s.last_synced_on}")
            else:
                add("  ⚠ NO entries with sync_status='Synced'. Image sync has nothing to process!")
        else:
            add("  ⚠ Product map is EMPTY. Have you pushed any products to Magento yet?")

        # ------------------------------------------------------------------
        # 4. Image field on Item doctype
        # ------------------------------------------------------------------
        add("\n=== IMAGE FIELD ===")
        meta = frappe.get_meta("Item")
        has_image = meta.has_field("image")
        has_item_image = meta.has_field("item_image")
        image_field = "item_image" if has_item_image else ("image" if has_image else None)
        add(f"Item.image field exists: {has_image}")
        add(f"Item.item_image field exists: {has_item_image}")
        add(f"Will use field: {image_field or 'NONE — image sync will skip!'}")

        # ------------------------------------------------------------------
        # 5. Test media fetch for first synced product
        # ------------------------------------------------------------------
        add("\n=== IMAGE SYNC TEST (first synced product) ===")
        if client and total_map > 0:
            first_synced = frappe.get_all(
                "Magento Product Map",
                filters={"sync_status": "Synced"},
                fields=["item_code", "magento_sku"],
                limit=1,
            )
            if first_synced:
                test_sku = first_synced[0]["magento_sku"] or first_synced[0]["item_code"]
                add(f"Testing media fetch for SKU: {test_sku}")
                try:
                    import requests
                    media = client.get_product_media(test_sku)
                    add(f"Magento returned {len(media)} media entries.")
                    if media:
                        for i, entry in enumerate(media[:3]):
                            add(f"  [{i}] types={entry.get('types')}, file={entry.get('file')}, "
                                f"media_type={entry.get('media_type')}, disabled={entry.get('disabled')}")
                        if magento_url:
                            from connector.sync.image_sync import _extract_base_image_url
                            url = _extract_base_image_url(media, magento_url.rstrip("/"))
                            add(f"Extracted base image URL: {url or '(none)'}")
                    else:
                        add("  ⚠ Magento returned EMPTY media list for this product.")
                        add("  Check: Magento Admin → Catalog → Products → (this product) → Images and Videos")
                except MagentoAPIError as e:
                    add(f"  API error fetching media: {e}")
                except Exception as e:
                    add(f"  Error fetching media: {e}")
            else:
                add("No synced entries to test with.")
        elif not client:
            add("Skipped — MagentoClient failed to initialize.")
        else:
            add("Skipped — Product map is empty.")

        # ------------------------------------------------------------------
        # 6. Order sync diagnostics
        # ------------------------------------------------------------------
        add("\n=== ORDER SYNC ===")
        last_order_sync = self.last_order_sync_time
        add(f"last_order_sync_time: {last_order_sync or '(never — will fetch ALL orders)'}")

        if client:
            add("Fetching first page of orders from Magento...")
            try:
                orders = client.get_orders(updated_after=last_order_sync, page=1, page_size=5)
                add(f"Magento returned {len(orders)} orders (page 1, page_size=5).")
                if orders:
                    for o in orders[:3]:
                        oid = o.get("increment_id")
                        status = o.get("status")
                        items_info = []
                        for oi in (o.get("items") or [])[:5]:
                            sku = oi.get("sku", "?")
                            ptype = oi.get("product_type", "?")
                            exists = frappe.db.exists("Item", sku) if sku else False
                            items_info.append(f"{sku} (type={ptype}, in_erpnext={exists})")
                        add(f"  Order #{oid} status={status}, items: {items_info}")
                else:
                    add("  ⚠ Magento returned 0 orders.")
                    if last_order_sync:
                        add(f"  Try resetting last_order_sync_time to blank/null and syncing again.")
                    else:
                        add("  Check: Does your Magento integration token have 'Sales' API permissions?")

                # Test without date filter to see if any orders exist at all
                if not orders and last_order_sync:
                    add("\nRetrying WITHOUT date filter to check if orders exist...")
                    all_orders = client.get_orders(updated_after=None, page=1, page_size=3)
                    add(f"Without filter: Magento returned {len(all_orders)} orders.")
                    if all_orders:
                        add(f"  → Orders DO exist but last_order_sync_time ({last_order_sync}) is filtering them out.")
                        add(f"  Fix: Clear 'Last Order Sync Time' field and save, then sync again.")
            except MagentoAPIError as e:
                add(f"  API error fetching orders: {e}")
                if e.status_code == 401:
                    add("  ⚠ 401 Unauthorized — check API permissions for Sales resources.")
            except Exception as e:
                add(f"  Error fetching orders: {e}")

        # ------------------------------------------------------------------
        # 7. ERPNext Items with sync_to_magento
        # ------------------------------------------------------------------
        add("\n=== ERPNEXT ITEMS ===")
        total_sync = frappe.db.count("Item", {"sync_to_magento": 1})
        add(f"Items with sync_to_magento=1: {total_sync}")
        total_items = frappe.db.count("Item")
        add(f"Total items: {total_items}")

        report = "\n".join(lines)
        frappe.msgprint(
            f"<pre style='white-space:pre-wrap;font-size:12px;max-height:500px;overflow:auto'>{report}</pre>",
            title="Sync Diagnostic Report",
            wide=True,
        )
        return report

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
    def reset_order_sync_cursor(self):
        """
        Clear last_order_sync_time so the next sync fetches orders from the last 30 days.
        Use this when orders are missing because the cursor skipped past them.
        """
        frappe.db.set_single_value("Magento Settings", "last_order_sync_time", None)
        frappe.db.commit()
        frappe.msgprint(
            "Order sync cursor has been reset. "
            "The next sync will fetch orders from the last 30 days.",
            indicator="green",
        )

    @frappe.whitelist()
    def purge_old_logs(self, days=30):
        """Delete Magento Sync Log entries older than `days` days."""
        days = int(days or 30)
        from frappe.utils import add_days, nowdate
        cutoff = add_days(nowdate(), -days)
        deleted = frappe.db.delete("Magento Sync Log", {"synced_on": ["<", cutoff]})
        frappe.db.commit()
        frappe.msgprint(
            f"Deleted sync logs older than {days} days (cutoff: {cutoff}).",
            indicator="green",
        )

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
