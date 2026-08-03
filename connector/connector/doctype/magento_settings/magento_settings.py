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


class MagentoSettings(Document):
    def validate(self):
        if self.magento_url:
            self.magento_url = _validate_secure_url(self.magento_url, "Magento URL")

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
        from connector.api.magento_client import MagentoClient, MagentoAPIError

        def _looks_like_acl_denial(response_body: str) -> bool:
            body = (response_body or "").lower()
            return (
                "isn't authorized to access" in body
                or "is not authorized to access" in body
                or "consumer isn't authorized" in body
                or "consumer is not authorized" in body
                or "resources" in body and "authorized" in body
            )

        checks = [
            (
                "Catalog",
                "/products",
                {"searchCriteria[pageSize]": 1},
            ),
            (
                "Sales",
                "/orders",
                {
                    "searchCriteria[pageSize]": 1,
                    "searchCriteria[currentPage]": 1,
                },
            ),
        ]

        try:
            client = MagentoClient()
            successful_checks = []
            unauthorized_failures = []
            forbidden_failures = []
            other_failures = []
            auth_mode = (
                "Integration Access Token"
                if client.use_integration_token
                else "Admin Username/Password"
            )

            for label, endpoint, params in checks:
                try:
                    client.get(endpoint, params=params)
                    successful_checks.append(label)
                except MagentoAPIError as api_err:
                    # Magento sometimes reports ACL denials as 401 instead of 403.
                    if api_err.status_code == 401 and _looks_like_acl_denial(api_err.response_body):
                        reason = (api_err.response_body or "").strip().replace("\n", " ")
                        if len(reason) > 180:
                            reason = reason[:180] + "..."
                        failure = f"{label} ({endpoint})"
                        if reason:
                            failure += f" -> {reason}"
                        forbidden_failures.append(failure)
                    elif api_err.status_code == 401:
                        reason = (api_err.response_body or "").strip().replace("\n", " ")
                        if len(reason) > 180:
                            reason = reason[:180] + "..."
                        failure = f"{label} ({endpoint})"
                        if reason:
                            failure += f" -> {reason}"
                        unauthorized_failures.append(failure)
                    elif api_err.status_code == 403:
                        reason = (api_err.response_body or "").strip().replace("\n", " ")
                        if len(reason) > 180:
                            reason = reason[:180] + "..."
                        failure = f"{label} ({endpoint})"
                        if reason:
                            failure += f" -> {reason}"
                        forbidden_failures.append(failure)
                    else:
                        other_failures.append(str(api_err))
                except Exception as check_err:
                    other_failures.append(str(check_err))

            if other_failures:
                raise Exception(other_failures[0])

            # Hard fail only when authentication itself is invalid.
            if unauthorized_failures and not successful_checks and not forbidden_failures:
                details = f" Failed checks: {', '.join(unauthorized_failures)}."
                raise Exception(
                    "Authentication failed (HTTP 401). The credentials/token are invalid, expired,"
                    + " or the connector is using a different auth mode than expected."
                    + f" Active auth mode: {auth_mode}."
                    + details
                    + " Verify credentials in Magento Settings and save again."
                )

            # Treat ACL denials as a warning if auth works but required scopes are missing.
            if forbidden_failures:
                verified = f" Verified resources: {', '.join(successful_checks)}." if successful_checks else ""
                denied = f" Missing resources: {', '.join(forbidden_failures)}."
                frappe.msgprint(
                    "Magento connected, but the integration token has limited API scopes."
                    + f" Auth mode: {auth_mode}."
                    + verified
                    + denied
                    + " Order reconciliation and product catalog operations may be limited until these permissions are granted"
                    + " in Magento Admin > System > Extensions > Integrations.",
                    title="Magento Connected With Limited Permissions",
                    indicator="orange",
                )
                return

            frappe.msgprint(
                "Connection successful! Magento API is reachable."
                + f" Auth mode: {auth_mode}."
                + (f" Verified resources: {', '.join(successful_checks)}." if successful_checks else ""),
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
    def preview_product_map_rebuild(self):
        """
        Show how many items need map rebuild without modifying data or
        calling Magento per item.
        """
        from connector.sync.product_map_rebuild import get_rebuild_preview

        preview = get_rebuild_preview()
        lines = [
            "=== PRODUCT MAP REBUILD PREVIEW ===",
            "",
            "This tool restores ERPNext Magento Product Map rows by reading",
            "existing Magento products (GET only). Magento catalog data is NOT modified.",
            "",
            f"Eligible items (sync_to_magento=1): {preview['eligible_total']}",
            f"Already mapped (synced):            {preview['already_mapped']}",
            f"Checked — not in Magento (Pending): {preview.get('pending_not_in_magento', 0)}",
            f"Need map rebuild:                   {preview['needs_rebuild']}",
            f"Item has magento_product_id but no map row: {preview['item_id_without_map']}",
        ]
        active = preview.get("active_rebuild")
        if active and active.get("status") in ("running", "stale"):
            lines.extend([
                "",
                "=== ACTIVE REBUILD ===",
                f"Status:    {active.get('status')}",
                f"Progress:  {active.get('processed', 0)} / {active.get('total', 0)} ({active.get('percent', 0)}%)",
                f"Mapped:    {active.get('mapped', 0)}",
                f"Not in MG: {active.get('skipped_not_in_magento', 0)}",
                f"Failed:    {active.get('failed', 0)}",
                f"Message:   {active.get('message') or '—'}",
            ])
        if preview.get("allowed_item_groups"):
            lines.append(f"Item group filter active: {', '.join(preview['allowed_item_groups'])}")
        else:
            lines.append("Item group filter: (none — all groups eligible)")
        lines.append("")
        if preview["sample_needs_rebuild"]:
            lines.append("Sample items needing rebuild:")
            for code in preview["sample_needs_rebuild"]:
                lines.append(f"  • {code}")
        else:
            lines.append("No items need rebuild.")
        lines.extend([
            "",
            "Recommended steps:",
            "  1. Test Connection (verify Magento API)",
            "  2. Run test rebuild on 5 items and verify results",
            "  3. Rebuild all maps (background job)",
        ])
        return _show_report(lines, "Product Map Rebuild Preview")

    @frappe.whitelist()
    def test_rebuild_product_maps(self, sample_size=5, dry_run=0):
        """
        Pilot map rebuild on a small sample. On success, unlocks full rebuild
        for the current user for 24 hours.
        """
        from connector.sync.product_map_rebuild import test_rebuild_product_maps

        result = test_rebuild_product_maps(
            sample_size=int(sample_size or 5),
            dry_run=bool(int(dry_run or 0)),
        )
        indicator = "green" if result.get("test_passed") else "orange"
        title = "Test Passed" if result.get("test_passed") else "Test Completed With Issues"
        if result.get("dry_run"):
            title = "Dry Run Complete"
        frappe.msgprint(
            f"<pre style='white-space:pre-wrap;font-size:12px;max-height:500px;overflow:auto'>"
            f"{frappe.utils.escape_html(result.get('report') or '')}</pre>",
            title=title,
            indicator=indicator,
            wide=True,
        )
        if result.get("test_passed") and not result.get("dry_run"):
            frappe.msgprint(
                "Test passed with zero failures. You may now run <b>Rebuild All Product Maps</b> "
                "within the next 24 hours.",
                indicator="green",
                title="Full Rebuild Unlocked",
            )
        elif not result.get("test_passed"):
            frappe.msgprint(
                "Test did not pass. Ensure at least one item maps successfully, fix API failures, "
                "or verify SKUs match between ERPNext item_code and Magento.",
                indicator="red",
                title="Test Did Not Pass",
            )
        return result

    @frappe.whitelist()
    def get_product_map_rebuild_status(self):
        """Return whether the current user has passed the test rebuild gate."""
        from connector.sync.product_map_rebuild import get_test_passed_status

        return get_test_passed_status()

    @frappe.whitelist()
    def get_product_map_rebuild_progress(self):
        """Return live progress for the active or last rebuild run."""
        from connector.sync.product_map_rebuild import get_rebuild_progress

        progress = get_rebuild_progress()
        return progress

    @frappe.whitelist()
    def resume_product_map_rebuild(self):
        """Resume a stalled or failed rebuild from the last saved offset."""
        from connector.sync.product_map_rebuild import trigger_full_product_map_rebuild

        result = trigger_full_product_map_rebuild(resume=True)
        if result.get("queued"):
            frappe.msgprint(
                result.get("message") or "Rebuild resumed.",
                indicator="blue",
                title="Rebuild Resumed",
            )
        return result

    @frappe.whitelist()
    def trigger_full_product_map_rebuild(self, confirm_phrase=""):
        """
        Rebuild all missing product maps in the background.
        Requires confirm_phrase='REBUILD MAPS' and a prior successful test run.
        """
        from connector.sync.product_map_rebuild import trigger_full_product_map_rebuild

        result = trigger_full_product_map_rebuild(confirm_phrase=confirm_phrase or "")
        if result.get("queued"):
            prog = result.get("progress") or {}
            msg = result.get("message") or "Full product map rebuild queued."
            if prog.get("total"):
                msg += f" Track progress: {prog.get('processed', 0)}/{prog.get('total')} processed."
            frappe.msgprint(msg, indicator="blue", title="Rebuild Queued")
        return result

    @frappe.whitelist()
    def get_attribute_map_overview(self):
        """Return every ERPNext Item Attribute with its current Magento mapping state."""
        from connector.sync.attribute_sync import get_attribute_map_overview

        return get_attribute_map_overview()

    @frappe.whitelist()
    def get_magento_attributes_for_mapping(self):
        """List existing Magento product attributes for the 'map to existing' dropdown."""
        from connector.sync.attribute_sync import get_magento_attributes_for_mapping

        return get_magento_attributes_for_mapping()

    @frappe.whitelist()
    def map_item_attribute_to_existing(self, item_attribute, magento_attribute_code):
        """Map an ERPNext Item Attribute onto a Magento attribute that already exists."""
        from connector.sync.attribute_sync import map_attribute_to_existing

        result = map_attribute_to_existing(item_attribute, magento_attribute_code)
        frappe.msgprint(
            f"Mapped '{item_attribute}' to Magento attribute '{result.get('magento_attribute_code')}'. "
            f"{len(result.get('added') or [])} new option(s) added, "
            f"{result.get('skipped_existing', 0)} already present.",
            indicator="green" if not result.get("failed") else "orange",
        )
        return result

    @frappe.whitelist()
    def create_item_attribute_in_magento(self, item_attribute):
        """Create a brand-new Magento product attribute for this ERPNext Item Attribute."""
        from connector.sync.attribute_sync import create_attribute_in_magento

        result = create_attribute_in_magento(item_attribute)
        frappe.msgprint(
            result.get("message")
            or f"Created Magento attribute '{result.get('magento_attribute_code')}'.",
            indicator="green",
        )
        return result

    @frappe.whitelist()
    def sync_item_attribute_options_now(self, item_attribute):
        """Manually push newly-added ERPNext values for one mapped attribute."""
        from connector.sync.attribute_sync import sync_attribute_options_now

        result = sync_attribute_options_now(item_attribute)
        if result.get("skipped"):
            frappe.msgprint(f"Nothing to sync ({result.get('reason')}).", indicator="orange")
        else:
            frappe.msgprint(
                f"{len(result.get('added') or [])} new option(s) added, "
                f"{result.get('skipped_existing', 0)} already present, "
                f"{result.get('failed', 0)} failed.",
                indicator="green" if not result.get("failed") else "orange",
            )
        return result

    @frappe.whitelist()
    def sync_all_attribute_options_now(self):
        """Manually enqueue a catch-up options sync for every mapped attribute."""
        frappe.enqueue(
            "connector.tasks.sync_attribute_options",
            queue="default",
            timeout=600,
            job_id="connector_manual_attribute_option_sync",
            deduplicate=True,
        )
        frappe.msgprint("Attribute option sync has been queued.", indicator="blue")

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
            "The next sync will fetch orders from the last 90 days.",
            indicator="green",
        )

    @frappe.whitelist()
    def purge_old_logs(self, days=None, success_days=None):
        """
        Run Magento Sync Log cleanup now.

        Uses Magento Settings retention when days are not passed.
        Explicit `days` overrides absolute retention; `success_days` overrides
        success/skipped retention.
        """
        from connector.connector.doctype.magento_sync_log.magento_sync_log import (
            cleanup_sync_logs,
            get_sync_log_stats,
        )

        kwargs = {}
        if days is not None and str(days).strip() != "":
            kwargs["all_retention_days"] = int(days)
        if success_days is not None and str(success_days).strip() != "":
            kwargs["success_retention_days"] = int(success_days)

        summary = cleanup_sync_logs(**kwargs)
        stats = get_sync_log_stats()
        frappe.msgprint(
            (
                f"Sync log cleanup complete.\n"
                f"• Scrubbed payloads from {summary.get('payloads_scrubbed', 0)} Success row(s)\n"
                f"• Deleted {summary.get('deleted_success_or_skipped', 0)} Success/Skipped "
                f"(older than {summary.get('success_retention_days')} day(s))\n"
                f"• Deleted {summary.get('deleted_past_absolute_retention', 0)} log(s) "
                f"(older than {summary.get('all_retention_days')} day(s))\n"
                f"• Remaining rows: {stats.get('total') or 0} "
                f"(Success {stats.get('success_count') or 0}, "
                f"Failed {stats.get('failed_count') or 0})"
            ),
            title="Sync Log Cleanup",
            indicator="green",
            as_list=False,
        )
        return summary

    @frappe.whitelist()
    def test_order_import(self):
        """
        Fetch the first available Magento order (no date filter) and trace every step
        of the import chain WITHOUT creating any ERPNext records.
        Displays exactly where the import would fail so the user can fix it.
        """
        from connector.api.magento_client import MagentoClient, MagentoAPIError
        from connector.sync.order_sync import _get_default_company, _get_valid_currency
        from connector.connector.doctype.magento_order_map.magento_order_map import is_order_imported

        lines = []

        def add(msg=""):
            lines.append(msg)

        def show():
            report = "\n".join(lines)
            frappe.msgprint(
                f"<pre style='white-space:pre-wrap;font-size:12px;max-height:600px;overflow:auto'>{report}</pre>",
                title="Order Import Diagnostic",
                wide=True,
            )
            return report

        add("=== ORDER IMPORT DIAGNOSTIC ===")
        add("Fetches the first Magento order (bypassing date filter) and")
        add("traces every import step. Nothing is created or modified.")
        add()

        # ── 1. API connection ──────────────────────────────────────────────
        add("--- Step 1: Magento API ---")
        try:
            client = MagentoClient()
            add(f"  OK — connected to {client.api_base}")
        except Exception as e:
            add(f"  FAILED: {e}")
            return show()
        add()

        # ── 2. Fetch first order ───────────────────────────────────────────
        add("--- Step 2: Fetch First Order (no date filter) ---")
        try:
            orders = client.get_orders(updated_after=None, page=1, page_size=1)
        except Exception as e:
            add(f"  FAILED to fetch orders: {e}")
            return show()

        if not orders:
            add("  Magento returned 0 orders even without a date filter.")
            add("  Check: Does your integration token have Sales API permissions?")
            return show()

        order = orders[0]
        increment_id = order.get("increment_id", "?")
        entity_id = order.get("entity_id")
        status = order.get("status", "")
        currency = order.get("order_currency_code", "?")
        add(f"  Order #{increment_id}  entity_id={entity_id}  status={status}  currency={currency}")
        add(f"  Already imported: {bool(is_order_imported(entity_id))}")
        add()

        # ── 3. Line-item analysis ─────────────────────────────────────────
        add("--- Step 3: Line Items ---")
        magento_items = order.get("items") or []
        any_match = False
        for mi in magento_items:
            sku = (mi.get("sku") or "").strip()
            ptype = mi.get("product_type", "?")
            qty = float(mi.get("qty_ordered") or 0)
            if qty <= 0:
                continue
            if ptype in ("configurable", "bundle"):
                add(f"  SKU: {sku:<30} type={ptype}  ← parent row, will be skipped")
                continue
            exists = bool(frappe.db.exists("Item", sku)) if sku else False
            mark = "✓" if exists else "✗ NOT FOUND"
            add(f"  SKU: {sku:<30} type={ptype}  qty={qty}  in ERPNext: {mark}")
            if exists:
                any_match = True

        if not any_match:
            add()
            add("  ⚠ NO items matched an ERPNext item_code.")
            add("  The order will be SKIPPED with status 'no matching items'.")
            add("  Fix: Make sure item_code in ERPNext equals the Magento SKU exactly.")
            add("  (Check capitalization, leading zeros, variant suffixes.)")
        else:
            add(f"  At least one item matched — line items will be created.")
        add()

        # ── 4. Customer ───────────────────────────────────────────────────
        add("--- Step 4: Customer ---")
        email = (order.get("customer_email") or "").strip().lower()
        magento_cust_id = order.get("customer_id")
        is_guest = bool(order.get("customer_is_guest"))
        billing = order.get("billing_address") or {}
        name_parts = " ".join(filter(None, [
            (billing.get("firstname") or order.get("customer_firstname") or "").strip(),
            (billing.get("lastname") or order.get("customer_lastname") or "").strip(),
        ])) or email or "Unknown Customer"

        existing_cust = None
        if not is_guest and magento_cust_id:
            existing_cust = frappe.db.get_value("Customer", {"magento_customer_id": magento_cust_id}, "name")
        if not existing_cust and email:
            existing_cust = frappe.db.get_value("Customer", {"email_id": email}, "name")

        add(f"  Name derived: '{name_parts}'  email: {email or '(none)'}  guest: {is_guest}")
        if existing_cust:
            add(f"  Existing ERPNext customer: {existing_cust}  ← will be reused")
        else:
            cg = frappe.db.get_single_value("Selling Settings", "customer_group") or "(none)"
            tr = frappe.db.get_single_value("Selling Settings", "territory") or "(none)"
            add(f"  No existing customer — will create new (group={cg}, territory={tr})")
        add()

        # ── 5. Company / Price List / Currency ────────────────────────────
        add("--- Step 5: Company / Price List / Currency ---")
        company = _get_default_company()
        add(f"  Default company  : {company or '⚠ NOT SET — SO insert will fail!'}")

        selling_pl = frappe.db.get_single_value("Selling Settings", "selling_price_list")
        add(f"  Selling price list: {selling_pl or '⚠ NOT SET — may fail validation'}")

        resolved_currency = _get_valid_currency(currency)
        add(f"  Order currency   : {currency}  →  resolved to '{resolved_currency}'")
        add()

        # ── 6. Taxes / Shipping accounts ─────────────────────────────────
        add("--- Step 6: Tax & Shipping Accounts ---")
        tax_amount = float(order.get("tax_amount") or 0)
        ship_amount = float(order.get("shipping_amount") or 0)
        if tax_amount > 0:
            tax_acct = frappe.db.get_value(
                "Account", {"account_type": "Tax", "company": company, "disabled": 0}, "name"
            )
            add(f"  Tax amount {tax_amount}  → account: {tax_acct or '⚠ NOT FOUND — tax row will be skipped'}")
        else:
            add(f"  No tax on this order.")
        if ship_amount > 0:
            freight_acct = frappe.db.get_value(
                "Account",
                {"account_name": ["like", "%freight%"], "company": company, "disabled": 0},
                "name",
            ) or frappe.db.get_value(
                "Account",
                {"account_name": ["like", "%shipping%"], "company": company, "disabled": 0},
                "name",
            )
            add(f"  Shipping amount {ship_amount}  → account: {freight_acct or '(not found — row skipped, OK)'}")
        else:
            add(f"  No shipping on this order.")
        add()

        # ── 7. Summary ────────────────────────────────────────────────────
        add("--- Summary ---")
        issues = []
        if not company:
            issues.append("Default company not configured (Global Defaults).")
        if not selling_pl:
            issues.append("No selling price list in Selling Settings.")
        if not any_match:
            issues.append("No Magento SKUs match ERPNext item_codes.")
        if issues:
            add("  Issues that must be fixed before orders can import:")
            for i in issues:
                add(f"    • {i}")
        else:
            add("  No blocking issues detected.")
            add("  If orders still fail, check ERPNext > Error Log for details after running")
            add("  Actions → Sync Orders Now.")

        return show()

    @frappe.whitelist()
    def view_recent_push_log(self):
        """
        Show the last 30 order operations received from the Magento extension
        and any recent failures, so the user can see what has been pushed.
        """
        logs = frappe.get_all(
            "Magento Sync Log",
            filters={"operation": "Order Pull"},
            fields=["name", "status", "synced_on", "magento_id", "document_name", "error_message", "response_payload"],
            order_by="synced_on desc",
            limit=30,
        )

        lines = ["=== RECENT ORDER PUSH LOG (last 30 entries) ===\n"]

        if not logs:
            lines.append("No order log entries found.")
        else:
            for log in logs:
                ts      = str(log.synced_on or "")[:19]
                status  = log.status or "?"
                mago_id = log.magento_id or "—"
                so_name = log.document_name or ""

                # Try to extract SO name from response payload if not in document_name
                if not so_name and log.response_payload:
                    try:
                        import json
                        payload = json.loads(log.response_payload) if isinstance(log.response_payload, str) else log.response_payload
                        so_name = payload.get("sales_order") or payload.get("imported") or ""
                    except Exception:
                        pass

                icon = "✓" if status == "Success" else ("✗" if status == "Failed" else "·")
                line = f"  {icon} [{ts}]  Magento #{mago_id:<14}  Status: {status}"
                if so_name:
                    line += f"  →  SO: {so_name}"
                if status != "Success" and log.error_message:
                    err = str(log.error_message)[:120]
                    line += f"\n       Error: {err}"
                lines.append(line)

        # Also surface any ERPNext error log entries tagged to order sync
        recent_errors = frappe.get_all(
            "Error Log",
            filters={"method": ["like", "%order%"], "creation": [">", frappe.utils.add_days(frappe.utils.nowdate(), -3)]},
            fields=["name", "method", "creation"],
            order_by="creation desc",
            limit=5,
        )
        if recent_errors:
            lines.append("\n=== RECENT ORDER ERROR LOG ENTRIES (last 3 days) ===")
            for err in recent_errors:
                lines.append(f"  [{str(err.creation)[:19]}]  {err.method}  →  {err.name}")

        report = "\n".join(lines)
        frappe.msgprint(
            f"<pre style='white-space:pre-wrap;font-size:12px;max-height:500px;overflow:auto'>{frappe.utils.escape_html(report)}</pre>",
            title="Order Push Log",
            wide=True,
        )
        return report

    @frappe.whitelist()
    def test_status_sync(self, sales_order):
        """
        Synchronously push a 'processing' status + comment to Magento for the
        given Sales Order.  Runs in the foreground so the user sees the result
        (or exact error) immediately — useful for diagnosing why background
        status-sync jobs are not updating Magento.
        """
        from connector.api.magento_client import MagentoClient, MagentoAPIError

        if not sales_order:
            frappe.throw("Please provide a Sales Order name.", title="Missing Input")

        # ── 1. Load the SO and verify it came from Magento ────────────────────
        try:
            so = frappe.get_doc("Sales Order", sales_order)
        except frappe.DoesNotExistError:
            frappe.throw(f"Sales Order '{sales_order}' not found.", title="Not Found")

        magento_order_id = so.get("magento_order_id")
        magento_increment = so.get("magento_increment_id") or ""

        lines = [
            f"=== STATUS SYNC DIAGNOSTIC FOR {sales_order} ===",
            f"",
            f"SO docstatus      : {so.docstatus} ({'Draft' if so.docstatus==0 else 'Submitted' if so.docstatus==1 else 'Cancelled'})",
            f"magento_order_id  : {magento_order_id or '(not set — not a Magento order)'}",
            f"magento_increment : {magento_increment or '(not set)'}",
            f"magento_status    : {so.get('magento_order_status') or '(not set)'}",
            f"",
        ]

        if not magento_order_id:
            lines.append("⚠ This Sales Order is NOT linked to a Magento order.")
            lines.append("  The status sync hooks only act on orders imported from Magento.")
            lines.append("  If this order WAS from Magento, check that the custom field")
            lines.append("  'magento_order_id' (Int) is present and populated on the SO.")
            _show_report(lines, "Status Sync Diagnostic")
            return

        # ── 2. Test Magento API connection ────────────────────────────────────
        lines.append("--- Step 1: Magento API Connection ---")
        try:
            client = MagentoClient()
            lines.append(f"  OK — connected to {client.api_base}")
        except Exception as exc:
            lines.append(f"  FAILED: {exc}")
            _show_report(lines, "Status Sync Diagnostic")
            return
        lines.append("")

        # ── 3. Fetch current Magento order status ─────────────────────────────
        lines.append("--- Step 2: Current Magento Order Status ---")
        try:
            order = client.get_order(int(magento_order_id))
            current_status = order.get("status", "?")
            current_state  = order.get("state", "?")
            lines.append(f"  Magento order #{magento_increment or magento_order_id}")
            lines.append(f"  Current status: {current_status}")
            lines.append(f"  Current state : {current_state}")
        except MagentoAPIError as exc:
            lines.append(f"  API error fetching order: {exc} (HTTP {exc.status_code})")
            if exc.status_code == 404:
                lines.append("  ⚠ Order not found in Magento — the ID may be stale.")
            elif exc.status_code in (401, 403):
                lines.append("  ⚠ Permission denied — check integration token ACL (Sales resources).")
            _show_report(lines, "Status Sync Diagnostic")
            return
        lines.append("")

        # ── 4. Attempt invoice first (required by many Magento workflows) ────
        lines.append("--- Step 3: Invoice Attempt (POST /V1/order/{id}/invoice) ---")
        invoice_created = False
        try:
            invoice_result = client.ensure_invoice_for_processing(int(magento_order_id), notify=False)
            if invoice_result is not None:
                lines.append(f"  ✓ Invoice created. Response: {invoice_result!r}")
                invoice_created = True
            else:
                lines.append("  ℹ No invoice created (already invoiced/processing or no invoiceable qty).")
        except Exception as exc:
            lines.append(f"  ✗ Invoice attempt FAILED: {exc}")
        lines.append("")

        # ── 5. Step 1 of the real sync: entity patch ──────────────────────────
        lines.append("--- Step 4a: Entity Patch (POST /V1/orders) ---")
        lines.append("  (Forces state+status transition — bypasses status-history validation)")
        entity_patched = False
        try:
            result_patch = client.update_order_entity_status(int(magento_order_id), "processing")
            lines.append(f"  ✓ Entity patch succeeded. Response: {result_patch!r}")
            entity_patched = True
        except MagentoAPIError as exc:
            lines.append(f"  ✗ Entity patch FAILED: {exc}")
            lines.append(f"    HTTP {exc.status_code}  Body: {exc.response_body}")
            if exc.status_code in (401, 403):
                lines.append("  ⚠ ACL required: Magento_Sales::actions_edit")
                lines.append("    Go to: Magento Admin → System → Integrations → (your token) → API")
                lines.append("    Enable: Sales → Operations → Orders → Actions")
            lines.append("  The comment step below will still run (informational, no status label).")
        lines.append("")

        # ── 6. Step 2 of the real sync: comment ───────────────────────────────
        lines.append("--- Step 4b: Status Comment (POST /V1/orders/{id}/comments) ---")
        test_comment = f"[ERPNext Test] Status sync diagnostic from {sales_order}."
        comment_body: dict = {
            "comment": test_comment,
            "is_customer_notified": 0,
            "is_visible_on_front": 0,
        }
        if entity_patched:
            comment_body["status"] = "processing"
            lines.append("  Posting comment WITH status='processing' (entity was patched).")
        else:
            lines.append("  Posting informational comment WITHOUT status change (entity patch failed).")
        try:
            result_comment = client.post(
                f"/orders/{int(magento_order_id)}/comments",
                data={"statusHistory": comment_body},
            )
            lines.append(f"  ✓ Comment posted. Response: {result_comment!r}")
        except MagentoAPIError as exc:
            lines.append(f"  ✗ Comment FAILED: {exc}")
            lines.append(f"    HTTP {exc.status_code}  Body: {exc.response_body}")
            _show_report(lines, "Status Sync Diagnostic")
            return
        lines.append("")

        # ── 7. Verify the status changed ─────────────────────────────────────
        lines.append("--- Step 5: Verify New Magento Order Status ---")
        try:
            order_after = client.get_order(int(magento_order_id))
            new_status = order_after.get("status", "?")
            new_state  = order_after.get("state", "?")
            lines.append(f"  New status: {new_status}  (was: {current_status})")
            lines.append(f"  New state : {new_state}  (was: {current_state})")
            if new_status == "processing":
                lines.append("  ✓ Magento status is now 'processing'.")
            else:
                lines.append(f"  ⚠ Status is '{new_status}', not 'processing'.")
                if not invoice_created:
                    lines.append("    Invoice was not created in Step 3; this is often required.")
                lines.append("    Magento may require a payment/invoice to transition to 'processing'.")
                lines.append("    Check: Stores → Order Statuses in Magento admin.")
        except Exception as exc:
            lines.append(f"  Could not re-fetch order to verify: {exc}")
        lines.append("")

        # ── 7. Mirror into ERPNext ────────────────────────────────────────────
        frappe.db.set_value(
            "Sales Order", sales_order, "magento_order_status", "processing",
            update_modified=False,
        )
        frappe.db.commit()
        lines.append("--- Step 6: ERPNext Mirror ---")
        lines.append(f"  magento_order_status on {sales_order} set to 'processing'.")

        _show_report(lines, "Status Sync Diagnostic")

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
            fetched   = result.get("total_fetched") or result.get("orders_fetched", 0)
            imported  = result.get("imported", 0)
            updated   = result.get("updated", 0)
            skipped   = result.get("skipped", 0)
            failed    = result.get("failed", 0)
            parts = [
                f"{fetched} orders fetched",
                f"{imported} imported",
                f"{updated} updated",
            ]
            if skipped:
                parts.append(f"{skipped} skipped (cancelled / no matching items)")
            if failed:
                parts.append(f"{failed} errors — see Error Log for details")
            frappe.msgprint(
                "Order sync complete: " + ", ".join(parts) + ".",
                indicator="green" if not failed else "orange",
            )
        else:
            frappe.msgprint("Order sync finished.", indicator="blue")

    @frappe.whitelist()
    def view_signature_verification_status(self):
        """
        Show cached HMAC signature verification diagnostics for inbound
        Magento push requests.
        """
        from connector.security.request_signing import get_signature_diagnostics

        settings = frappe.get_single("Connector Settings")
        diag = get_signature_diagnostics()

        verify_enabled = bool(getattr(settings, "enable_signed_push_verification", 0))
        enforce_enabled = bool(getattr(settings, "enforce_signed_push_verification", 0))
        tolerance = int(getattr(settings, "signature_tolerance_seconds", 300) or 300)

        lines = [
            "=== SIGNATURE VERIFICATION STATUS ===",
            "",
            f"Verify mode enabled : {verify_enabled}",
            f"Strict enforce mode : {enforce_enabled}",
            f"Time window (sec)   : {tolerance}",
            "",
            f"Last result         : {diag.get('last_result')}",
            f"Last seen on        : {diag.get('last_seen_on') or '(never)'}",
            f"Last detail         : {diag.get('last_detail')}",
            "",
            f"Total checks        : {diag.get('total_checks', 0)}",
            f"Successful checks   : {diag.get('success_count', 0)}",
            f"Warnings (allowed)  : {diag.get('warning_count', 0)}",
            f"Denied (blocked)    : {diag.get('denied_count', 0)}",
        ]
        return _show_report(lines, "Signature Verification Status")

    @frappe.whitelist()
    def reset_signature_verification_counters(self):
        """Clear cached signature verification diagnostics counters."""
        from connector.security.request_signing import reset_signature_diagnostics

        reset_signature_diagnostics()
        frappe.msgprint(
            "Signature verification diagnostics counters have been reset.",
            title="Counters Reset",
            indicator="green",
        )


# ── Module-level helpers ───────────────────────────────────────────────────


def _show_report(lines, title="Diagnostic"):
    """Render a list of strings as a scrollable pre-formatted dialog."""
    report = "\n".join(lines)
    frappe.msgprint(
        f"<pre style='white-space:pre-wrap;font-size:12px;"
        f"max-height:550px;overflow:auto;font-family:monospace'>"
        f"{frappe.utils.escape_html(report)}</pre>",
        title=title,
        wide=True,
    )
    return report
