"""
Tests for "Selected Items Only" Item Groups: a group can be added to "Item
Groups to Sync" while only hand-picked items from it are pushed to Magento.
Every push path must apply the same rule; Magento Settings validates the
picks; the form gets a push-endpoint security status.
"""

import unittest
from unittest.mock import MagicMock, patch


class Row(dict):
    """Child-table row: attribute AND .get() access, like a Frappe row."""

    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


def _settings(groups=(), selected=()):
    """groups: [(item_group, sync_mode)], selected: [item_code]."""
    s = MagicMock()
    s.magento_item_groups = [
        Row(item_group=g, sync_mode=m, idx=i + 1) for i, (g, m) in enumerate(groups)
    ]
    s.magento_selected_items = [Row(item_code=c, idx=i + 1) for i, c in enumerate(selected)]
    return s


SEL = "Selected Items Only"
ALL = "All Items"
MIXED = _settings([("Books", ALL), ("Bags", SEL)], ["BAG-1"])


class TestSyncScope(unittest.TestCase):

    def _scope(self, settings):
        from connector.sync.product_sync import MagentoSyncScope
        return MagentoSyncScope(settings)

    def test_empty_groups_table_allows_everything(self):
        scope = self._scope(_settings())
        self.assertTrue(scope.allows("Anything", "X"))
        self.assertEqual(scope.item_query({"disabled": 0}), ({"disabled": 0}, None))

    def test_all_items_group_allows_every_item(self):
        scope = self._scope(MIXED)
        self.assertTrue(scope.allows("Books", "BOOK-99"))

    def test_selected_only_group_allows_only_picked_items(self):
        scope = self._scope(MIXED)
        self.assertTrue(scope.allows("Bags", "BAG-1"))
        self.assertFalse(scope.allows("Bags", "BAG-2"))

    def test_group_not_in_table_is_rejected_even_if_picked(self):
        scope = self._scope(MIXED)
        self.assertFalse(scope.allows("Pens", "BAG-1"))

    def test_rows_without_sync_mode_default_to_all_items(self):
        s = MagicMock()
        s.magento_item_groups = [MagicMock(item_group="Books")]  # legacy row
        scope = self._scope(s)
        self.assertTrue(scope.allows("Books", "ANY"))
        self.assertEqual(
            scope.item_query({"disabled": 0}),
            ({"disabled": 0, "item_group": ["in", ["Books"]]}, None),
        )

    def test_item_query_narrows_selected_groups_with_or_filters(self):
        filters, or_filters = self._scope(MIXED).item_query({"disabled": 0})
        self.assertEqual(filters["item_group"], ["in", ["Bags", "Books"]])
        self.assertEqual(
            or_filters,
            [["item_group", "in", ["Books"]], ["item_code", "in", ["BAG-1"]]],
        )

    def test_item_query_none_when_nothing_can_sync(self):
        scope = self._scope(_settings([("Bags", SEL)], []))
        self.assertIsNone(scope.item_query({"disabled": 0}))

    @patch("connector.sync.product_sync.frappe")
    def test_get_eligible_item_rows_skips_query_when_nothing_can_sync(self, mock_frappe):
        mock_frappe.get_single.return_value = _settings([("Bags", SEL)], [])
        from connector.sync.product_sync import get_eligible_item_rows
        self.assertEqual(get_eligible_item_rows({"disabled": 0}, ["item_code"]), [])
        mock_frappe.get_all.assert_not_called()


def _item(**data):
    item = MagicMock(**data)
    item.get.side_effect = lambda key, *a, **k: data.get(key)
    return item


