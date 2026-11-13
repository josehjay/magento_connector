"""
Order import: Magento discounts, totals check, one transaction per order and
lead_time_days handling (connector.sync.order_sync / customer_sync /
magento_order_map).
"""

import json
import unittest
from unittest.mock import MagicMock, call, patch


class FakeSalesOrder:
    """Just enough of a Sales Order to run _process_order. calculate_taxes_and_totals
    mimics ERPNext for 'Actual' tax rows and an additional discount on Net Total."""

    def __init__(self, events):
        self._events = events
        self.name = "SO-0001"
        self.items = []
        self.taxes = []
        self.flags = MagicMock()
        self.selling_price_list = "Standard Selling"
        self.company = None
        self.apply_discount_on = None
        self.additional_discount_percentage = None
        self.discount_amount = 0
        self.grand_total = 0
        self.remarks = ""
        self.discount_at_calculate = None

    def get(self, key, default=None):
        return getattr(self, key, default)

    def append(self, table, row):
        getattr(self, table).append(row)

    def run_method(self, method):
        if method == "calculate_taxes_and_totals":
            self.discount_at_calculate = (self.apply_discount_on, self.discount_amount)
            net = sum(r["rate"] * r["qty"] for r in self.items)
            if self.apply_discount_on == "Net Total":
                net -= self.discount_amount
            self.grand_total = round(net + sum(t["tax_amount"] for t in self.taxes), 2)

    def insert(self):
        self._events.append("insert")


BASE_ORDER = {
    "entity_id": 2001,
    "increment_id": "000002001",
    "status": "processing",
    "order_currency_code": "KES",
    "customer_email": "jane@example.com",
    "billing_address": {"firstname": "Jane", "lastname": "Buyer", "telephone": "0700"},
    "items": [{"sku": "BOOK-1", "name": "Book", "qty_ordered": 2, "price": 500, "product_type": "simple"}],
    "tax_amount": 144,
    "shipping_amount": 200,
    # 2 x 500 = 1000, coupon -100, tax 144, shipping 200 => 1244
    "discount_amount": -100,
    "discount_description": "BACK2SCHOOL",
    "grand_total": 1244,
}

ITEMS = [{"item_code": "BOOK-1", "qty": 2, "rate": 500}]
TAXES = [
    {"charge_type": "Actual", "account_head": "VAT", "description": "Magento Tax", "tax_amount": 144},
    {"charge_type": "Actual", "account_head": "Freight", "description": "Shipping", "tax_amount": 200},
]


class _ImportHarness(unittest.TestCase):
    """Patches every collaborator of _process_order and records write/commit order."""

    default_customer = ""

    def setUp(self):
        self.events = []
        self.so = FakeSalesOrder(self.events)
        ev = self.events

        def rec(name, ret=None):
            def _f(*a, **k):
                ev.append(name)
                return ret
            return _f

        p = lambda target, **kw: self._start(patch(target, **kw))
        self.frappe = p("connector.sync.order_sync.frappe")
        self.frappe.new_doc.return_value = self.so
        self.frappe.db.commit.side_effect = rec("commit")
        self.frappe.db.get_value.return_value = None
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: self.default_customer if k == "default_customer" else d
        settings.lead_time_days = 3
        self.settings = settings
        self.frappe.get_single.return_value = settings

        p("connector.sync.order_sync.is_order_imported", return_value=False)
        self.items = p("connector.sync.order_sync._build_order_items", return_value=[dict(r) for r in ITEMS])
        p("connector.sync.order_sync._build_taxes_and_charges", return_value=[dict(t) for t in TAXES])
        p("connector.sync.order_sync._get_default_company", return_value="Kitabu")
        p("connector.sync.order_sync._get_valid_currency", return_value="KES")
        p("connector.sync.order_sync._apply_default_warehouse")
        self.customer = p("connector.sync.order_sync.get_or_create_customer", side_effect=rec("customer", "Jane Buyer"))
        self.address = p("connector.sync.order_sync.get_or_create_address", side_effect=rec("address", "ADDR-1"))
        self.create_map = p("connector.sync.order_sync.create_map", side_effect=rec("create_map"))
        self.create_log = p("connector.sync.order_sync.create_log", side_effect=rec("create_log"))

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def run_import(self, order=None):
        from connector.sync.order_sync import _process_order
        return _process_order(dict(order or BASE_ORDER), None)


