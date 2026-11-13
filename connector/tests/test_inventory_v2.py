"""
Inventory sync v2: available stock (actual - reserved) over the chosen
warehouses, pushed only when it changed (or is >24h stale), with the
per-item Send Stock flag read in one query; plus remote ERPNext Item Price
upserts, paginated Magento categories and the pin-sync job removal.
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch

NOW = datetime.datetime(2026, 11, 13, 12, 0, 0)


def _frappe_mock(mapped, items, warehouses=None, qty_rows=None, send_stock=1):
    """A frappe mock wired for sync_inventory."""
    m = MagicMock()
    m.utils.now_datetime.return_value = NOW
    m.utils.get_datetime.side_effect = lambda v: v

    def single(doctype, field):
        return {"sync_enabled": 1, "send_stock_to_magento": send_stock}.get(field)

    m.db.get_single_value.side_effect = single

    def get_all(doctype, **kwargs):
        if doctype == "Magento Product Map":
            return mapped
        if doctype == "Item":
            return items
        if doctype == "Magento Stock Warehouse":
            return list(warehouses or [])
        raise AssertionError(doctype)

    m.get_all.side_effect = get_all
    m.db.sql.return_value = qty_rows or []
    return m


def _map(code, last_qty=None, last_on=None, sku=None):
    return {
        "name": code,
        "item_code": code,
        "magento_sku": sku or code,
        "last_pushed_qty": last_qty,
        "last_stock_push_on": last_on,
    }


@patch("connector.sync.inventory_sync.create_log")
@patch("connector.sync.inventory_sync._is_magento_enabled", return_value=True)
class TestSyncInventoryV2(unittest.TestCase):

    def _run(self, mock_frappe, client):
        with patch("connector.sync.inventory_sync.frappe", mock_frappe), \
             patch("connector.sync.inventory_sync.MagentoClient", return_value=client):
            from connector.sync.inventory_sync import sync_inventory
            sync_inventory()

    def test_pushes_only_changed_never_pushed_and_stale_items(self, _enabled, _log):
        fresh = NOW - datetime.timedelta(hours=1)
        stale = NOW - datetime.timedelta(hours=25)
        mapped = [
            _map("SAME", 5, fresh),
            _map("CHANGED", 5, fresh),
            _map("NEVER"),
            _map("STALE", 3, stale),
        ]
        items = [{"name": c, "magento_send_stock": None} for c in ("SAME", "CHANGED", "NEVER", "STALE")]
        qty_rows = [
            {"item_code": "SAME", "available_qty": 5},
            {"item_code": "CHANGED", "available_qty": 7},
            {"item_code": "NEVER", "available_qty": 2},
            {"item_code": "STALE", "available_qty": 3},
        ]
        m = _frappe_mock(mapped, items, qty_rows=qty_rows)
        client = MagicMock()

        self._run(m, client)

        pushed = {c.args[0]: c.args[1] for c in client.update_stock.call_args_list}
        self.assertEqual(pushed, {"CHANGED": 7.0, "NEVER": 2.0, "STALE": 3.0})
        updates = {c.args[1]: c.args[2] for c in m.db.set_value.call_args_list}
        self.assertEqual(set(updates), {"CHANGED", "NEVER", "STALE"})
        self.assertEqual(updates["CHANGED"]["last_pushed_qty"], 7.0)
        self.assertEqual(updates["CHANGED"]["last_stock_push_on"], NOW)
        for c in m.db.set_value.call_args_list:
            self.assertEqual(c.args[0], "Magento Product Map")
            self.assertIs(c.kwargs.get("update_modified"), False)
        m.db.commit.assert_called()

    def test_nothing_changed_does_not_even_build_a_client(self, _enabled, _log):
        fresh = NOW - datetime.timedelta(hours=1)
        m = _frappe_mock(
            [_map("A", 4, fresh)],
            [{"name": "A", "magento_send_stock": None}],
            qty_rows=[{"item_code": "A", "available_qty": 4}],
        )
        with patch("connector.sync.inventory_sync.frappe", m), \
             patch("connector.sync.inventory_sync.MagentoClient") as client_cls:
            from connector.sync.inventory_sync import sync_inventory
            sync_inventory()
        client_cls.assert_not_called()

    def test_missing_bin_counts_as_zero_and_negative_is_floored(self, _enabled, _log):
        m = _frappe_mock(
            [_map("NOBIN", 3, NOW), _map("NEG", 1, NOW)],
            [{"name": "NOBIN", "magento_send_stock": None}, {"name": "NEG", "magento_send_stock": None}],
            qty_rows=[{"item_code": "NEG", "available_qty": -4}],
        )
        client = MagicMock()
        self._run(m, client)
        pushed = {c.args[0]: c.args[1] for c in client.update_stock.call_args_list}
        self.assertEqual(pushed, {"NOBIN": 0.0, "NEG": 0.0})

    def test_send_stock_flags_read_in_one_query_and_respected(self, _enabled, _log):
        mapped = [_map("YES"), _map("NO"), _map("BLANK"), _map("DISABLED")]
        # DISABLED is absent: the Item query filters disabled=0.
        items = [
            {"name": "YES", "magento_send_stock": "Yes"},
            {"name": "NO", "magento_send_stock": "No"},
            {"name": "BLANK", "magento_send_stock": ""},
        ]
        m = _frappe_mock(mapped, items, send_stock=0)
        client = MagicMock()
        self._run(m, client)

        self.assertEqual([c.args[0] for c in client.update_stock.call_args_list], ["YES"])
        m.db.get_value.assert_not_called()
        item_calls = [c for c in m.get_all.call_args_list if c.args[0] == "Item"]
        self.assertEqual(len(item_calls), 1)
        self.assertIn("magento_send_stock", item_calls[0].kwargs["fields"])
        self.assertEqual(item_calls[0].kwargs["filters"]["disabled"], 0)

    def test_sql_uses_available_qty_and_configured_warehouses(self, _enabled, _log):
        m = _frappe_mock(
            [_map("A")], [{"name": "A", "magento_send_stock": None}],
            warehouses=["Stores - K", "Shop - K"],
        )
        self._run(m, MagicMock())
        self.assertEqual(m.db.sql.call_count, 1)
        query, params = m.db.sql.call_args.args
        self.assertIn("reserved_qty", query)
        self.assertIn("actual_qty", query)
        self.assertIn("b.warehouse IN %(warehouses)s", query)
        self.assertNotIn("is_group", query)
        self.assertEqual(params["warehouses"], ["Stores - K", "Shop - K"])
        self.assertEqual(params["item_codes"], ["A"])
        wh_call = [c for c in m.get_all.call_args_list if c.args[0] == "Magento Stock Warehouse"][0]
        self.assertEqual(
            wh_call.kwargs["filters"],
            {"parent": "Magento Settings", "parenttype": "Magento Settings"},
        )

    def test_no_warehouses_configured_uses_all_non_group(self, _enabled, _log):
        m = _frappe_mock([_map("A")], [{"name": "A", "magento_send_stock": None}])
        self._run(m, MagicMock())
        query = m.db.sql.call_args.args[0]
        self.assertIn("w.is_group = 0", query)
        self.assertNotIn("IN %(warehouses)s", query)

    def test_failure_is_isolated_and_not_recorded_as_pushed(self, _enabled, mock_log):
        from connector.api.magento_client import MagentoAPIError

        m = _frappe_mock(
            [_map("BAD"), _map("GOOD")],
            [{"name": "BAD", "magento_send_stock": None}, {"name": "GOOD", "magento_send_stock": None}],
            qty_rows=[{"item_code": "BAD", "available_qty": 1}, {"item_code": "GOOD", "available_qty": 2}],
        )
        client = MagicMock()
        client.update_stock.side_effect = lambda sku, qty: (
            (_ for _ in ()).throw(MagentoAPIError("boom", 500)) if sku == "BAD" else None
        )
        self._run(m, client)

        self.assertEqual([c.args[1] for c in m.db.set_value.call_args_list], ["GOOD"])
        failed_logs = [c for c in mock_log.call_args_list if c.kwargs.get("status") == "Failed"]
        self.assertEqual(len(failed_logs), 1)
        self.assertEqual(failed_logs[0].kwargs["document_name"], "BAD")

    def test_float_noise_does_not_trigger_push(self, _enabled, _log):
        from connector.sync.inventory_sync import _needs_push

        with patch("connector.sync.inventory_sync.frappe") as m:
            m.utils.get_datetime.side_effect = lambda v: v
            recent = NOW - datetime.timedelta(minutes=5)
            before = NOW - datetime.timedelta(hours=24)
            self.assertFalse(_needs_push(5.0, 5.0000000001, recent, before))
            self.assertTrue(_needs_push(5.0, 0.0, recent, before))
            self.assertTrue(_needs_push(0.0, None, recent, before))


class TestProductMapStockFields(unittest.TestCase):

    def test_doctype_has_read_only_stock_fields_in_collapsible_section(self):
        import json
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "connector", "doctype",
            "magento_product_map", "magento_product_map.json",
        )
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        fields = {f["fieldname"]: f for f in meta["fields"]}
        self.assertEqual(fields["stock_section"]["fieldtype"], "Section Break")
        self.assertEqual(fields["stock_section"].get("collapsible"), 1)
        self.assertEqual(fields["last_pushed_qty"]["fieldtype"], "Float")
        self.assertEqual(fields["last_stock_push_on"]["fieldtype"], "Datetime")
        self.assertEqual(fields["last_pushed_qty"].get("read_only"), 1)
        self.assertEqual(fields["last_stock_push_on"].get("read_only"), 1)
        order = meta["field_order"]
        self.assertLess(order.index("stock_section"), order.index("last_pushed_qty"))
        self.assertEqual(meta["modified"], "2026-11-13 15:00:00.000000")


class TestRemoteItemPrice(unittest.TestCase):

    def _client(self):
        from connector.api.erpnext_client import ERPNextClient

        client = ERPNextClient.__new__(ERPNextClient)
        client.site_name = "remote"
        client.base_url = "https://remote.example"
        return client

    def test_find_item_price_filters_selling_price_list(self):
        import json

        client = self._client()
        client.get = MagicMock(return_value={"data": [{"name": "IP-1", "price_list_rate": 10}]})
        self.assertEqual(client.find_item_price("A", "Retail"), {"name": "IP-1", "price_list_rate": 10})
        endpoint = client.get.call_args.args[0]
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(endpoint, "/api/resource/Item Price")
        filters = json.loads(params["filters"])
        self.assertIn(["item_code", "=", "A"], filters)
        self.assertIn(["price_list", "=", "Retail"], filters)
        self.assertIn(["selling", "=", 1], filters)
        self.assertEqual(json.loads(params["fields"]), ["name", "price_list_rate"])

    def test_upsert_creates_updates_or_skips(self):
        client = self._client()
        client.post = MagicMock(return_value={"data": {}})
        client.put = MagicMock(return_value={"data": {}})

        client.get = MagicMock(return_value={"data": []})
        self.assertEqual(client.upsert_item_price("A", "Retail", 12), "created")
        self.assertEqual(
            client.post.call_args.kwargs["data"],
            {"item_code": "A", "price_list": "Retail", "price_list_rate": 12.0, "selling": 1},
        )

        client.get = MagicMock(return_value={"data": [{"name": "IP 1", "price_list_rate": 10}]})
        self.assertEqual(client.upsert_item_price("A", "Retail", 12), "updated")
        self.assertEqual(client.put.call_args.args[0], "/api/resource/Item Price/IP%201")
        self.assertEqual(client.put.call_args.kwargs["data"], {"price_list_rate": 12.0})

        client.put.reset_mock()
        client.get = MagicMock(return_value={"data": [{"name": "IP-1", "price_list_rate": 12}]})
        self.assertEqual(client.upsert_item_price("A", "Retail", 12), "unchanged")
        client.put.assert_not_called()


@patch("connector.sync.erpnext_product_sync.create_log")
@patch("connector.sync.erpnext_product_sync.upsert_map")
@patch("connector.sync.erpnext_product_sync.get_remote_item_code", return_value=None)
@patch("connector.sync.erpnext_product_sync._is_erpnext_site_sync_enabled", return_value=True)
class TestPushItemPrice(unittest.TestCase):

    def _frappe(self, price):
        m = MagicMock()
        m.db.exists.return_value = True
        item = MagicMock()
        item.get.side_effect = lambda k, d=None: {"sync_to_erpnext_sites": 1, "barcodes": []}.get(k, d)
        item.item_code = "A"
        site = MagicMock(enable_sync=1, price_list="Retail")
        m.get_doc.side_effect = lambda dt, name: item if dt == "Item" else site
        m.db.get_value.return_value = price
        return m

    def _run(self, m, client):
        with patch("connector.sync.erpnext_product_sync.frappe", m), \
             patch("connector.sync.erpnext_product_sync.ERPNextClient", return_value=client):
            from connector.sync.erpnext_product_sync import push_item_to_site
            push_item_to_site("A", "remote")

    def test_price_upserted_after_successful_push(self, _en, _code, mock_map, mock_log):
        client = MagicMock()
        client.item_exists.return_value = True
        client.update_item.return_value = {"item_code": "A-R"}
        self._run(self._frappe(250), client)

        client.upsert_item_price.assert_called_once_with("A-R", "Retail", 250.0)
        self.assertEqual(mock_map.call_args.args[3], "Synced")

    def test_price_failure_logged_but_item_stays_synced(self, _en, _code, mock_map, mock_log):
        from connector.api.erpnext_client import ERPNextAPIError

        client = MagicMock()
        client.item_exists.return_value = False
        client.create_item.return_value = {"name": "A"}
        client.upsert_item_price.side_effect = ERPNextAPIError("Price List Retail not found", 417)
        self._run(self._frappe(99), client)

        self.assertEqual([c.args[3] for c in mock_map.call_args_list], ["Synced"])
        statuses = [c.kwargs["status"] for c in mock_log.call_args_list]
        self.assertEqual(sorted(statuses), ["Failed", "Success"])
        failed = [c for c in mock_log.call_args_list if c.kwargs["status"] == "Failed"][0]
        self.assertIn("Item Price", failed.kwargs["error_message"])
        self.assertIn("Price List with the same name", failed.kwargs["error_message"])

    def test_no_local_price_skips_price_call(self, _en, _code, _map, _log):
        client = MagicMock()
        client.item_exists.return_value = True
        client.update_item.return_value = {"item_code": "A"}
        self._run(self._frappe(None), client)
        client.upsert_item_price.assert_not_called()


class TestErpnextItemPriceHook(unittest.TestCase):

    @patch("connector.sync.erpnext_product_sync._is_erpnext_site_sync_enabled", return_value=True)
    @patch("connector.sync.erpnext_product_sync.frappe")
    def test_price_change_requeues_sites_using_that_price_list(self, m, _en):
        from connector.sync.erpnext_product_sync import on_item_price_change

        m.db.get_value.return_value = 1
        m.get_all.return_value = ["remote"]
        doc = MagicMock()
        doc.get.side_effect = {"selling": 1, "item_code": "A", "price_list": "Retail"}.get
        on_item_price_change(doc, "on_update")

        self.assertEqual(
            m.get_all.call_args.kwargs["filters"], {"enable_sync": 1, "price_list": "Retail"}
        )
        self.assertEqual(m.enqueue.call_args.kwargs["remote_site"], "remote")
        self.assertEqual(m.enqueue.call_args.kwargs["item_code"], "A")

    @patch("connector.sync.erpnext_product_sync._is_erpnext_site_sync_enabled", return_value=True)
    @patch("connector.sync.erpnext_product_sync.frappe")
    def test_buying_price_ignored(self, m, _en):
        from connector.sync.erpnext_product_sync import on_item_price_change

        doc = MagicMock()
        doc.get.side_effect = {"selling": 0, "item_code": "A", "price_list": "Buy"}.get
        on_item_price_change(doc, "on_update")
        m.enqueue.assert_not_called()


class TestCategoryPagination(unittest.TestCase):

    def _client(self):
        from connector.api.magento_client import MagentoClient

        client = MagentoClient.__new__(MagentoClient)
        client.CATEGORY_PAGE_SIZE = 2
        return client

    def test_walks_pages_until_short_page(self):
        client = self._client()
        pages = {
            1: {"items": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}], "total_count": 5},
            2: {"items": [{"id": 3, "name": "C"}, {"id": 4, "name": "D"}], "total_count": 5},
            3: {"items": [{"id": 5, "name": "E"}], "total_count": 5},
        }
        client.get = MagicMock(side_effect=lambda ep, params=None: pages[params["searchCriteria[currentPage]"]])
        self.assertEqual([c["id"] for c in client.get_categories()], [1, 2, 3, 4, 5])
        self.assertEqual(client.get.call_count, 3)

    def test_stops_when_magento_repeats_last_page(self):
        client = self._client()
        page = {"items": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}
        client.get = MagicMock(return_value=page)
        self.assertEqual([c["id"] for c in client.get_categories()], [1, 2])
        self.assertEqual(client.get.call_count, 2)

    def test_stops_at_total_count(self):
        client = self._client()
        page = {"items": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}], "total_count": 2}
        client.get = MagicMock(return_value=page)
        client.get_categories()
        self.assertEqual(client.get.call_count, 1)

    def test_falls_back_to_tree_when_list_fails(self):
        from connector.api.magento_client import MagentoAPIError

        client = self._client()

        def get(ep, params=None):
            if ep == "/categories/list":
                raise MagentoAPIError("nope", 404)
            return {"id": 1, "name": "Root", "children_data": [{"id": 2, "name": "Kids"}]}

        client.get = MagicMock(side_effect=get)
        self.assertEqual([c["id"] for c in client.get_categories()], [1, 2])


class TestPinSyncJobRemoved(unittest.TestCase):

    def test_scheduler_no_longer_pushes_to_github(self):
        import connector.hooks as hooks
        import connector.tasks as tasks

        jobs = [j for js in hooks.scheduler_events["cron"].values() for j in js]
        self.assertNotIn("connector.tasks.sync_frappe_pins_to_github", jobs)
        self.assertFalse(hasattr(tasks, "sync_frappe_pins_to_github"))
        self.assertFalse(hasattr(tasks, "_run_frappe_pin_sync"))

    def test_item_price_hooks_include_remote_site_push(self):
        import connector.hooks as hooks

        events = hooks.doc_events["Item Price"]
        for ev in ("after_insert", "on_update"):
            self.assertIn("connector.sync.erpnext_product_sync.on_item_price_change", events[ev])
            self.assertIn("connector.sync.product_sync.on_item_price_change", events[ev])


if __name__ == "__main__":
    unittest.main()
