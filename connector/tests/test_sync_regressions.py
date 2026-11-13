"""
Regression tests for sync bugs fixed in the Nov 2026 review:
image-sync starvation, billing overwriting shipping addresses, shipping-tax
double count, SO/map atomicity, remote-site barcodes and failed-row retries,
and comment-only status pushes for cancellations.
"""

import datetime
import unittest
from unittest.mock import MagicMock, call, patch


class TestImageSyncBatchRotation(unittest.TestCase):

    def _mapped(self, n):
        return [{"item_code": f"ITEM-{i:03d}", "magento_sku": f"ITEM-{i:03d}"} for i in range(n)]

    @patch("connector.sync.image_sync.frappe")
    def test_batches_advance_past_the_cursor_and_wrap(self, mock_frappe):
        from connector.sync.image_sync import _next_image_batch

        mapped = self._mapped(5)
        mock_frappe.cache.return_value.get_value.return_value = None
        first = _next_image_batch(mapped, 2)
        mock_frappe.cache.return_value.get_value.return_value = first[-1]["item_code"]
        second = _next_image_batch(mapped, 2)
        mock_frappe.cache.return_value.get_value.return_value = "ITEM-004"
        wrapped = _next_image_batch(mapped, 2)

        self.assertEqual([r["item_code"] for r in first], ["ITEM-000", "ITEM-001"])
        self.assertEqual([r["item_code"] for r in second], ["ITEM-002", "ITEM-003"])
        self.assertEqual([r["item_code"] for r in wrapped], ["ITEM-000", "ITEM-001"])

    @patch("connector.sync.image_sync.frappe")
    def test_small_catalog_processes_everything(self, mock_frappe):
        from connector.sync.image_sync import _next_image_batch

        mapped = self._mapped(3)
        self.assertEqual(_next_image_batch(mapped, 200), mapped)


class TestAddressLookupByType(unittest.TestCase):

    ORDER = {
        "increment_id": "100",
        "billing_address": {"street": ["1 Bill St"], "city": "Nairobi", "country_id": "KE"},
        "extension_attributes": {"shipping_assignments": [{"shipping": {"address": {
            "street": ["2 Ship Rd"], "city": "Mombasa", "country_id": "KE",
        }}}]},
    }

    @patch("connector.sync.customer_sync._get_country_name", return_value="Kenya")
    @patch("connector.sync.customer_sync.frappe")
    def test_existing_address_lookup_filters_on_address_type(self, mock_frappe, _country):
        from connector.sync.customer_sync import get_or_create_address

        mock_frappe.get_all.return_value = ["ADDR-BILL"]
        get_or_create_address(self.ORDER, "Jane", address_kind="billing")

        filters = mock_frappe.get_all.call_args.kwargs["filters"]
        self.assertIn(["address_type", "=", "Billing"], filters)
        updated = mock_frappe.db.set_value.call_args[0][2]
        self.assertEqual(updated["address_line1"], "1 Bill St")
        self.assertNotIn("address_type", updated)


class TestOrderTaxesAndAtomicity(unittest.TestCase):

    @patch("connector.sync.order_sync._get_default_company", return_value="Co")
    @patch("connector.sync.order_sync._get_freight_account", return_value="Freight - Co")
    @patch("connector.sync.order_sync._get_tax_account", return_value="VAT - Co")
    def test_shipping_tax_is_not_double_counted(self, _tax, _freight, _company):
        from connector.sync.order_sync import _build_taxes_and_charges

        rows = _build_taxes_and_charges({
            "tax_amount": 16.0,           # includes 1.60 shipping tax
            "shipping_amount": 10.0,
            "shipping_tax_amount": 1.6,
        })

        self.assertEqual(sum(r["tax_amount"] for r in rows), 26.0)

    @patch("connector.sync.order_sync.frappe")
    @patch("connector.sync.order_sync.is_order_imported", return_value=False)
    @patch("connector.sync.order_sync.create_map")
    @patch("connector.sync.order_sync.create_log")
    @patch("connector.sync.order_sync.get_or_create_customer", return_value="John")
    @patch("connector.sync.order_sync.get_or_create_address", return_value=None)
    @patch("connector.sync.order_sync._build_order_items", return_value=[{"item_code": "A", "qty": 1}])
    def test_no_commit_between_sales_order_insert_and_map(
        self, _items, _addr, _cust, _log, mock_create_map, _imported, mock_frappe
    ):
        from connector.sync.order_sync import _process_order

        mock_frappe.get_single.return_value.get.return_value = ""
        mock_frappe.get_single.return_value.lead_time_days = 3
        so = MagicMock()
        so.name = "SO-1"
        mock_frappe.new_doc.return_value = so

        tracker = MagicMock()
        tracker.attach_mock(so.insert, "insert")
        tracker.attach_mock(mock_frappe.db.commit, "commit")
        tracker.attach_mock(mock_create_map, "create_map")

        _process_order({"entity_id": 5, "increment_id": "5", "status": "pending"}, None)

        names = [c[0] for c in tracker.mock_calls]
        insert_at = names.index("insert")
        self.assertEqual(names[insert_at + 1], "create_map")