class TestDiscountImport(_ImportHarness):

    def test_coupon_applied_as_net_total_discount_before_totals(self):
        result = self.run_import()

        self.assertEqual(result["status"], "imported")
        self.assertEqual(self.so.apply_discount_on, "Net Total")
        self.assertEqual(self.so.additional_discount_percentage, 0)
        self.assertEqual(self.so.discount_amount, 100.0)
        # Set before ERPNext computes totals.
        self.assertEqual(self.so.discount_at_calculate, ("Net Total", 100.0))
        self.assertIn("BACK2SCHOOL", self.so.remarks)
        # Actual tax rows + Net Total discount reproduce Magento's grand total.
        self.assertEqual(self.so.grand_total, 1244)
        self.assertNotIn("Totals differ", self.so.remarks)
        log_kwargs = self.create_log.call_args.kwargs
        self.assertEqual(log_kwargs["status"], "Success")
        self.assertIsNone(log_kwargs["error_message"])

    def test_no_discount_leaves_discount_fields_unset(self):
        order = {**BASE_ORDER, "discount_amount": 0, "grand_total": 1344}
        order.pop("discount_description")
        self.run_import(order)
        self.assertIsNone(self.so.apply_discount_on)
        self.assertEqual(self.so.discount_amount, 0)
        self.assertNotIn("Discount", self.so.remarks)

    def test_missing_description_uses_generic_label(self):
        order = {**BASE_ORDER, "discount_description": None}
        self.run_import(order)
        self.assertIn("Magento discount", self.so.remarks)

    def test_totals_mismatch_is_logged_not_fatal(self):
        order = {**BASE_ORDER, "grand_total": 1240}  # e.g. Magento rounding / unsupported fee
        result = self.run_import(order)

        self.assertEqual(result["status"], "imported")
        self.assertIn("Totals differ from Magento by +4.00", self.so.remarks)
        log_kwargs = self.create_log.call_args.kwargs
        self.assertEqual(log_kwargs["status"], "Success")
        self.assertIn("Totals differ from Magento by +4.00", log_kwargs["error_message"])
        self.frappe.logger.return_value.warning.assert_called()

    def test_difference_within_a_cent_is_ignored(self):
        self.run_import({**BASE_ORDER, "grand_total": 1244.01})
        self.assertNotIn("Totals differ", self.so.remarks)

    def test_order_without_grand_total_is_not_checked(self):
        order = dict(BASE_ORDER)
        order.pop("grand_total")
        self.run_import(order)
        self.assertNotIn("Totals differ", self.so.remarks)


class TestSingleTransaction(_ImportHarness):

    def test_helpers_called_without_commit_and_one_commit_after_map(self):
        self.run_import()

        self.assertEqual(self.customer.call_args.kwargs.get("commit"), False)
        for c in self.address.call_args_list:
            self.assertEqual(c.kwargs.get("commit"), False)
        self.assertEqual(self.create_map.call_args.kwargs.get("commit"), False)
        self.assertEqual(
            self.events,
            ["customer", "address", "address", "insert", "create_map", "commit", "create_log"],
        )

    def test_insert_failure_commits_nothing(self):
        def boom():
            raise ValueError("validation failed")
        self.so.insert = boom
        with self.assertRaises(RuntimeError):
            self.run_import()
        self.assertNotIn("commit", self.events)
        self.assertNotIn("create_log", self.events)
        self.create_map.assert_not_called()

    def test_map_failure_commits_nothing(self):
        self.create_map.side_effect = Exception("Duplicate entry")
        with self.assertRaises(Exception):
            self.run_import()
        self.assertNotIn("commit", self.events)

    def test_no_matching_items_skips_before_any_write(self):
        self.items.return_value = []
        result = self.run_import()
        self.assertEqual(result["status"], "skipped")
        self.customer.assert_not_called()
        self.address.assert_not_called()
        self.frappe.new_doc.assert_not_called()

    def test_address_failure_rolls_back_to_savepoint(self):
        self.address.side_effect = Exception("bad address")
        result = self.run_import()
        self.assertEqual(result["status"], "imported")
        self.frappe.db.savepoint.assert_any_call("magento_order_address")
        self.frappe.db.rollback.assert_called_once_with(save_point="magento_order_address")
        self.assertFalse(hasattr(self.so, "shipping_address_name"))
        self.assertEqual(self.events.count("commit"), 1)

    def test_customer_failure_without_default_customer_raises(self):
        self.customer.side_effect = Exception("no customer group")
        with self.assertRaises(RuntimeError):
            self.run_import()
        self.frappe.db.rollback.assert_called_once_with(save_point="magento_order_customer")
        self.assertNotIn("commit", self.events)


