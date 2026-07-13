"""
Unit tests for attribute_sync.py
"""

import unittest
from unittest.mock import MagicMock, patch

from connector.api.magento_client import MagentoAPIError
from connector.sync.attribute_sync import _slugify_attribute_code


class TestSlugifyAttributeCode(unittest.TestCase):
    """Pure-logic tests for Magento attribute_code generation (no frappe dependency)."""

    def test_simple_name(self):
        self.assertEqual(_slugify_attribute_code("Size"), "size")

    def test_spaces_and_punctuation(self):
        self.assertEqual(_slugify_attribute_code("T-Shirt Size"), "t_shirt_size")

    def test_starts_with_digit_gets_prefixed(self):
        code = _slugify_attribute_code("2XL Size")
        self.assertTrue(code[0].isalpha())
        self.assertIn("2xl_size", code)

    def test_truncated_to_max_length(self):
        long_name = "A Very Very Long Attribute Name That Exceeds Thirty Characters"
        code = _slugify_attribute_code(long_name)
        self.assertLessEqual(len(code), 30)

    def test_blank_name_falls_back(self):
        self.assertEqual(_slugify_attribute_code("   "), "erp_attribute")


class TestSyncAttributeOptions(unittest.TestCase):
    """sync_attribute_options must only ADD missing options — never touch existing ones."""

    @patch("connector.sync.attribute_sync.create_log")
    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.MagentoClient")
    @patch("connector.sync.attribute_sync.frappe")
    def test_only_adds_missing_options(
        self, mock_frappe, mock_client_cls, mock_get_map, mock_upsert, mock_log
    ):
        mock_get_map.return_value = {"magento_attribute_code": "size"}
        # numeric_values lookup + Item Attribute Value lookup
        mock_frappe.db.get_value.return_value = 0  # not numeric
        mock_frappe.get_all.return_value = [
            {"attribute_value": "Small"},
            {"attribute_value": "Medium"},
            {"attribute_value": "Large"},
        ]

        mock_client = MagicMock()
        mock_client.get_attribute_options.return_value = [
            {"label": "Small", "value": "10"},
            {"label": "Medium", "value": "11"},
        ]
        mock_client_cls.return_value = mock_client

        from connector.sync.attribute_sync import sync_attribute_options
        result = sync_attribute_options("Size")

        mock_client.add_attribute_option.assert_called_once_with("size", "Large")
        self.assertEqual(result["added"], ["Large"])
        self.assertEqual(result["skipped_existing"], 2)
        self.assertEqual(result["failed"], 0)

    @patch("connector.sync.attribute_sync.create_log")
    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.MagentoClient")
    @patch("connector.sync.attribute_sync.frappe")
    def test_all_options_already_present_adds_nothing(
        self, mock_frappe, mock_client_cls, mock_get_map, mock_upsert, mock_log
    ):
        mock_get_map.return_value = {"magento_attribute_code": "size"}
        mock_frappe.db.get_value.return_value = 0
        mock_frappe.get_all.return_value = [
            {"attribute_value": "Small"},
            {"attribute_value": "Medium"},
        ]

        mock_client = MagicMock()
        mock_client.get_attribute_options.return_value = [
            {"label": "Small"},
            {"label": "Medium"},
            {"label": "Extra Large"},  # Magento-only option, must not be touched
        ]
        mock_client_cls.return_value = mock_client

        from connector.sync.attribute_sync import sync_attribute_options
        result = sync_attribute_options("Size")

        mock_client.add_attribute_option.assert_not_called()
        self.assertEqual(result["added"], [])
        self.assertEqual(result["skipped_existing"], 2)

    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.frappe")
    def test_unmapped_attribute_is_skipped(self, mock_frappe, mock_get_map, mock_upsert):
        mock_get_map.return_value = None

        from connector.sync.attribute_sync import sync_attribute_options
        result = sync_attribute_options("Size")

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "not_mapped")
        mock_upsert.assert_not_called()

    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.frappe")
    def test_numeric_attribute_is_skipped(self, mock_frappe, mock_get_map, mock_upsert):
        mock_get_map.return_value = {"magento_attribute_code": "weight"}
        mock_frappe.db.get_value.return_value = 1  # numeric_values = True

        from connector.sync.attribute_sync import sync_attribute_options
        result = sync_attribute_options("Weight")

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "numeric_attribute")


