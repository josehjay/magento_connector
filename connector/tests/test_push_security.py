"""
Push endpoint gate (connector.security.permissions.require_push_endpoint_access)
when Connector Settings → push_endpoint_role is NOT configured: only System
Users (the Magento extension's API-key user) and Administrator may call the
inbound push endpoints; Guest and portal/website users are denied.
"""

import json
import unittest
from unittest.mock import MagicMock, patch


class _PermissionError(Exception):
    pass


def _raise(message=None, exc=Exception, *args, **kwargs):
    raise exc(message)


def _mock_frappe(user="magento@example.com", user_type="System User", roles=(), push_role=""):
    mock = MagicMock()
    mock.PermissionError = _PermissionError
    mock.throw.side_effect = _raise
    mock.session.user = user
    mock.get_roles.return_value = list(roles)
    mock.db.get_single_value.side_effect = (
        lambda doctype, field: push_role if field == "push_endpoint_role" else None
    )
    mock.db.get_value.side_effect = (
        lambda doctype, name, field=None, *a, **k: user_type if (doctype, field) == ("User", "user_type") else None
    )
    return mock


def _call_gate(frappe_mock):
    with patch("connector.security.permissions.frappe", frappe_mock), \
         patch("connector.security.permissions._record_push_role_diag") as diag:
        from connector.security.permissions import require_push_endpoint_access
        try:
            require_push_endpoint_access("receive_order")
            raised = None
        except _PermissionError as exc:
            raised = exc
    return raised, diag


class TestPushGateWithoutRole(unittest.TestCase):

    def test_system_user_allowed_with_warning(self):
        f = _mock_frappe(user_type="System User")
        raised, diag = _call_gate(f)
        self.assertIsNone(raised)
        self.assertEqual(diag.call_args[0][0], "not_configured")
        f.logger.return_value.warning.assert_called_once()

    def test_website_user_denied(self):
        f = _mock_frappe(user="customer@example.com", user_type="Website User")
        raised, diag = _call_gate(f)
        self.assertIsNotNone(raised)
        self.assertEqual(diag.call_args[0][0], "denied")
        self.assertIn("customer@example.com", diag.call_args[0][1])

    def test_unknown_user_type_denied(self):
        raised, diag = _call_gate(_mock_frappe(user_type=None))
        self.assertIsNotNone(raised)
        self.assertEqual(diag.call_args[0][0], "denied")

    def test_guest_denied_without_user_lookup(self):
        f = _mock_frappe(user="Guest", user_type="System User")
        raised, diag = _call_gate(f)
        self.assertIsNotNone(raised)
        self.assertEqual(diag.call_args[0][0], "denied")
        f.db.get_value.assert_not_called()

    def test_administrator_always_passes(self):
        f = _mock_frappe(user="Administrator", user_type=None)
        raised, diag = _call_gate(f)
        self.assertIsNone(raised)
        self.assertEqual(diag.call_args[0][0], "ok")

    def test_user_type_lookup_failure_denies(self):
        f = _mock_frappe()
        f.db.get_value.side_effect = RuntimeError("db down")
        raised, diag = _call_gate(f)
        self.assertIsNotNone(raised)
        self.assertEqual(diag.call_args[0][0], "denied")


class TestPushGateWithRole(unittest.TestCase):

    def test_administrator_passes_with_role_configured(self):
        raised, _ = _call_gate(_mock_frappe(user="Administrator", push_role="Magento Integration"))
        self.assertIsNone(raised)

    def test_role_holder_passes(self):
        raised, diag = _call_gate(_mock_frappe(roles=["Magento Integration"], push_role="Magento Integration"))
        self.assertIsNone(raised)
        self.assertEqual(diag.call_args[0][0], "ok")

    def test_system_user_without_role_denied(self):
        raised, diag = _call_gate(_mock_frappe(roles=["Sales User"], push_role="Magento Integration"))
        self.assertIsNotNone(raised)
        self.assertEqual(diag.call_args[0][0], "denied")


class TestEndpointsDenyWebsiteUsers(unittest.TestCase):
    """The three whitelisted push endpoints stop before any work for a portal user."""

    def _website_user(self):
        return _mock_frappe(user="customer@example.com", user_type="Website User")

    @patch("connector.sync.order_sync._process_order")
    @patch("connector.sync.order_sync.verify_incoming_signed_request")
    def test_receive_order(self, verify, process):
        with patch("connector.security.permissions.frappe", self._website_user()), \
             patch("connector.security.permissions._record_push_role_diag"):
            from connector.sync.order_sync import receive_order
            with self.assertRaises(_PermissionError):
                receive_order(json.dumps({"entity_id": 1, "increment_id": "1"}))
        verify.assert_not_called()
        process.assert_not_called()

    @patch("connector.sync.order_sync._sync_status_from_magento")
    @patch("connector.sync.order_sync.verify_incoming_signed_request")
    def test_receive_order_status(self, verify, status_sync):
        with patch("connector.security.permissions.frappe", self._website_user()), \
             patch("connector.security.permissions._record_push_role_diag"):
            from connector.sync.order_sync import receive_order_status
            with self.assertRaises(_PermissionError):
                receive_order_status(1, "complete")
        verify.assert_not_called()
        status_sync.assert_not_called()

    @patch("connector.sync.image_sync.verify_incoming_signed_request")
    def test_receive_image_update(self, verify):
        with patch("connector.security.permissions.frappe", self._website_user()), \
             patch("connector.security.permissions._record_push_role_diag"):
            from connector.sync.image_sync import receive_image_update
            with self.assertRaises(_PermissionError):
                receive_image_update("SKU-1", "https://shop.example.com/media/a.jpg")
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