class TestCustomerFallbackTransaction(_ImportHarness):
    default_customer = "Web Sales"

    def test_customer_failure_falls_back_after_savepoint_rollback(self):
        self.customer.side_effect = Exception("duplicate email")
        result = self.run_import()
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["customer"], "Web Sales")
        self.frappe.db.rollback.assert_called_once_with(save_point="magento_order_customer")
        self.assertEqual(self.events.count("commit"), 1)


class TestCallersRollBack(unittest.TestCase):

    @patch("connector.sync.order_sync.create_log")
    @patch("connector.sync.order_sync._process_order", side_effect=RuntimeError("insert failed"))
    @patch("connector.sync.order_sync._sync_disabled_reason", return_value=None)
    @patch("connector.sync.order_sync.verify_incoming_signed_request")
    @patch("connector.sync.order_sync.require_push_endpoint_access")
    @patch("connector.sync.order_sync.frappe")
    def test_receive_order_rolls_back_before_logging(self, mock_frappe, _gate, _verify, _dis, _proc, mock_log):
        tracker = MagicMock()
        tracker.attach_mock(mock_frappe.db.rollback, "rollback")
        tracker.attach_mock(mock_log, "create_log")
        from connector.sync.order_sync import receive_order
        result = receive_order(json.dumps(BASE_ORDER))
        self.assertFalse(result["ok"])
        self.assertEqual([c[0] for c in tracker.mock_calls][:2], ["rollback", "create_log"])

    @patch("connector.sync.order_sync.create_log")
    @patch("connector.sync.order_sync._process_order", side_effect=RuntimeError("insert failed"))
    @patch("connector.sync.order_sync.MagentoClient")
    @patch("connector.sync.order_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.order_sync.frappe")
    def test_sync_orders_rolls_back_failed_order(self, mock_frappe, _en, client_cls, _proc, mock_log):
        client_cls.return_value.get_all_new_orders.return_value = [dict(BASE_ORDER, updated_at="2026-11-13 10:00:00")]
        mock_frappe.get_single.return_value.last_order_sync_time = "2026-11-12 00:00:00"
        tracker = MagicMock()
        tracker.attach_mock(mock_frappe.db.rollback, "rollback")
        tracker.attach_mock(mock_log, "create_log")
        from connector.sync.order_sync import sync_orders
        summary = sync_orders()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual([c[0] for c in tracker.mock_calls][:2], ["rollback", "create_log"])


class TestStatusSyncTransaction(unittest.TestCase):

    @patch("connector.sync.order_sync.update_status")
    @patch("connector.sync.order_sync.get_sales_order_for_magento_order", return_value="SO-1")
    @patch("connector.sync.order_sync.frappe")
    def test_map_and_sales_order_status_commit_together(self, mock_frappe, _so, mock_update):
        from connector.sync.order_sync import _sync_status_from_magento
        _sync_status_from_magento(2001, "complete")
        mock_update.assert_called_once_with(2001, "complete", commit=False)
        mock_frappe.db.set_value.assert_called_once_with("Sales Order", "SO-1", "magento_order_status", "complete")
        mock_frappe.db.commit.assert_called_once()


class TestLeadTime(unittest.TestCase):

    def test_explicit_zero_is_respected(self):
        from connector.sync.order_sync import _lead_time_days
        self.assertEqual(_lead_time_days(0), 0)
        self.assertEqual(_lead_time_days("0"), 0)

    def test_unset_defaults_to_three(self):
        from connector.sync.order_sync import _lead_time_days
        self.assertEqual(_lead_time_days(None), 3)
        self.assertEqual(_lead_time_days(""), 3)
        self.assertEqual(_lead_time_days("abc"), 3)
        self.assertEqual(_lead_time_days(7), 7)

    @patch("connector.sync.order_sync.add_days", side_effect=lambda d, n: f"{d}+{n}")
    @patch("connector.sync.order_sync.nowdate", return_value="2026-11-13")
    @patch("connector.sync.order_sync.frappe")
    def test_build_order_items_uses_zero_lead_time(self, mock_frappe, _now, _add):
        mock_frappe.db.get_single_value.return_value = 0
        mock_frappe.db.exists.return_value = True
        mock_frappe.db.get_value.return_value = "Nos"
        from connector.sync.order_sync import _build_order_items
        items = _build_order_items(BASE_ORDER)
        self.assertEqual(items[0]["delivery_date"], "2026-11-13+0")

    def test_process_order_uses_zero_lead_time(self):
        harness = _ImportHarness("run_import")
        harness.setUp()
        try:
            harness.settings.lead_time_days = 0
            with patch("connector.sync.order_sync.add_days", side_effect=lambda d, n: n):
                harness.run_import()
            self.assertEqual(harness.so.delivery_date, 0)
        finally:
            harness.doCleanups()