class TestCreateAttributeInMagento(unittest.TestCase):
    """create_attribute_in_magento must never create a duplicate attribute."""

    @patch("connector.sync.attribute_sync.create_log")
    @patch("connector.sync.attribute_sync.sync_attribute_options")
    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.MagentoClient")
    @patch("connector.sync.attribute_sync.frappe")
    def test_reuses_existing_attribute_instead_of_duplicating(
        self, mock_frappe, mock_client_cls, mock_get_map, mock_upsert, mock_sync_options, mock_log
    ):
        mock_frappe.get_roles.return_value = ["System Manager"]
        mock_frappe.db.exists.return_value = True
        mock_get_map.return_value = None  # not already mapped in ERPNext
        mock_frappe.db.get_value.return_value = 0  # not numeric
        mock_frappe.get_all.return_value = [{"attribute_value": "Red"}]

        mock_client = MagicMock()
        mock_client.get_attribute.return_value = {"attribute_id": 55, "default_frontend_label": "Color"}
        mock_client_cls.return_value = mock_client
        mock_sync_options.return_value = {"added": [], "skipped_existing": 1, "failed": 0}

        from connector.sync.attribute_sync import create_attribute_in_magento
        result = create_attribute_in_magento("Color")

        mock_client.create_attribute.assert_not_called()
        self.assertTrue(result.get("reused_existing"))
        self.assertEqual(result["magento_attribute_code"], "color")

    @patch("connector.sync.attribute_sync.create_log")
    @patch("connector.sync.attribute_sync.upsert_map")
    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.MagentoClient")
    @patch("connector.sync.attribute_sync.frappe")
    def test_creates_new_attribute_with_current_values_as_options(
        self, mock_frappe, mock_client_cls, mock_get_map, mock_upsert, mock_log
    ):
        mock_frappe.get_roles.return_value = ["System Manager"]
        mock_frappe.db.exists.return_value = True
        mock_get_map.return_value = None
        mock_frappe.db.get_value.return_value = 0
        mock_frappe.get_all.return_value = [
            {"attribute_value": "Red"},
            {"attribute_value": "Blue"},
        ]
        mock_frappe.get_single.return_value = MagicMock(magento_item_groups=[])

        mock_client = MagicMock()
        mock_client.get_attribute.side_effect = MagentoAPIError("not found", status_code=404)
        mock_client.create_attribute.return_value = {"attribute_id": 77}
        mock_client_cls.return_value = mock_client

        from connector.sync.attribute_sync import create_attribute_in_magento
        result = create_attribute_in_magento("Color")

        create_payload = mock_client.create_attribute.call_args.args[0]
        self.assertEqual(create_payload["attribute_code"], "color")
        self.assertEqual(create_payload["frontend_input"], "select")
        self.assertEqual(
            [o["label"] for o in create_payload["options"]],
            ["Red", "Blue"],
        )
        self.assertEqual(result["magento_attribute_code"], "color")

    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.frappe")
    def test_throws_if_already_mapped(self, mock_frappe, mock_get_map):
        mock_frappe.get_roles.return_value = ["System Manager"]
        mock_frappe.db.exists.return_value = True
        mock_get_map.return_value = {"magento_attribute_code": "color"}
        mock_frappe.throw.side_effect = Exception("already mapped")

        from connector.sync.attribute_sync import create_attribute_in_magento
        with self.assertRaises(Exception):
            create_attribute_in_magento("Color")


class TestOnItemAttributeSave(unittest.TestCase):
    """The real-time hook must only auto-sync attributes that are already mapped."""

    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.frappe")
    def test_skips_unmapped_attribute(self, mock_frappe, mock_get_map):
        mock_frappe.db.get_single_value.return_value = True
        mock_get_map.return_value = None
        doc = MagicMock()
        doc.get.return_value = "Size"

        from connector.sync.attribute_sync import on_item_attribute_save
        on_item_attribute_save(doc, "on_update")

        mock_frappe.enqueue.assert_not_called()

    @patch("connector.sync.attribute_sync.get_map")
    @patch("connector.sync.attribute_sync.frappe")
    def test_enqueues_sync_for_mapped_attribute(self, mock_frappe, mock_get_map):
        mock_frappe.db.get_single_value.return_value = True
        mock_get_map.return_value = {"magento_attribute_code": "size"}
        doc = MagicMock()
        doc.get.return_value = "Size"

        from connector.sync.attribute_sync import on_item_attribute_save
        on_item_attribute_save(doc, "on_update")

        mock_frappe.enqueue.assert_called_once()
        kwargs = mock_frappe.enqueue.call_args.kwargs
        self.assertEqual(kwargs.get("item_attribute"), "Size")
        self.assertTrue(kwargs.get("enqueue_after_commit"))


if __name__ == "__main__":
    unittest.main()
