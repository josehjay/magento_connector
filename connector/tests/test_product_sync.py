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
    def test_on_item_trash_enqueues_magento_removal(self, mock_frappe):
        """on_item_trash should enqueue a Magento removal job after commit."""
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.db.exists.return_value = True
        doc = MagicMock(item_code="TEST-SKU-001")
        doc.get.side_effect = lambda key, *a, **k: {
            "item_code": "TEST-SKU-001",
            "name": "TEST-SKU-001",
            "sync_to_magento": 1,
        }.get(key, 1)

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
        mock_frappe.db.get_value.return_value = {
            "magento_product_id": 101,
            "magento_sku": "TEST-SKU-001",
        }
        mock_frappe.db.exists.side_effect = lambda dt, name: dt == "Magento Product Map"

        from connector.sync.product_sync import remove_from_magento
        remove_from_magento("TEST-SKU-001")

        mock_client.delete_product.assert_called_once_with("TEST-SKU-001")
        mock_frappe.db.set_value.assert_not_called()
        mock_frappe.db.commit.assert_called_once()
        mock_delete_map.assert_called_once_with("TEST-SKU-001")

    @patch("connector.sync.product_sync.frappe")
    @patch("connector.sync.product_sync.delete_map")
    @patch("connector.sync.product_sync.get_magento_product_id")
    @patch("connector.sync.product_sync.create_log")
    @patch("connector.sync.product_sync.MagentoClient")
    def test_remove_from_magento_uses_mapped_sku(
        self,
        mock_client_cls,
        mock_log,
        mock_get_id,
        mock_delete_map,
        mock_frappe,
    ):
        """Deletion must use magento_sku from the map when it differs from item_code."""
        mock_get_id.return_value = 55
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_frappe.db.get_value.return_value = {
            "magento_product_id": 55,
            "magento_sku": "ALT-SKU",
        }
        mock_frappe.db.exists.return_value = False

        from connector.sync.product_sync import remove_from_magento
        remove_from_magento("TEST-SKU-001")

        mock_client.delete_product.assert_called_once_with("ALT-SKU")
        mock_delete_map.assert_called_once_with("TEST-SKU-001")


