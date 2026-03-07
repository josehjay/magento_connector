"""
Unit tests for product_sync.py
"""

import unittest
from unittest.mock import MagicMock, patch


MOCK_ITEM = {
    "item_code": "TEST-SKU-001",
    "item_name": "Test Product",
    "description": "A test product description",
    "is_sales_item": 1,
    "sync_to_magento": 1,
    "weight_per_unit": 1.5,
    "magento_product_id": None,
    "magento_last_synced_on": None,
    "magento_sync_error": "",
}


class TestProductSync(unittest.TestCase):

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.MagentoClient")
    @patch("connector.sync.product_sync.get_magento_product_id")
    @patch("connector.sync.product_sync.upsert_map")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync._get_item_price")
    def test_push_new_item_creates_product(
        self,
        mock_price,
        mock_log,
        mock_upsert,
        mock_get_id,
        mock_client_cls,
        mock_frappe,
    ):
        """push_item_to_magento should call create_product for a new item."""
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.get_doc.return_value = MagicMock(**MOCK_ITEM)
        mock_frappe.get_single.return_value = MagicMock(
            price_list="Standard Selling",
            magento_item_groups=[],
        )
        mock_price.return_value = 99.99
        mock_get_id.return_value = None

        mock_client = MagicMock()
        mock_client.product_exists.return_value = False
        mock_client.create_product.return_value = {"id": 101, "sku": "TEST-SKU-001"}
        mock_client_cls.return_value = mock_client

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        mock_client.create_product.assert_called_once()
        mock_upsert.assert_called_once_with("TEST-SKU-001", 101, "TEST-SKU-001", "Synced")

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.MagentoClient")
    @patch("connector.sync.product_sync.get_magento_product_id")
    @patch("connector.sync.product_sync.upsert_map")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync._get_item_price")
    def test_push_existing_item_updates_product(
        self,
        mock_price,
        mock_log,
        mock_upsert,
        mock_get_id,
        mock_client_cls,
        mock_frappe,
    ):
        """push_item_to_magento should call update_product for an existing item."""
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.get_doc.return_value = MagicMock(**MOCK_ITEM)
        mock_frappe.get_single.return_value = MagicMock(price_list="Standard Selling")
        mock_price.return_value = 49.99
        mock_get_id.return_value = 77

        mock_client = MagicMock()
        mock_client.update_product.return_value = {"id": 77, "sku": "TEST-SKU-001"}
        mock_client_cls.return_value = mock_client

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        mock_client.update_product.assert_called_once()
        mock_client.create_product.assert_not_called()

    @patch("connector.sync.product_sync.frappe")
    def test_skips_when_sync_disabled(self, mock_frappe):
        """push_item_to_magento should exit early when sync is disabled."""
        mock_frappe.db.get_single_value.return_value = False

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        mock_frappe.get_doc.assert_not_called()

    @patch("connector.sync.product_sync.frappe")
    def test_skips_item_with_sync_to_magento_false(self, mock_frappe):
        """push_item_to_magento should skip items with sync_to_magento=0."""
        mock_frappe.db.get_single_value.return_value = True
        item = MagicMock(**{**MOCK_ITEM, "sync_to_magento": 0})
        mock_frappe.get_doc.return_value = item

        with patch("connector.sync.product_sync.MagentoClient") as mock_client_cls:
            from connector.sync.product_sync import push_item_to_magento
            push_item_to_magento("TEST-SKU-001")
            mock_client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