@patch("connector.sync.product_sync._is_sync_enabled", return_value=True)
class TestPushPaths(unittest.TestCase):

    @patch("connector.sync.product_sync._enqueue_product_sync")
    @patch("connector.sync.product_sync.frappe")
    def test_on_item_save_skips_unpicked_item(self, mock_frappe, mock_enqueue, _e):
        mock_frappe.get_single.return_value = MIXED
        from connector.sync.product_sync import on_item_save
        on_item_save(_item(item_code="BAG-2", item_group="Bags", sync_to_magento=1, disabled=0), "on_update")
        mock_enqueue.assert_not_called()

    @patch("connector.sync.product_sync._enqueue_product_sync")
    @patch("connector.sync.product_sync.frappe")
    def test_on_item_save_pushes_picked_item(self, mock_frappe, mock_enqueue, _e):
        mock_frappe.get_single.return_value = MIXED
        from connector.sync.product_sync import on_item_save
        on_item_save(_item(item_code="BAG-1", item_group="Bags", sync_to_magento=1, disabled=0), "on_update")
        mock_enqueue.assert_called_once_with("BAG-1", mode="push")

    @patch("connector.sync.product_sync.MagentoClient")
    @patch("connector.sync.product_sync.frappe")
    def test_push_never_contacts_magento_for_unpicked_item(self, mock_frappe, mock_client, _e):
        mock_frappe.get_single.return_value = MIXED
        doc = _item(item_code="BAG-2", item_group="Bags", sync_to_magento=1, disabled=0)
        doc.item_group = "Bags"
        mock_frappe.get_doc.return_value = doc
        from connector.sync.product_sync import _push_item_to_magento
        _push_item_to_magento("BAG-2")
        mock_client.assert_not_called()

    @patch("connector.sync.product_sync._enqueue_product_sync")
    @patch("connector.sync.product_sync.frappe")
    def test_price_change_skips_unpicked_item(self, mock_frappe, mock_enqueue, _e):
        mock_frappe.get_single.return_value = MIXED
        mock_frappe.db.get_single_value.return_value = "Standard Selling"
        mock_frappe.db.get_value.return_value = {"disabled": 0, "sync_to_magento": 1, "item_group": "Bags"}
        from connector.sync.product_sync import on_item_price_change
        price = _item(item_code="BAG-2", price_list="Standard Selling", selling=1)
        on_item_price_change(price, "on_update")
        mock_enqueue.assert_not_called()

        mock_frappe.db.get_value.return_value = {"disabled": 0, "sync_to_magento": 1, "item_group": "Books"}
        on_item_price_change(_item(item_code="BOOK-1", price_list="Standard Selling", selling=1), "on_update")
        mock_enqueue.assert_called_once_with("BOOK-1", mode="push")

    @patch("connector.sync.product_sync._enqueue_product_sync")
    @patch("connector.sync.product_sync.frappe")
    def test_price_change_skips_item_not_marked_for_sync(self, mock_frappe, mock_enqueue, _e):
        mock_frappe.get_single.return_value = _settings()
        mock_frappe.db.get_single_value.return_value = "Standard Selling"
        mock_frappe.db.get_value.return_value = {"disabled": 0, "sync_to_magento": 0, "item_group": "Books"}
        from connector.sync.product_sync import on_item_price_change
        on_item_price_change(_item(item_code="BOOK-1", price_list="Standard Selling", selling=1), "on_update")
        mock_enqueue.assert_not_called()

    @patch("connector.sync.product_sync.frappe")
    def test_full_sync_scan_uses_selection_filters(self, mock_frappe, _e):
        mock_frappe.get_single.return_value = MIXED
        mock_frappe.get_all.return_value = []
        from connector.sync.product_sync import _get_items_needing_full_sync
        self.assertEqual(_get_items_needing_full_sync(), [])
        kwargs = mock_frappe.get_all.call_args.kwargs
        self.assertEqual(kwargs["filters"]["item_group"], ["in", ["Bags", "Books"]])
        self.assertIn(["item_code", "in", ["BAG-1"]], kwargs["or_filters"])

    @patch("connector.sync.product_sync.frappe")
    def test_retry_drops_unpicked_items(self, mock_frappe, _e):
        mock_frappe.get_single.return_value = MIXED
        captured = {}

        def get_all(doctype, **kwargs):
            if doctype == "Magento Product Map":
                if (kwargs.get("filters") or {}).get("sync_status") == "Failed":
                    return [{"item_code": "BAG-2", "retry_count": 1, "last_failed_at": None}]
                return []
            if doctype == "Item":
                captured.update(kwargs)
                return []  # BAG-2 is not picked → filtered out by the query
            return []
        mock_frappe.get_all.side_effect = get_all

        from connector.sync.product_sync import retry_failed_product_sync
        retry_failed_product_sync()

        self.assertIn(["item_code", "in", ["BAG-1"]], captured["or_filters"])
        mock_frappe.enqueue.assert_not_called()


class TestRebuildEligibility(unittest.TestCase):

    @patch("connector.sync.product_sync.frappe")
    def test_rebuild_eligible_items_use_selection(self, mock_frappe):
        mock_frappe.get_single.return_value = MIXED
        mock_frappe.get_all.return_value = [
            {"item_code": "BAG-1", "has_variants": 0},
            {"item_code": "BOOK-1", "has_variants": 1},
        ]
        from connector.sync.product_map_rebuild import _get_eligible_items
        rows = _get_eligible_items()
        self.assertEqual([r["item_code"] for r in rows], ["BOOK-1", "BAG-1"])
        kwargs = mock_frappe.get_all.call_args.kwargs
        self.assertIn(["item_code", "in", ["BAG-1"]], kwargs["or_filters"])
        self.assertEqual(kwargs["order_by"], "item_code asc")


