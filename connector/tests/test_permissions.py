"""
Unit tests for connector.security.permissions and the guards applied to the
whitelisted endpoints (receive_order, get_magento_payment_details, Item-form
push buttons).
"""

import json
import unittest
from unittest.mock import MagicMock, patch


class _PermissionError(Exception):
    pass


class _DoesNotExistError(Exception):
    pass


def _raise(message=None, exc=Exception, *args, **kwargs):
    raise exc(message)


def _mock_frappe(user="test@example.com", roles=(), push_role=""):
    """frappe MagicMock whose throw() raises and whose role lookups are real."""
    mock = MagicMock()
    mock.PermissionError = _PermissionError
    mock.DoesNotExistError = _DoesNotExistError
    mock.throw.side_effect = _raise
    mock.session.user = user
    mock.get_roles.return_value = list(roles)
    mock.db.get_single_value.side_effect = (
        lambda doctype, field: push_role if field == "push_endpoint_role" else None
    )
    return mock


class TestRoleHelpers(unittest.TestCase):

    def test_administrator_always_passes(self):
        with patch("connector.security.permissions.frappe", _mock_frappe(user="Administrator")) as f:
            from connector.security.permissions import require_roles
            require_roles("System Manager")
            f.get_roles.assert_not_called()

    def test_user_with_role_passes(self):
        with patch("connector.security.permissions.frappe", _mock_frappe(roles=["System Manager"])):
            from connector.security.permissions import require_admin
            require_admin()

    def test_user_without_role_is_denied(self):
        with patch("connector.security.permissions.frappe", _mock_frappe(roles=["Stock User"])):
            from connector.security.permissions import require_admin
            with self.assertRaises(_PermissionError):
                require_admin()

    def test_no_roles_given_denies_non_admin(self):
        with patch("connector.security.permissions.frappe", _mock_frappe(roles=["System Manager"])):
            from connector.security.permissions import has_any_role
            self.assertFalse(has_any_role())

    def test_require_doc_permission_delegates_with_throw(self):
        with patch("connector.security.permissions.frappe", _mock_frappe()) as f:
            from connector.security.permissions import require_doc_permission
            require_doc_permission("Sales Invoice", "read", doc="SINV-1")
            f.has_permission.assert_called_once_with("Sales Invoice", "read", doc="SINV-1", throw=True)


class TestPushEndpointGate(unittest.TestCase):

    def test_unset_role_allows_and_warns(self):
        f = _mock_frappe(roles=["Guest-ish"], push_role="")
        with patch("connector.security.permissions.frappe", f), \
             patch("connector.security.permissions._record_push_role_diag") as diag:
            from connector.security.permissions import require_push_endpoint_access
            require_push_endpoint_access("receive_order")
        f.logger.return_value.warning.assert_called_once()
        self.assertEqual(diag.call_args[0][0], "not_configured")

    def test_configured_role_denies_user_without_it(self):
        f = _mock_frappe(roles=["Sales User"], push_role="Magento Integration")
        with patch("connector.security.permissions.frappe", f), \
             patch("connector.security.permissions._record_push_role_diag") as diag:
            from connector.security.permissions import require_push_endpoint_access
            with self.assertRaises(_PermissionError):
                require_push_endpoint_access("receive_order")
        self.assertEqual(diag.call_args[0][0], "denied")

    def test_configured_role_allows_holder(self):
        f = _mock_frappe(roles=["Magento Integration"], push_role="Magento Integration")
        with patch("connector.security.permissions.frappe", f), \
             patch("connector.security.permissions._record_push_role_diag"):
            from connector.security.permissions import require_push_endpoint_access
            require_push_endpoint_access("receive_order")

    def test_configured_role_allows_system_manager(self):
        f = _mock_frappe(roles=["System Manager"], push_role="Magento Integration")
        with patch("connector.security.permissions.frappe", f), \
             patch("connector.security.permissions._record_push_role_diag"):
            from connector.security.permissions import require_push_endpoint_access
            require_push_endpoint_access("receive_order")


PUSHED_ORDER = {"entity_id": 1001, "increment_id": "000001001", "status": "processing", "grand_total": 1}


