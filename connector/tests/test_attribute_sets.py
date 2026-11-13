"""
Tests for loading Magento attribute sets (Item Groups to Sync → attribute set)
and resolving them on Magento Settings save.
"""

import unittest
from unittest.mock import MagicMock, patch


def _settings():
    s = MagicMock()
    s.magento_url = "https://magento.test"
    s.magento_store_code = "default"
    s.use_integration_token = True
    s.get_password.return_value = "token"
    return s


def _resp(status, payload=None, text=None):
    r = MagicMock()
    r.status_code = status
    r.text = text if text is not None else ("x" if payload is not None else "")
    r.json.return_value = payload
    return r


class TestGetAttributeSets(unittest.TestCase):

    @patch("connector.api.magento_client.frappe")
    @patch("connector.api.magento_client.requests.Session")
    def _client(self, mock_session_cls, mock_frappe, responses):
        mock_frappe.get_single.return_value = _settings()
        session = MagicMock()
        session.request.side_effect = responses
        mock_session_cls.return_value = session
        from connector.api.magento_client import MagentoClient
        client = MagentoClient()
        return client, session

    def test_returns_sets_sorted_and_queries_global_scope_first(self):
        payload = {
            "items": [
                {"attribute_set_id": 9, "attribute_set_name": "Stationery"},
                {"attribute_set_id": 10, "attribute_set_name": "Books"},
            ],
            "total_count": 2,
        }
        client, session = self._client(responses=[_resp(200, payload)])

        sets = client.get_attribute_sets()

        self.assertEqual([s["attribute_set_name"] for s in sets], ["Books", "Stationery"])
        url = session.request.call_args[0][1]
        self.assertEqual(url, "https://magento.test/rest/V1/products/attribute-sets/sets/list")

    def test_acl_denial_is_raised_not_swallowed(self):
        from connector.api.magento_client import MagentoAPIError
        denied = _resp(401, text='{"message":"The consumer isn\'t authorized to access %resources."}')
        # 2 endpoints x 2 API bases, each answering 401.
        client, _ = self._client(responses=[denied] * 4)

        with self.assertRaises(MagentoAPIError) as ctx:
            client.get_attribute_sets()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_falls_back_to_eav_endpoint(self):
        not_found = _resp(404, text='{"message":"Request does not match any route."}')
        eav = _resp(200, {"items": [{"attribute_set_id": 4, "attribute_set_name": "Default"}]})
        client, session = self._client(responses=[not_found, eav])

        sets = client.get_attribute_sets()

        self.assertEqual(sets, [{"attribute_set_id": 4, "attribute_set_name": "Default"}])
        eav_params = session.request.call_args.kwargs["params"]
        self.assertEqual(
            eav_params["searchCriteria[filterGroups][0][filters][0][value]"], "catalog_product"
        )

    def test_paginates_until_short_page(self):
        from connector.api.magento_client import MagentoClient
        full_page = {
            "items": [
                {"attribute_set_id": i, "attribute_set_name": f"Set {i:03d}"}
                for i in range(1, MagentoClient.ATTRIBUTE_SET_PAGE_SIZE + 1)
            ],
            "total_count": MagentoClient.ATTRIBUTE_SET_PAGE_SIZE + 1,
        }
        last_page = {"items": [{"attribute_set_id": 999, "attribute_set_name": "Zz"}]}
        client, session = self._client(responses=[_resp(200, full_page), _resp(200, last_page)])

        sets = client.get_attribute_sets()

        self.assertEqual(len(sets), MagentoClient.ATTRIBUTE_SET_PAGE_SIZE + 1)
        self.assertEqual(session.request.call_count, 2)


class TestAttributeSetOptionsEndpoint(unittest.TestCase):

    @patch("connector.api.magento_options.require_doc_permission")
    @patch("connector.api.magento_options.frappe")
    @patch("connector.api.magento_options.MagentoClient")
    def test_acl_error_returns_not_ok_with_hint(self, mock_client_cls, _frappe, _perm):
        from connector.api.magento_client import MagentoAPIError
        from connector.api.magento_options import get_magento_attribute_sets

        mock_client_cls.return_value.get_attribute_sets.side_effect = MagentoAPIError(
            "denied", 401, "not authorized"
        )
        result = get_magento_attribute_sets()

        self.assertFalse(result["ok"])
        self.assertIn("Magento_Catalog::sets", result["error"])

    @patch("connector.api.magento_options.require_doc_permission")
    @patch("connector.api.magento_options.frappe")
    @patch("connector.api.magento_options.MagentoClient")
    def test_empty_list_is_reported_as_error(self, mock_client_cls, _frappe, _perm):
        from connector.api.magento_options import get_magento_attribute_sets

        mock_client_cls.return_value.get_attribute_sets.return_value = []
        result = get_magento_attribute_sets()

        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


def _row(idx, item_group, name="", set_id=None):
    row = MagicMock()
    row.idx = idx
    row.item_group = item_group
    row.attribute_set_name = name
    row.attribute_set_id = set_id
    return row


class TestMagentoSettingsItemGroupValidation(unittest.TestCase):

    def _settings_doc(self, rows):
        from connector.connector.doctype.magento_settings.magento_settings import MagentoSettings
        doc = MagentoSettings.__new__(MagentoSettings)
        doc.get = lambda key, default=None: rows if key == "magento_item_groups" else default
        return doc

    @patch("connector.connector.doctype.magento_settings.magento_settings.frappe")
    def test_duplicate_item_group_rejected(self, mock_frappe):
        mock_frappe.throw.side_effect = ValueError
        doc = self._settings_doc([_row(1, "Books"), _row(2, "Books")])
        with self.assertRaises(ValueError):
            doc._validate_item_groups()

    @patch("connector.api.magento_client.MagentoClient")
    @patch("connector.connector.doctype.magento_settings.magento_settings.frappe")
    def test_name_without_id_is_resolved_from_magento(self, mock_frappe, mock_client_cls):
        mock_client_cls.return_value.get_attribute_sets.return_value = [
            {"attribute_set_id": 10, "attribute_set_name": "Books"},
        ]
        row = _row(1, "Textbooks", name=" books ")
        doc = self._settings_doc([row])

        doc._validate_item_groups()

        self.assertEqual(row.attribute_set_id, 10)
        self.assertEqual(row.attribute_set_name, "Books")
        mock_frappe.msgprint.assert_not_called()

    @patch("connector.api.magento_client.MagentoClient")
    @patch("connector.connector.doctype.magento_settings.magento_settings.frappe")
    def test_unknown_name_warns_without_blocking_save(self, mock_frappe, mock_client_cls):
        mock_client_cls.return_value.get_attribute_sets.return_value = [
            {"attribute_set_id": 10, "attribute_set_name": "Books"},
        ]
        row = _row(1, "Toys", name="Toys")
        doc = self._settings_doc([row])

        doc._validate_item_groups()

        self.assertIsNone(row.attribute_set_id)
        mock_frappe.msgprint.assert_called_once()

    @patch("connector.api.magento_client.MagentoClient")
    @patch("connector.connector.doctype.magento_settings.magento_settings.frappe")
    def test_rows_with_ids_do_not_call_magento(self, mock_frappe, mock_client_cls):
        doc = self._settings_doc([_row(1, "Books", name="Books", set_id=10)])
        doc._validate_item_groups()
        mock_client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
