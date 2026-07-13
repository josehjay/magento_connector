"""
Unit tests for product_sync.py
"""

import unittest
from unittest.mock import MagicMock, patch

from connector.api.magento_client import MagentoAPIError


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
    @patch("connector.sync.product_sync.upsert_map")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync._get_item_price")
    def test_push_new_item_creates_product(
        self,
        mock_price,
        mock_log,
        mock_upsert,
        mock_client_cls,
        mock_frappe,
    ):
        """push_item_to_magento should call create_product when Magento has no such SKU (404)."""
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.get_doc.return_value = MagicMock(**MOCK_ITEM)
        mock_frappe.get_single.return_value = MagicMock(
            price_list="Standard Selling",
            magento_item_groups=[],
        )
        mock_price.return_value = 99.99

        mock_client = MagicMock()
        mock_client.get_product.side_effect = MagentoAPIError("not found", status_code=404)
        mock_client.create_product.return_value = {"id": 101, "sku": "TEST-SKU-001"}
        mock_client_cls.return_value = mock_client

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        mock_client.create_product.assert_called_once()
        mock_client.update_product.assert_not_called()
        mock_upsert.assert_called_once_with("TEST-SKU-001", 101, "TEST-SKU-001", "Synced")

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.MagentoClient")
    @patch("connector.sync.product_sync.upsert_map")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync._get_item_price")
    def test_push_existing_item_updates_product(
        self,
        mock_price,
        mock_log,
        mock_upsert,
        mock_client_cls,
        mock_frappe,
    ):
        """push_item_to_magento should call update_product when Magento already has the SKU."""
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.get_doc.return_value = MagicMock(**MOCK_ITEM)
        mock_frappe.get_single.return_value = MagicMock(price_list="Standard Selling")
        mock_price.return_value = 49.99

        mock_client = MagicMock()
        mock_client.get_product.return_value = {
            "sku": "TEST-SKU-001",
            "id": 77,
            "custom_attributes": [],
            "extension_attributes": {},
        }
        mock_client.update_product.return_value = {"id": 77, "sku": "TEST-SKU-001"}
        mock_client_cls.return_value = mock_client

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        mock_client.update_product.assert_called_once()
        mock_client.create_product.assert_not_called()

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.MagentoClient")
    @patch("connector.sync.product_sync.upsert_map")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync._get_item_price")
    def test_push_preserves_magento_only_data(
        self,
        mock_price,
        mock_log,
        mock_upsert,
        mock_client_cls,
        mock_frappe,
    ):
        """
        Pushing to an existing Magento product must not wipe category links,
        media gallery entries, or custom attributes ERPNext doesn't manage —
        Magento is never overwritten with data ERPNext doesn't know about.
        ERPNext's name/price must still win over whatever Magento currently has.
        """
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.get_doc.return_value = MagicMock(**MOCK_ITEM)
        mock_frappe.get_single.return_value = MagicMock(
            price_list="Standard Selling",
            magento_item_groups=[],
        )
        mock_price.return_value = 49.99

        mock_client = MagicMock()
        mock_client.get_product.return_value = {
            "sku": "TEST-SKU-001",
            "id": 77,
            "name": "Stale Magento-side Name",
            "price": 999.0,
            "media_gallery_entries": [{"id": 1, "file": "/a/b/existing.jpg"}],
            "custom_attributes": [
                {"attribute_code": "description", "value": "old description"},
                {"attribute_code": "some_magento_only_attr", "value": "keep me"},
            ],
            "extension_attributes": {
                "category_links": [{"category_id": "42"}],
                "stock_item": {"qty": 5, "is_in_stock": True},
            },
        }
        mock_client.update_product.return_value = {"id": 77, "sku": "TEST-SKU-001"}
        mock_client_cls.return_value = mock_client

        from connector.sync.product_sync import push_item_to_magento
        push_item_to_magento("TEST-SKU-001")

        sent_payload = mock_client.update_product.call_args.args[1]

        # ERPNext wins for the fields it owns.
        self.assertEqual(sent_payload["name"], MOCK_ITEM["item_name"])
        self.assertEqual(sent_payload["price"], 49.99)

        # Magento-only data ERPNext never touches is carried through untouched.
        self.assertEqual(
            sent_payload["media_gallery_entries"],
            [{"id": 1, "file": "/a/b/existing.jpg"}],
        )
        self.assertEqual(
            sent_payload["extension_attributes"]["category_links"],
            [{"category_id": "42"}],
        )
        custom_by_code = {a["attribute_code"]: a["value"] for a in sent_payload["custom_attributes"]}
        self.assertEqual(custom_by_code["some_magento_only_attr"], "keep me")
        # ERPNext-managed custom attribute is overwritten with the fresh value.
        self.assertEqual(custom_by_code["description"], MOCK_ITEM["description"])
        # ERPNext-managed extension attribute (stock placeholder) is present too.
        self.assertIn("stock_item", sent_payload["extension_attributes"])

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

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.get_magento_product_id")
    def test_on_item_trash_enqueues_magento_removal(self, mock_get_id, mock_frappe):
        """on_item_trash should enqueue a Magento removal job after commit."""
        mock_frappe.db.get_single_value.return_value = True
        mock_get_id.return_value = 101
        doc = MagicMock(item_code="TEST-SKU-001")
        doc.get.return_value = 1

        from connector.sync.product_sync import on_item_trash
        on_item_trash(doc, "on_trash")

        mock_frappe.enqueue.assert_called_once()
        kwargs = mock_frappe.enqueue.call_args.kwargs
        self.assertEqual(kwargs.get("item_code"), "TEST-SKU-001")
        self.assertEqual(kwargs.get("job_id"), "magento_remove_TEST-SKU-001")
        self.assertTrue(kwargs.get("enqueue_after_commit"))
        self.assertTrue(kwargs.get("deduplicate"))
        self.assertEqual(
            mock_frappe.enqueue.call_args.args[0],
            "connector.sync.product_sync.remove_from_magento",
        )

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.delete_map")
    @patch("connector.sync.product_sync.get_magento_product_id")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync.MagentoClient")
    def test_remove_from_magento_skips_item_field_clear_if_item_deleted(
        self,
        mock_client_cls,
        mock_log,
        mock_get_id,
        mock_delete_map,
        mock_frappe,
    ):
        """remove_from_magento should not call set_value when Item row is gone."""
        mock_get_id.return_value = 101
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_frappe.db.exists.return_value = False

        from connector.sync.product_sync import remove_from_magento
        remove_from_magento("TEST-SKU-001")

        mock_client.delete_product.assert_called_once_with("TEST-SKU-001")
        mock_frappe.db.set_value.assert_not_called()
        mock_frappe.db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