class TestCustomerSyncCommitFlag(unittest.TestCase):

    ORDER = {
        "customer_email": "jane@example.com",
        "customer_is_guest": True,
        "increment_id": "2001",
        "billing_address": {"firstname": "Jane", "lastname": "Buyer", "street": ["1 Road"],
                            "city": "Nairobi", "country_id": "KE"},
    }

    @patch("connector.sync.customer_sync._get_territory", return_value="Kenya")
    @patch("connector.sync.customer_sync._get_customer_group", return_value="Individual")
    @patch("connector.sync.customer_sync.frappe")
    def test_new_customer_commit_flag(self, mock_frappe, _g, _t):
        mock_frappe.db.get_value.return_value = None
        from connector.sync.customer_sync import get_or_create_customer
        get_or_create_customer(self.ORDER, commit=False)
        mock_frappe.db.commit.assert_not_called()
        get_or_create_customer(self.ORDER)
        mock_frappe.db.commit.assert_called_once()

    @patch("connector.sync.customer_sync.frappe")
    def test_existing_customer_id_backfill_commit_flag(self, mock_frappe):
        order = {"customer_email": "a@b.c", "customer_id": 9, "customer_is_guest": False}
        mock_frappe.db.get_value.side_effect = [None, "CUST-1", None]
        from connector.sync.customer_sync import get_or_create_customer
        self.assertEqual(get_or_create_customer(order, commit=False), "CUST-1")
        mock_frappe.db.set_value.assert_called_once()
        mock_frappe.db.commit.assert_not_called()

    @patch("connector.sync.customer_sync._get_country_name", return_value="Kenya")
    @patch("connector.sync.customer_sync.frappe")
    def test_new_address_commit_flag(self, mock_frappe, _c):
        from connector.sync.customer_sync import get_or_create_address
        get_or_create_address(self.ORDER, "Jane", also_link_to="Web Sales", always_create_new=True, commit=False)
        mock_frappe.db.commit.assert_not_called()
        get_or_create_address(self.ORDER, "Jane", always_create_new=True)
        mock_frappe.db.commit.assert_called_once()

    @patch("connector.sync.customer_sync._get_country_name", return_value="Kenya")
    @patch("connector.sync.customer_sync.frappe")
    def test_existing_address_update_and_link_commit_flag(self, mock_frappe, _c):
        mock_frappe.get_all.return_value = ["ADDR-1"]
        addr = MagicMock()
        addr.get.return_value = []
        mock_frappe.get_doc.return_value = addr
        from connector.sync.customer_sync import get_or_create_address
        name = get_or_create_address(self.ORDER, "Jane", also_link_to="Web Sales", commit=False)
        self.assertEqual(name, "ADDR-1")
        addr.save.assert_called_once()
        mock_frappe.db.commit.assert_not_called()


class TestOrderMapCommitFlag(unittest.TestCase):

    @patch("connector.connector.doctype.magento_order_map.magento_order_map.frappe")
    def test_create_map_commit_flag(self, mock_frappe):
        from connector.connector.doctype.magento_order_map.magento_order_map import create_map
        create_map(1, "1", "pending", "SO-1", commit=False)
        mock_frappe.new_doc.return_value.insert.assert_called_once_with(ignore_permissions=True)
        mock_frappe.db.commit.assert_not_called()
        create_map(2, "2", "pending", "SO-2")
        mock_frappe.db.commit.assert_called_once()

    @patch("connector.connector.doctype.magento_order_map.magento_order_map.frappe")
    def test_update_status_commit_flag(self, mock_frappe):
        mock_frappe.db.get_value.return_value = "MAP-1"
        from connector.connector.doctype.magento_order_map.magento_order_map import update_status
        update_status(1, "complete", commit=False)
        mock_frappe.db.commit.assert_not_called()
        update_status(1, "complete")
        mock_frappe.db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