class TestRemoteSiteSync(unittest.TestCase):

    def test_payload_includes_barcodes_child_table(self):
        from connector.sync.erpnext_product_sync import _build_item_payload

        doc = MagicMock()
        values = {
            "barcodes": [{"barcode": "9780000000001", "barcode_type": "EAN"}, {"barcode": ""}],
            "weight_per_unit": None, "weight_uom": None, "brand": None,
        }
        doc.get.side_effect = lambda key, default=None: values.get(key, default)
        with patch("connector.sync.erpnext_product_sync._get_item_price", return_value=None):
            payload = _build_item_payload(doc)

        self.assertEqual(payload["barcodes"], [{"barcode": "9780000000001", "barcode_type": "EAN"}])

    @patch("connector.sync.erpnext_product_sync._get_enabled_sites", return_value=["Shop"])
    @patch("connector.sync.erpnext_product_sync._is_erpnext_site_sync_enabled", return_value=True)
    @patch("connector.sync.erpnext_product_sync.frappe")
    def test_failed_rows_are_retried_after_backoff(self, mock_frappe, _enabled, _sites):
        from connector.sync.erpnext_product_sync import full_erpnext_product_sync

        now = datetime.datetime(2026, 11, 13, 12, 0, 0)
        mock_frappe.utils.now_datetime.return_value = now
        mock_frappe.utils.add_to_date.side_effect = lambda d, minutes=0: d + datetime.timedelta(minutes=minutes)
        mock_frappe.get_doc.return_value.sync_item_group_filter = None
        old = now - datetime.timedelta(hours=3)
        recent = now - datetime.timedelta(minutes=5)
        items = [
            {"item_code": "FAILED-OLD", "modified": old},
            {"item_code": "FAILED-RECENT", "modified": old},
            {"item_code": "SYNCED", "modified": old},
        ]
        maps = [
            {"item_code": "FAILED-OLD", "last_synced_on": now - datetime.timedelta(hours=2), "sync_status": "Failed"},
            {"item_code": "FAILED-RECENT", "last_synced_on": recent, "sync_status": "Failed"},
            {"item_code": "SYNCED", "last_synced_on": recent, "sync_status": "Synced"},
        ]
        mock_frappe.get_all.side_effect = [items, maps]

        full_erpnext_product_sync()

        queued = [c.kwargs["item_code"] for c in mock_frappe.enqueue.call_args_list]
        self.assertEqual(queued, ["FAILED-OLD"])


class TestCancellationComments(unittest.TestCase):

    @patch("connector.sync.status_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.status_sync._magento_orders_for_si", return_value=[("SO-1", 77)])
    @patch("connector.sync.status_sync.frappe")
    def test_invoice_cancel_queues_comment_only_job(self, mock_frappe, _orders, _enabled):
        from connector.sync.status_sync import on_sales_invoice_cancel

        doc = MagicMock()
        doc.name = "SINV-1"
        on_sales_invoice_cancel(doc, "on_cancel")

        self.assertEqual(
            mock_frappe.enqueue.call_args[0][0],
            "connector.sync.status_sync._push_order_comment",
        )

    @patch("connector.sync.status_sync.create_log")
    @patch("connector.sync.status_sync.MagentoClient")
    def test_comment_job_never_requests_processing(self, mock_client_cls, _log):
        from connector.sync.status_sync import _push_order_comment

        _push_order_comment("SO-1", 77, "Sales Invoice SINV-1 was cancelled in ERPNext.")

        client = mock_client_cls.return_value
        client.add_order_comment.assert_called_once()
        client.update_order_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
