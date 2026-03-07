"""
Unit tests for customer_sync.py
"""

import unittest
from unittest.mock import MagicMock, patch


SAMPLE_ORDER_GUEST = {
    "customer_email": "guest@example.com",
    "customer_firstname": "Jane",
    "customer_lastname": "Guest",
    "customer_is_guest": True,
    "customer_id": None,
    "billing_address": {
        "firstname": "Jane",
        "lastname": "Guest",
    },
    "extension_attributes": {},
}

SAMPLE_ORDER_REGISTERED = {
    "customer_email": "reg@example.com",
    "customer_firstname": "Bob",
    "customer_lastname": "Registered",
    "customer_is_guest": False,
    "customer_id": 99,
    "billing_address": {
        "firstname": "Bob",
        "lastname": "Registered",
    },
    "extension_attributes": {},
}


class TestCustomerSync(unittest.TestCase):

    @patch("connector.sync.customer_sync.frappe")
    def test_creates_guest_customer_by_email(self, mock_frappe):
        """Guest orders should create a customer using email as identifier."""
        mock_frappe.db.get_value.return_value = None
        mock_frappe.db.get_single_value.return_value = "All Customer Groups"

        mock_customer = MagicMock()
        mock_customer.name = "Jane Guest"
        mock_frappe.new_doc.return_value = mock_customer

        from connector.sync.customer_sync import get_or_create_customer
        name = get_or_create_customer(SAMPLE_ORDER_GUEST)

        mock_customer.insert.assert_called_once()
        self.assertEqual(name, mock_customer.name)

    @patch("connector.sync.customer_sync.frappe")
    def test_returns_existing_customer_by_magento_id(self, mock_frappe):
        """Registered customers should be matched by magento_customer_id first."""
        def mock_get_value(doctype, filters, fieldname):
            if doctype == "Customer" and filters.get("magento_customer_id") == 99:
                return "Bob Registered"
            return None

        mock_frappe.db.get_value.side_effect = mock_get_value

        from connector.sync.customer_sync import get_or_create_customer
        name = get_or_create_customer(SAMPLE_ORDER_REGISTERED)

        self.assertEqual(name, "Bob Registered")
        mock_frappe.new_doc.assert_not_called()

    @patch("connector.sync.customer_sync.frappe")
    def test_address_created_from_shipping(self, mock_frappe):
        """Address should be created from shipping_assignments if present."""
        order_with_shipping = {
            **SAMPLE_ORDER_GUEST,
            "extension_attributes": {
                "shipping_assignments": [{
                    "shipping": {
                        "address": {
                            "firstname": "Jane",
                            "lastname": "Guest",
                            "street": ["456 Oak Ave", "Suite 2"],
                            "city": "Springfield",
                            "region": "IL",
                            "postcode": "62701",
                            "country_id": "US",
                            "telephone": "555-1234",
                        }
                    }
                }]
            },
        }
        mock_frappe.db.get_value.return_value = None
        mock_frappe.db.get_single_value.return_value = None

        mock_addr = MagicMock()
        mock_addr.name = "Jane Guest-Shipping"
        mock_frappe.new_doc.return_value = mock_addr

        from connector.sync.customer_sync import get_or_create_address
        addr_name = get_or_create_address(order_with_shipping, "Jane Guest")

        mock_addr.insert.assert_called_once()
        self.assertEqual(mock_addr.address_line1, "456 Oak Ave")
        self.assertEqual(mock_addr.address_line2, "Suite 2")
        self.assertEqual(mock_addr.city, "Springfield")


if __name__ == "__main__":
    unittest.main()
