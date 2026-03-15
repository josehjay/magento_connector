"""
Unit tests for MagentoClient.
Uses unittest.mock to avoid real HTTP calls.
Run with: bench run-tests --app connector
"""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock


class TestMagentoClient(unittest.TestCase):

    def _make_settings(self, **overrides):
        """Return a mock Magento Settings object."""
        settings = MagicMock()
        settings.magento_url = "https://magento.test"
        settings.magento_store_code = "default"
        settings.use_integration_token = True
        settings.get_password.return_value = "test_token_abc123"
        settings.cached_token = None
        settings.token_expiry = None
        settings.admin_username = "admin"
        for k, v in overrides.items():
            setattr(settings, k, v)
        return settings

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_get_product_calls_correct_url(self, mock_session_cls, mock_frappe):
        """GET /products/{sku} should call the correct endpoint."""
        mock_frappe.get_single.return_value = self._make_settings()

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"id": 42, "sku": "ITEM-001"}'
        mock_resp.json.return_value = {"id": 42, "sku": "ITEM-001"}
        mock_session.request.return_value = mock_resp

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        result = client.get_product("ITEM-001")

        self.assertEqual(result["id"], 42)
        call_args = mock_session.request.call_args
        self.assertIn("/products/ITEM-001", call_args[0][1])

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_update_stock_sends_correct_payload(self, mock_session_cls, mock_frappe):
        """update_stock should PUT with correct qty and is_in_stock values."""
        mock_frappe.get_single.return_value = self._make_settings()

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"qty": 10}'
        mock_resp.json.return_value = {"qty": 10}
        mock_session.request.return_value = mock_resp

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        client.update_stock("ITEM-001", 10)

        call_args = mock_session.request.call_args
        self.assertEqual(call_args[0][0], "PUT")
        payload = call_args[1]["json"]
        self.assertEqual(payload["stockItem"]["qty"], 10.0)
        self.assertTrue(payload["stockItem"]["is_in_stock"])

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_zero_stock_sets_out_of_stock(self, mock_session_cls, mock_frappe):
        """update_stock with qty=0 should set is_in_stock=False."""
        mock_frappe.get_single.return_value = self._make_settings()

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"qty": 0}'
        mock_resp.json.return_value = {"qty": 0}
        mock_session.request.return_value = mock_resp

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        client.update_stock("ITEM-001", 0)

        payload = mock_session.request.call_args[1]["json"]
        self.assertFalse(payload["stockItem"]["is_in_stock"])

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_get_orders_passes_updated_after_filter(self, mock_session_cls, mock_frappe):
        """get_orders should include updated_at filter when updated_after is provided."""
        mock_frappe.get_single.return_value = self._make_settings()

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"items": []}'
        mock_resp.json.return_value = {"items": []}
        mock_session.request.return_value = mock_resp

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        client.get_orders(updated_after="2024-01-01 00:00:00")

        call_args = mock_session.request.call_args
        params = call_args[1].get("params") or {}
        self.assertIn("searchCriteria[filterGroups][0][filters][0][value]", params)
        self.assertEqual(params["searchCriteria[filterGroups][0][filters][0][value]"], "2024-01-01 00:00:00")

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_processing_status_attempts_invoice_first(self, mock_session_cls, mock_frappe):
        """update_order_status('processing') should trigger invoice attempt first."""
        mock_frappe.get_single.return_value = self._make_settings()
        mock_frappe.logger.return_value = MagicMock()
        mock_session_cls.return_value = MagicMock()

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        client.ensure_invoice_for_processing = MagicMock(return_value=1234)
        client.post = MagicMock(return_value={"ok": True})

        client.update_order_status(order_id=7, status="processing", comment="test")

        client.ensure_invoice_for_processing.assert_called_once_with(7)

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_invoiceable_items_only_include_positive_qty(self, mock_session_cls, mock_frappe):
        """Invoice payload should only contain concrete rows with qty_to_invoice."""
        mock_frappe.get_single.return_value = self._make_settings()
        mock_session_cls.return_value = MagicMock()

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()

        order = {
            "items": [
                {"item_id": 10, "product_type": "configurable", "qty_to_invoice": 1},
                {"item_id": 11, "product_type": "simple", "qty_to_invoice": 0},
                {"item_id": 12, "product_type": "simple", "qty_to_invoice": 2},
                {"item_id": 13, "product_type": "simple", "qty_to_invoice": "1.5"},
            ]
        }

        rows = client._invoiceable_items_from_order(order)

        self.assertEqual(
            rows,
            [
                {"order_item_id": 12, "qty": 2.0},
                {"order_item_id": 13, "qty": 1.5},
            ],
        )

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def test_ensure_invoice_calls_create_invoice_when_needed(self, mock_session_cls, mock_frappe):
        """Invoice should be created when order still has invoiceable quantity."""
        mock_frappe.get_single.return_value = self._make_settings()
        mock_frappe.logger.return_value = MagicMock()
        mock_session_cls.return_value = MagicMock()

        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        client.get_order = MagicMock(
            return_value={
                "status": "pending",
                "items": [{"item_id": 22, "product_type": "simple", "qty_to_invoice": 1}],
            }
        )
        client.create_invoice = MagicMock(return_value=555)

        result = client.ensure_invoice_for_processing(7)

        self.assertEqual(result, 555)
        client.create_invoice.assert_called_once_with(7, items=[{"order_item_id": 22, "qty": 1.0}], capture=False, notify=False)


class TestMagentoAPIError(unittest.TestCase):

    def test_error_stores_status_code(self):
        from connector.api.magento_client import MagentoAPIError
        err = MagentoAPIError("Not found", status_code=404, response_body='{"message": "Not found"}')
        self.assertEqual(err.status_code, 404)
        self.assertIn("Not found", str(err))


if __name__ == "__main__":
    unittest.main()
