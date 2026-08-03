"""
Unit tests for Magento Sync Log creation + cleanup helpers.
"""

import unittest
from unittest.mock import MagicMock, patch


class TestCreateLogPayloadPolicy(unittest.TestCase):
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.frappe")
    def test_success_omits_payloads_by_default(self, mock_frappe):
        mock_frappe.db.get_single_value.side_effect = lambda *a, **k: {
            "success_log_retention_days": 7,
            "sync_log_retention_days": 30,
            "store_success_payloads": 0,
        }.get(a[1] if len(a) > 1 else None, 0)

        log_doc = MagicMock()
        mock_frappe.new_doc.return_value = log_doc

        from connector.connector.doctype.magento_sync_log.magento_sync_log import create_log

        create_log(
            operation="Product Push",
            status="Success",
            document_name="SKU-1",
            request_payload={"sku": "SKU-1", "name": "Big"},
            response_payload={"id": 1},
        )

        self.assertEqual(log_doc.request_payload, "")
        self.assertEqual(log_doc.response_payload, "")
        log_doc.insert.assert_called_once_with(ignore_permissions=True)

    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.frappe")
    def test_failed_keeps_truncated_payload(self, mock_frappe):
        mock_frappe.db.get_single_value.side_effect = lambda *a, **k: 0
        log_doc = MagicMock()
        mock_frappe.new_doc.return_value = log_doc

        from connector.connector.doctype.magento_sync_log.magento_sync_log import (
            MAX_PAYLOAD_CHARS,
            create_log,
        )

        huge = {"data": "x" * (MAX_PAYLOAD_CHARS + 500)}
        create_log(
            operation="Product Push",
            status="Failed",
            request_payload=huge,
            error_message="boom",
        )

        self.assertTrue(log_doc.request_payload)
        self.assertIn("truncated", log_doc.request_payload)
        self.assertLessEqual(len(log_doc.request_payload), MAX_PAYLOAD_CHARS)


class TestCleanupSyncLogs(unittest.TestCase):
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.scrub_success_payloads")
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log._delete_logs")
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.add_to_date")
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.now_datetime")
    @patch("connector.connector.doctype.magento_sync_log.magento_sync_log.frappe")
    def test_cleanup_calls_scrub_and_deletes(
        self, mock_frappe, mock_now, mock_add, mock_delete, mock_scrub
    ):
        mock_frappe.db.get_single_value.side_effect = lambda *a, **k: {
            "success_log_retention_days": 7,
            "sync_log_retention_days": 30,
            "store_success_payloads": 0,
        }.get(a[1] if len(a) > 1 else None)
        mock_now.return_value = "2026-08-03 00:00:00"
        mock_add.side_effect = lambda *a, **k: "cutoff"
        mock_scrub.return_value = 12
        mock_delete.side_effect = [5, 3]

        from connector.connector.doctype.magento_sync_log.magento_sync_log import (
            cleanup_sync_logs,
        )

        summary = cleanup_sync_logs()

        mock_scrub.assert_called_once()
        self.assertEqual(mock_delete.call_count, 2)
        self.assertEqual(summary["payloads_scrubbed"], 12)
        self.assertEqual(summary["deleted_success_or_skipped"], 5)
        self.assertEqual(summary["deleted_past_absolute_retention"], 3)
        self.assertEqual(summary["deleted_total"], 8)


if __name__ == "__main__":
    unittest.main()