def _settings_doc(groups=(), selected=()):
    from connector.connector.doctype.magento_settings.magento_settings import MagentoSettings
    doc = MagentoSettings()
    doc.magento_url = ""
    doc.magento_item_groups = [
        Row(item_group=g, sync_mode=m, attribute_set_name="", attribute_set_id=None, idx=i + 1)
        for i, (g, m) in enumerate(groups)
    ]
    doc.magento_selected_items = [
        Row(item_code=c, item_group=grp, idx=i + 1) for i, (c, grp) in enumerate(selected)
    ]
    return doc


MS = "connector.connector.doctype.magento_settings.magento_settings.frappe"


class TestSettingsValidation(unittest.TestCase):

    @patch(MS)
    def test_duplicate_selected_item_throws(self, mock_frappe):
        mock_frappe.throw.side_effect = Exception
        doc = _settings_doc([("Bags", SEL)], [("BAG-1", "Bags"), ("BAG-1", "Bags")])
        with self.assertRaises(Exception):
            doc.validate()
        self.assertIn("Row 2", mock_frappe.throw.call_args.args[0])

    @patch(MS)
    def test_item_from_non_selected_group_throws_with_row(self, mock_frappe):
        mock_frappe.throw.side_effect = Exception
        doc = _settings_doc([("Books", ALL), ("Bags", SEL)], [("BAG-1", "Bags"), ("BOOK-1", "Books")])
        with self.assertRaises(Exception):
            doc.validate()
        msg = mock_frappe.throw.call_args.args[0]
        self.assertIn("Row 2", msg)
        self.assertIn("BOOK-1", msg)

    @patch(MS)
    def test_warns_when_selected_group_has_no_picks(self, mock_frappe):
        mock_frappe.get_all.return_value = []
        mock_frappe.utils.escape_html.side_effect = lambda s: s
        doc = _settings_doc([("Bags", SEL)], [])
        doc.validate()
        mock_frappe.throw.assert_not_called()
        titles = [c.kwargs.get("title") for c in mock_frappe.msgprint.call_args_list]
        self.assertIn("No Items Selected", titles)

    @patch(MS)
    def test_warns_with_count_of_synced_items_that_stop_syncing(self, mock_frappe):
        mock_frappe.get_all.return_value = ["BAG-1", "BAG-2", "BAG-3"]
        mock_frappe.db.count.return_value = 2
        doc = _settings_doc([("Bags", SEL)], [("BAG-1", "Bags")])
        doc.validate()
        unpicked = mock_frappe.db.count.call_args.args[1]["item_code"][1]
        self.assertEqual(sorted(unpicked), ["BAG-2", "BAG-3"])
        call = mock_frappe.msgprint.call_args
        self.assertEqual(call.kwargs.get("indicator"), "orange")
        self.assertIn("2 item(s)", call.args[0])
        self.assertIn("NOT deleted", call.args[0])

    @patch(MS)
    def test_all_items_groups_skip_selection_checks(self, mock_frappe):
        doc = _settings_doc([("Books", ALL)], [])
        doc.validate()
        mock_frappe.msgprint.assert_not_called()
        mock_frappe.get_all.assert_not_called()


class TestPushSecurityStatus(unittest.TestCase):

    def _status(self, mock_frappe, values, roles=("System Manager",)):
        mock_frappe.get_roles.return_value = list(roles)
        mock_frappe.db.get_single_value.side_effect = lambda dt, f: values.get(f)
        return _settings_doc().get_push_security_status()

    @patch(MS)
    def test_insecure_when_role_missing(self, mock_frappe):
        s = self._status(mock_frappe, {
            "enable_signed_push_verification": 1, "enforce_signed_push_verification": 1,
        })
        self.assertFalse(s["secure"])
        self.assertIsNone(s["push_endpoint_role"])

    @patch(MS)
    def test_insecure_when_signing_not_enforced(self, mock_frappe):
        s = self._status(mock_frappe, {
            "push_endpoint_role": "Magento Push", "enable_signed_push_verification": 1,
        })
        self.assertFalse(s["secure"])
        self.assertTrue(s["signing_enabled"])
        self.assertFalse(s["signing_enforced"])

    @patch(MS)
    def test_enforce_without_enable_is_not_enforced(self, mock_frappe):
        s = self._status(mock_frappe, {
            "push_endpoint_role": "Magento Push", "enforce_signed_push_verification": 1,
        })
        self.assertFalse(s["signing_enforced"])

    @patch(MS)
    def test_secure_when_role_and_enforced_signing(self, mock_frappe):
        s = self._status(mock_frappe, {
            "push_endpoint_role": " Magento Push ",
            "enable_signed_push_verification": 1, "enforce_signed_push_verification": 1,
        })
        self.assertTrue(s["secure"])
        self.assertEqual(s["push_endpoint_role"], "Magento Push")

    @patch(MS)
    def test_non_system_manager_gets_nothing(self, mock_frappe):
        self.assertIsNone(self._status(mock_frappe, {}, roles=("Sales User",)))


if __name__ == "__main__":
    unittest.main()
