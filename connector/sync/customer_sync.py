"""
Customer Sync: Magento Order → ERPNext Customer + Address

Called internally during order_sync.py — not scheduled independently.
Creates or matches ERPNext Customer records from Magento order data.
"""

import frappe
from frappe.utils import cstr


def _get_customer_group():
    """Return the default customer group, falling back gracefully."""
    # 1. Use Selling Settings default
    group = frappe.db.get_single_value("Selling Settings", "customer_group")
    if group and frappe.db.exists("Customer Group", group):
        return group
    # 2. Try common names
    for name in ("All Customer Groups", "All", "Commercial", "Individual"):
        if frappe.db.exists("Customer Group", name):
            return name
    # 3. Use the first available group
    first = frappe.db.get_value("Customer Group", {}, "name")
    return first or "All Customer Groups"


def _get_territory():
    """Return the default territory, falling back gracefully."""
    territory = frappe.db.get_single_value("Selling Settings", "territory")
    if territory and frappe.db.exists("Territory", territory):
        return territory
    for name in ("All Territories", "All", "Rest Of The World"):
        if frappe.db.exists("Territory", name):
            return name
    first = frappe.db.get_value("Territory", {}, "name")
    return first or "All Territories"


def get_or_create_customer(magento_order):
    """
    Find or create an ERPNext Customer from a Magento order dict.
    Returns the ERPNext Customer name (document name).
    """
    email = (magento_order.get("customer_email") or "").strip().lower()
    magento_customer_id = magento_order.get("customer_id")
    is_guest = bool(magento_order.get("customer_is_guest"))

    billing = magento_order.get("billing_address") or {}
    firstname = (billing.get("firstname") or magento_order.get("customer_firstname") or "").strip()
    lastname = (billing.get("lastname") or magento_order.get("customer_lastname") or "").strip()
    customer_name = f"{firstname} {lastname}".strip() or email or "Unknown Customer"

    existing = None

    if not is_guest and magento_customer_id:
        existing = frappe.db.get_value(
            "Customer",
            {"magento_customer_id": magento_customer_id},
            "name",
        )

    if not existing and email:
        existing = frappe.db.get_value(
            "Customer",
            {"email_id": email},
            "name",
        )

    if existing:
        if not is_guest and magento_customer_id:
            current_id = frappe.db.get_value("Customer", existing, "magento_customer_id")
            if not current_id:
                frappe.db.set_value("Customer", existing, "magento_customer_id", magento_customer_id)
                frappe.db.commit()
        return existing

    # Create new customer
    customer = frappe.new_doc("Customer")
    customer.customer_name = customer_name
    customer.customer_type = "Individual"
    customer.customer_group = _get_customer_group()
    customer.territory = _get_territory()

    if email:
        customer.email_id = email

    if not is_guest and magento_customer_id:
        customer.magento_customer_id = magento_customer_id

    customer.flags.ignore_permissions = True
    customer.insert()
    frappe.db.commit()
    return customer.name


def get_or_create_address(magento_order, customer_name):
    """
    Create or update the shipping address for a customer.
    Returns the ERPNext Address name, or None if address data is missing.
    """
    shipping_address = None
    ext = magento_order.get("extension_attributes") or {}
    assignments = ext.get("shipping_assignments") or []
    if assignments:
        shipping = (assignments[0] or {}).get("shipping") or {}
        shipping_address = shipping.get("address")

    addr_data = shipping_address or magento_order.get("billing_address") or {}

    if not addr_data:
        return None

    street = addr_data.get("street") or []
    if isinstance(street, list):
        address_line1 = street[0] if len(street) > 0 else ""
        address_line2 = street[1] if len(street) > 1 else ""
    else:
        address_line1 = cstr(street)
        address_line2 = ""

    city = addr_data.get("city") or ""
    state = addr_data.get("region") or addr_data.get("region_code") or ""
    pincode = addr_data.get("postcode") or ""
    country_code = addr_data.get("country_id") or "US"
    phone = addr_data.get("telephone") or ""

    country = _get_country_name(country_code)

    # Check if an address already exists for this customer
    existing_addr = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Customer",
            "link_name": customer_name,
            "parenttype": "Address",
        },
        "parent",
    )

    if existing_addr:
        try:
            frappe.db.set_value(
                "Address",
                existing_addr,
                {
                    "address_line1": address_line1 or "N/A",
                    "address_line2": address_line2,
                    "city": city or "N/A",
                    "state": state,
                    "pincode": pincode,
                    "country": country,
                    "phone": phone,
                    "address_type": "Shipping",
                },
            )
            frappe.db.commit()
        except Exception as e:
            frappe.logger("connector").warning(
                f"get_or_create_address: could not update existing address {existing_addr}: {e}"
            )
        return existing_addr

    # Create new address
    addr = frappe.new_doc("Address")
    addr.address_title = customer_name
    addr.address_type = "Shipping"
    addr.address_line1 = address_line1 or "N/A"
    addr.address_line2 = address_line2
    addr.city = city or "N/A"
    addr.state = state
    addr.pincode = pincode
    addr.country = country
    addr.phone = phone
    addr.append("links", {
        "link_doctype": "Customer",
        "link_name": customer_name,
    })
    addr.flags.ignore_permissions = True
    addr.insert()
    frappe.db.commit()
    return addr.name


def _get_country_name(country_code):
    """Convert ISO 2-letter country code to ERPNext country name."""
    if not country_code:
        return "United States"
    name = frappe.db.get_value("Country", {"code": country_code.lower()}, "name")
    return name or country_code