@patch("connector.sync.order_sync.create_log")
@patch("connector.sync.order_sync._sync_disabled_reason", return_value=None)
@patch("connector.sync.order_sync._process_order", return_value={"status": "imported", "sales_order": "SO-1"})
@patch("connector.sync.order_sync.frappe")
class TestReceiveOrderGate(unittest.TestCase):

    def test_denied_when_role_configured_and_missing(self, mock_frappe, mock_process, _dis, _log):
        perm_frappe = _mock_frappe(roles=["Sales User"], push_role="Magento Integration")
        with patch("connector.security.permissions.frappe", perm_frappe), \
             patch("connector.security.permissions._record_push_role_diag"), \
             patch("connector.sync.order_sync.verify_incoming_signed_request") as verify:
            from connector.sync.order_sync import receive_order
            with self.assertRaises(_PermissionError):
                receive_order(json.dumps(PUSHED_ORDER))
        verify.assert_not_called()
        mock_process.assert_not_called()

    def test_allowed_holder_processes_pushed_body(self, mock_frappe, mock_process, _dis, _log):
        perm_frappe = _mock_frappe(roles=["Magento Integration"], push_role="Magento Integration")
        with patch("connector.security.permissions.frappe", perm_frappe),              patch("connector.security.permissions._record_push_role_diag"),              patch("connector.sync.order_sync.verify_incoming_signed_request", return_value=True):
            from connector.sync.order_sync import receive_order
            result = receive_order(json.dumps(PUSHED_ORDER))
        self.assertTrue(result["ok"])
        self.assertEqual(mock_process.call_args[0][0], PUSHED_ORDER)

    def test_unset_role_still_processes_push(self, mock_frappe, mock_process, _dis, _log):
        perm_frappe = _mock_frappe(roles=["Sales User"], push_role="")
        with patch("connector.security.permissions.frappe", perm_frappe),              patch("connector.security.permissions._record_push_role_diag"),              patch("connector.sync.order_sync.verify_incoming_signed_request", return_value=True):
            from connector.sync.order_sync import receive_order
            result = receive_order(json.dumps(PUSHED_ORDER))
        self.assertTrue(result["ok"])
        mock_process.assert_called_once()


class TestPaymentDetailsPermission(unittest.TestCase):

    @patch("connector.sync.payment_sync.MagentoClient")
    @patch("connector.sync.payment_sync.frappe")
    def test_denied_without_sales_invoice_read(self, mock_frappe, client_cls):
        perm_frappe = _mock_frappe()
        perm_frappe.has_permission.side_effect = _PermissionError("no read")
        with patch("connector.security.permissions.frappe", perm_frappe):
            from connector.sync.payment_sync import get_magento_payment_details
            with self.assertRaises(_PermissionError):
                get_magento_payment_details("SINV-0001")
        perm_frappe.has_permission.assert_called_once_with(
            "Sales Invoice", "read", doc="SINV-0001", throw=True
        )
        mock_frappe.get_doc.assert_not_called()
        client_cls.assert_not_called()

    @patch("connector.sync.payment_sync.MagentoClient")
    @patch("connector.sync.payment_sync.frappe")
    def test_denied_without_linked_sales_order_read(self, mock_frappe, client_cls):
        si = MagicMock()
        si.items = [MagicMock(sales_order="SO-0001")]
        mock_frappe.get_doc.return_value = si

        def has_permission(doctype, ptype, doc=None, throw=False):
            if doctype == "Sales Order":
                raise _PermissionError("no SO read")
            return True

        perm_frappe = _mock_frappe()
        perm_frappe.has_permission.side_effect = has_permission
        with patch("connector.security.permissions.frappe", perm_frappe):
            from connector.sync.payment_sync import get_magento_payment_details
            with self.assertRaises(_PermissionError):
                get_magento_payment_details("SINV-0001")
        mock_frappe.get_doc.assert_called_once_with("Sales Invoice", "SINV-0001")
        client_cls.assert_not_called()


class TestItemPushGuards(unittest.TestCase):

    @patch("connector.sync.product_sync._push_item_to_magento", return_value={"ok": True})
    def test_push_button_requires_item_write(self, mock_internal):
        perm_frappe = _mock_frappe()
        perm_frappe.has_permission.side_effect = _PermissionError("no write")
        with patch("connector.security.permissions.frappe", perm_frappe):
            from connector.sync.product_sync import push_item_to_magento
            with self.assertRaises(_PermissionError):
                push_item_to_magento("TEST-SKU-001")
        mock_internal.assert_not_called()

    @patch("connector.sync.product_sync._push_item_to_magento", return_value={"ok": True})
    def test_push_button_calls_internal_when_permitted(self, mock_internal):
        perm_frappe = _mock_frappe()
        perm_frappe.db.exists.return_value = True
        with patch("connector.security.permissions.frappe", perm_frappe):
            from connector.sync.product_sync import push_item_to_magento
            self.assertEqual(push_item_to_magento("TEST-SKU-001"), {"ok": True})
        mock_internal.assert_called_once_with("TEST-SKU-001")
        perm_frappe.has_permission.assert_any_call("Item", "write", doc="TEST-SKU-001", throw=True)

    @patch("connector.sync.product_sync.frappe")
    def test_background_push_job_targets_unguarded_function(self, mock_frappe):
        from connector.sync.product_sync import _enqueue_product_sync
        _enqueue_product_sync("TEST-SKU-001", "push")
        self.assertEqual(
            mock_frappe.enqueue.call_args[0][0],
            "connector.sync.product_sync._push_item_to_magento",
        )


if __name__ == "__main__":
    unittest.main()
