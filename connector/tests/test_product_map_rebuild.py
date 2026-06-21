"""Tests for product map rebuild (read-only Magento lookup)."""

import unittest
from unittest.mock import MagicMock, patch


class TestProductMapRebuild(unittest.TestCase):
    def test_summarize_results(self):
        from connector.sync.product_map_rebuild import _summarize_results

        summary = _summarize_results([
            {"status": "mapped"},
            {"status": "skipped_not_in_magento"},
            {"status": "failed"},
        ])
        self.assertEqual(summary["mapped"], 1)
        self.assertEqual(summary["skipped_not_in_magento"], 1)
        self.assertEqual(summary["failed"], 1)

    @patch("connector.sync.product_map_rebuild.frappe")
    def test_rebuild_map_dry_run(self, mock_frappe):
        from connector.sync.product_map_rebuild import rebuild_map_for_item

        mock_frappe.db.get_value.return_value = None
        client = MagicMock()
        client.product_exists.return_value = True
        client.get_product.return_value = {"id": 42, "sku": "ITEM-001"}

        result = rebuild_map_for_item(client, "ITEM-001", dry_run=True)

        self.assertEqual(result["status"], "dry_run_ok")
        self.assertEqual(result["magento_product_id"], 42)
        client.get_product.assert_called_once()

    @patch("connector.sync.product_map_rebuild.frappe")
    def test_rebuild_skips_already_mapped_without_api(self, mock_frappe):
        from connector.sync.product_map_rebuild import rebuild_map_for_item

        mock_frappe.db.get_value.return_value = {
            "magento_product_id": 99,
            "sync_status": "Synced",
            "magento_sku": "ITEM-001",
        }
        client = MagicMock()

        result = rebuild_map_for_item(client, "ITEM-001", skip_existing=True)

        self.assertEqual(result["status"], "skipped_existing")
        client.product_exists.assert_not_called()
        client.get_product.assert_not_called()

    @patch("connector.sync.product_map_rebuild.frappe")
    def test_rebuild_skips_not_in_magento(self, mock_frappe):
        from connector.sync.product_map_rebuild import rebuild_map_for_item

        mock_frappe.db.get_value.return_value = None
        mock_frappe.db.exists.return_value = True
        client = MagicMock()
        client.product_exists.return_value = False

        result = rebuild_map_for_item(
            client, "MISSING-SKU", dry_run=False, mark_not_in_magento=True
        )

        self.assertEqual(result["status"], "skipped_not_in_magento")
        client.get_product.assert_not_called()


if __name__ == "__main__":
    unittest.main()