class TestGetItemsNeedingFullSync(unittest.TestCase):
    """
    _get_items_needing_full_sync must skip items already "Synced" and
    unchanged (whether that came from a real push or Rebuild Product Maps),
    retry "Failed" items under the retry cap, and respect the "Item Groups
    to Sync" filter.
    """

    @staticmethod
    def _wire_get_all(mock_frappe, items, map_rows):
        def side_effect(doctype, **kwargs):
            if doctype == "Item":
                return items
            if doctype == "Magento Product Map":
                return map_rows
            return []
        mock_frappe.get_all.side_effect = side_effect

    @patch("connector.sync.product_sync.frappe")
    def test_skips_synced_unchanged_item(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-01 00:00:00",
            "magento_last_synced_on": "2026-01-02 00:00:00",
            "has_variants": 0, "variant_of": None,
        }]
        map_rows = [{"item_code": "A", "sync_status": "Synced", "retry_count": 0}]
        self._wire_get_all(mock_frappe, items, map_rows)

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), [])

    @patch("connector.sync.product_sync.frappe")
    def test_includes_synced_item_edited_since_last_sync(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-03 00:00:00",
            "magento_last_synced_on": "2026-01-02 00:00:00",
            "has_variants": 0, "variant_of": None,
        }]
        map_rows = [{"item_code": "A", "sync_status": "Synced", "retry_count": 0}]
        self._wire_get_all(mock_frappe, items, map_rows)

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), ["A"])

    @patch("connector.sync.product_sync.frappe")
    def test_includes_never_synced_item(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-01 00:00:00",
            "magento_last_synced_on": None,
            "has_variants": 0, "variant_of": None,
        }]
        self._wire_get_all(mock_frappe, items, [])  # no map row at all

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), ["A"])

    @patch("connector.sync.product_sync.frappe")
    def test_includes_pending_item(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-01 00:00:00",
            "magento_last_synced_on": None,
            "has_variants": 0, "variant_of": None,
        }]
        map_rows = [{"item_code": "A", "sync_status": "Pending", "retry_count": 0}]
        self._wire_get_all(mock_frappe, items, map_rows)

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), ["A"])

    @patch("connector.sync.product_sync.frappe")
    def test_retries_failed_item_under_the_retry_cap(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-01 00:00:00",
            "magento_last_synced_on": None,
            "has_variants": 0, "variant_of": None,
        }]
        map_rows = [{"item_code": "A", "sync_status": "Failed", "retry_count": 3}]
        self._wire_get_all(mock_frappe, items, map_rows)

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), ["A"])

    @patch("connector.sync.product_sync.frappe")
    def test_skips_failed_item_that_exhausted_retries(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        items = [{
            "item_code": "A", "modified": "2026-01-01 00:00:00",
            "magento_last_synced_on": None,
            "has_variants": 0, "variant_of": None,
        }]
        map_rows = [{"item_code": "A", "sync_status": "Failed", "retry_count": 999}]
        self._wire_get_all(mock_frappe, items, map_rows)

        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), [])

    @patch("connector.sync.product_sync.frappe")
    def test_respects_item_groups_to_sync_filter(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(
            magento_item_groups=[MagicMock(item_group="Books")]
        )
        captured_filters = {}

        def side_effect(doctype, **kwargs):
            if doctype == "Item":
                captured_filters.update(kwargs.get("filters") or {})
            return []
        mock_frappe.get_all.side_effect = side_effect

        from connector.sync.product_sync import _get_items_needing_full_sync
        _get_items_needing_full_sync()

        self.assertEqual(captured_filters.get("item_group"), ["in", ["Books"]])


class TestRunBatchProductSyncSoftDeadline(unittest.TestCase):
    """
    A soft time budget must stop the batch early instead of ever risking a
    hard job-timeout kill mid-item — untouched items are simply left for the
    caller to retry on the next chunk.
    """

    @patch("connector.sync.product_sync.push_item_to_magento")
    @patch("connector.sync.product_sync.time")
    @patch("connector.sync.product_sync.frappe")
    def test_stops_once_time_budget_is_exhausted(self, mock_frappe, mock_time, mock_push):
        # monotonic() is called once up front to compute the deadline (t=0,
        # budget=10 -> deadline=10), then once per item before it starts:
        # item A at t=1 (within budget, proceeds), item B at t=20 (budget
        # already exceeded, loop stops before pushing B or C).
        mock_time.monotonic.side_effect = [0, 1, 20]

        from connector.sync.product_sync import _run_batch_product_sync
        _run_batch_product_sync(["A", "B", "C"], time_budget_seconds=10)

        mock_push.assert_called_once_with("A")

    @patch("connector.sync.product_sync.push_item_to_magento")
    @patch("connector.sync.product_sync.frappe")
    def test_processes_all_items_when_no_budget_given(self, mock_frappe, mock_push):
        from connector.sync.product_sync import _run_batch_product_sync
        _run_batch_product_sync(["A", "B", "C"])

        self.assertEqual(mock_push.call_count, 3)

    @patch("connector.sync.product_sync.push_item_to_magento")
    @patch("connector.sync.product_sync.frappe")
    def test_one_item_failure_does_not_stop_the_rest(self, mock_frappe, mock_push):
        mock_push.side_effect = [None, Exception("boom"), None]

        from connector.sync.product_sync import _run_batch_product_sync
        _run_batch_product_sync(["A", "B", "C"])

        self.assertEqual(mock_push.call_count, 3)


class TestRetryFailedProductSyncItemGroupFilter(unittest.TestCase):
    """retry_failed_product_sync must stop retrying items whose Item Group is
    no longer in "Item Groups to Sync"."""

    @patch("connector.sync.product_sync.frappe")
    def test_respects_item_groups_to_sync_filter(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(
            magento_item_groups=[MagicMock(item_group="Books")]
        )

        captured_item_filters = {}

        def side_effect(doctype, **kwargs):
            if doctype == "Magento Product Map":
                filters = kwargs.get("filters") or {}
                if filters.get("sync_status") == "Failed":
                    return [{"item_code": "A", "retry_count": 1, "last_failed_at": None}]
                return []  # no Pending
            if doctype == "Item":
                captured_item_filters.update(kwargs.get("filters") or {})
                return []  # "A" is not in an allowed group -> filtered out
            return []
        mock_frappe.get_all.side_effect = side_effect

        from connector.sync.product_sync import retry_failed_product_sync
        retry_failed_product_sync()

        self.assertEqual(captured_item_filters.get("item_group"), ["in", ["Books"]])
        mock_frappe.enqueue.assert_not_called()

    @patch("connector.sync.product_sync.frappe")
    def test_includes_pending_map_rows(self, mock_frappe):
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])
        mock_frappe.utils.now_datetime.return_value = "2026-01-01 00:00:00"
        mock_frappe.utils.add_to_date.side_effect = lambda *a, **k: "2099-01-01"

        def side_effect(doctype, **kwargs):
            if doctype == "Magento Product Map":
                filters = kwargs.get("filters") or {}
                if filters.get("sync_status") == "Failed":
                    return []
                if filters.get("sync_status") == "Pending":
                    return [{"item_code": "PENDING-1"}]
                return []
            if doctype == "Item":
                return [{"item_code": "PENDING-1"}] if "pluck" not in kwargs else ["PENDING-1"]
            return []
        mock_frappe.get_all.side_effect = side_effect

        from connector.sync.product_sync import retry_failed_product_sync
        retry_failed_product_sync()

        mock_frappe.enqueue.assert_called_once()
        self.assertEqual(
            mock_frappe.enqueue.call_args.kwargs.get("item_codes"),
            ["PENDING-1"],
        )


class TestFullProductSyncChunkChaining(unittest.TestCase):
    """Follow-up chunks must use unique job_ids so deduplicate doesn't drop them."""

    @patch("connector.sync.product_sync._run_batch_product_sync")
    @patch("connector.sync.product_sync._get_items_needing_full_sync")
    @patch("connector.sync.product_sync.frappe")
    def test_enqueues_next_chunk_with_unique_job_id(
        self, mock_frappe, mock_needs, mock_batch
    ):
        mock_frappe.db.get_single_value.return_value = True
        mock_frappe.cache.return_value.set_value = MagicMock()
        mock_frappe.cache.return_value.delete_value = MagicMock()
        # First call (before process): 25 items; second call (after): 5 remain.
        mock_needs.side_effect = [
            [f"I{i}" for i in range(25)],
            [f"I{i}" for i in range(5)],
        ]

        from connector.sync.product_sync import (
            BATCH_SIZE,
            run_full_product_sync_chunk,
            _full_sync_chunk_job_id,
        )

        run_full_product_sync_chunk(chunk_no=0)

        mock_batch.assert_called_once()
        self.assertEqual(len(mock_batch.call_args.args[0]), BATCH_SIZE)
        mock_frappe.enqueue.assert_called_once()
        kwargs = mock_frappe.enqueue.call_args.kwargs
        self.assertEqual(kwargs.get("job_id"), _full_sync_chunk_job_id(1))
        self.assertEqual(kwargs.get("chunk_no"), 1)
        self.assertTrue(kwargs.get("deduplicate"))


if __name__ == "__main__":
    unittest.main()
