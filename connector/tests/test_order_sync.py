"""
Unit tests for order_sync.py
"""

import unittest
from unittest.mock import MagicMock, patch


SAMPLE_MAGENTO_ORDER = {
    "entity_id": 1001,
    "increment_id": "000001001",
    "status": "processing",
    "state": "processing",
    "customer_email": "john@example.com",
    "customer_firstname": "John",
    "customer_lastname": "Doe",
    "customer_is_guest": False,
    "customer_id": 55,
    "order_currency_code": "USD",
    "tax_amount": 8.50,
    "shipping_amount": 5.00,
    "shipping_tax_amount": 0,
    "shipping_description": "Flat Rate",
    "billing_address": {
        "firstname": "John",
        "lastname": "Doe",
        "street": ["123 Main St"],
        "city": "Anytown",
        "region": "California",
        "postcode": "90210",
        "country_id": "US",
        "telephone": "555-0100",
    },
    "extension_attributes": {
        "shipping_assignments": [{
            "shipping": {
                "address": {
                    "firstname": "John",
                    "lastname": "Doe",
                    "street": ["123 Main St", "Apt 4"],
                    "city": "Anytown",
                    "region": "California",
                    "postcode": "90210",
                    "country_id": "US",
                    "telephone": "555-0100",
                }
            }
        }]
    },
    "items": [
        {
            "sku": "ITEM-001",
            "name": "Test Product",
            "qty_ordered": 2,
            "price": 49.99,
            "product_type": "simple",
        }
    ],
}


class TestOrderSync(unittest.TestCase):

    @patch("connector.sync.order_sync.frappe")
    @patch("connector.sync.order_sync.is_order_imported")
    @patch("connector.sync.order_sync.create_map")
    @patch("connector.sync.order_sync.create_log")
    @patch("connector.sync.order_sync.get_or_create_customer")
    @patch("connector.sync.order_sync.get_or_create_address")
    def test_imports_new_order_as_draft(
        self,
        mock_address,
        mock_customer,
        mock_log,
        mock_create_map,
        mock_is_imported,
        mock_frappe,
    ):
        """A new Magento order should be created as a Draft Sales Order."""
        mock_is_imported.return_value = False
        mock_customer.return_value = "John Doe"
        mock_address.return_value = "John Doe-Shipping"

        mock_frappe.db.exists.return_value = True
        mock_frappe.db.get_value.return_value = "Nos"
        mock_frappe.db.get_single_value.side_effect = lambda dt, field: {
            ("Magento Settings", "sync_enabled"): True,
            ("Magento Settings", "lead_time_days"): 3,
        }.get((dt, field), None)
        mock_frappe.defaults.get_defaults.return_value = {"company": "Test Co"}

        mock_so = MagicMock()
        mock_so.name = "SAL-ORD-2024-00001"
        mock_frappe.new_doc.return_value = mock_so

        from connector.sync.order_sync import _process_order
        result = _process_order(SAMPLE_MAGENTO_ORDER, MagicMock())

        self.assertEqual(result, "imported")
        mock_so.insert.assert_called_once()
        mock_so.submit.assert_not_called()

    @patch("connector.sync.order_sync.frappe")
    @patch("connector.sync.order_sync.is_order_imported")
    @patch("connector.sync.order_sync._sync_status_from_magento")
    def test_skips_already_imported_order(self, mock_status_sync, mock_is_imported, mock_frappe):
        """An order already in the map should not create a duplicate SO."""
        mock_is_imported.return_value = True

        from connector.sync.order_sync import _process_order
        result = _process_order(SAMPLE_MAGENTO_ORDER, MagicMock())

        self.assertEqual(result, "updated")
        mock_status_sync.assert_called_once()

    @patch("connector.sync.order_sync.frappe")
    @patch("connector.sync.order_sync.is_order_imported")
    @patch("connector.sync.order_sync.create_log")
    def test_skips_cancelled_order(self, mock_log, mock_is_imported, mock_frappe):
        """A cancelled Magento order should be skipped, not imported."""
        mock_is_imported.return_value = False
        cancelled_order = {**SAMPLE_MAGENTO_ORDER, "status": "canceled"}

        from connector.sync.order_sync import _process_order
        result = _process_order(cancelled_order, MagicMock())

        self.assertEqual(result, "skipped")

    def test_build_order_items_skips_missing_sku(self):
        """Items with SKUs not in ERPNext should be silently skipped."""
        with patch("connector.sync.order_sync.frappe") as mock_frappe:
            mock_frappe.db.exists.return_value = False
            mock_frappe.logger.return_value = MagicMock()
            mock_frappe.db.get_single_value.return_value = 3

            from connector.sync.order_sync import _build_order_items
            items = _build_order_items(SAMPLE_MAGENTO_ORDER)

        self.assertEqual(len(items), 0)

    def test_build_order_items_includes_valid_sku(self):
        """Items with known SKUs should be included in the order items list."""
        with patch("connector.sync.order_sync.frappe") as mock_frappe:
            mock_frappe.db.exists.return_value = True
            mock_frappe.db.get_value.return_value = "Nos"
            mock_frappe.db.get_single_value.return_value = 3
            mock_frappe.logger.return_value = MagicMock()

            from connector.sync.order_sync import _build_order_items
            items = _build_order_items(SAMPLE_MAGENTO_ORDER)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_code"], "ITEM-001")
        self.assertEqual(items[0]["qty"], 2.0)
        self.assertAlmostEqual(items[0]["rate"], 49.99)


if __name__ == "__main__":
    unittest.main()
