app_name = "connector"
app_title = "Connector"
app_publisher = "Bookspot"
app_description = "ERPNext integration connector — Magento, multi-site ERPNext sync, and more"
app_email = "info@bookspot.co.ke"
app_license = "MIT"

# Fixtures — exported/imported to set up custom fields in target environments
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["name", "in", [
                # Item custom fields — Magento tab
                "Item-magento_tab",
                "Item-magento_send_stock",
                "Item-magento_product_id",
                "Item-sync_to_magento",
                "Item-magento_last_synced_on",
                "Item-magento_sync_error",
                "Item-magento_section_break",
                # Item custom fields — ERPNext site sync
                "Item-erpnext_sync_section_break",
                "Item-sync_to_erpnext_sites",
                # Sales Order custom fields
                "Sales Order-magento_order_id",
                "Sales Order-magento_increment_id",
                "Sales Order-magento_order_status",
                "Sales Order-magento_so_section_break",
                # Customer custom fields
                "Customer-magento_customer_id",
                "Customer-magento_customer_section_break",
            ]]
        ]
    }
]

# Document event hooks
doc_events = {
    "Item": {
        "after_insert": [
            "connector.sync.product_sync.on_item_save",
            "connector.sync.erpnext_product_sync.on_item_save",
        ],
        "on_update": [
            "connector.sync.product_sync.on_item_save",
            "connector.sync.erpnext_product_sync.on_item_save",
        ],
        "on_trash": [
            "connector.sync.product_sync.on_item_trash",
        ],
    },
    "Item Price": {
        "after_insert": "connector.sync.product_sync.on_item_price_change",
        "on_update": "connector.sync.product_sync.on_item_price_change",
        "on_trash": "connector.sync.product_sync.on_item_price_change",
    },
    # Only acts on Item Attributes that are already mapped to Magento — pushes
    # newly-added values as new Magento options (additive only, never deletes).
    "Item Attribute": {
        "on_update": "connector.sync.attribute_sync.on_item_attribute_save",
    },
    "Sales Order": {
        # on_submit → Magento "processing" (order confirmed in ERP)
        "on_submit": "connector.sync.status_sync.on_sales_order_submit",
        # on_cancel → cancel the Magento order
        "on_cancel": "connector.sync.status_sync.on_sales_order_cancel",
    },
    # Delivery Note submitted → create Magento shipment
    # Delivery Note cancelled → add informational comment to Magento order
    "Delivery Note": {
        "on_submit": "connector.sync.status_sync.on_delivery_note_submit",
        "on_cancel": "connector.sync.status_sync.on_delivery_note_cancel",
    },
    # Sales Invoice submitted → create Magento invoice
    # Sales Invoice cancelled → add informational comment to Magento order
    "Sales Invoice": {
        "on_submit": "connector.sync.status_sync.on_sales_invoice_submit",
        "on_cancel": "connector.sync.status_sync.on_sales_invoice_cancel",
    },
}

# Scheduled tasks using cron expressions
scheduler_events = {
    "cron": {
        # Inventory sync every 15 minutes
        "*/15 * * * *": [
            "connector.tasks.sync_inventory",
        ],
        # Order pull every 4 hours (real-time push handled by Magento extension)
        "0 */4 * * *": [
            "connector.tasks.sync_orders",
        ],
        # ERPNext site product sync every 10 minutes
        "*/10 * * * *": [
            "connector.tasks.erpnext_product_sync",
        ],
        # Magento product catch-up every hour — Pending / Failed / unsynced
        # items in batches (chunked so Magento is never flooded).
        "0 * * * *": [
            "connector.tasks.full_product_sync",
            "connector.tasks.sync_attribute_options",
        ],
        # Image URL sync + Magento Product Map Pending/Failed batch every 30 min
        "*/30 * * * *": [
            "connector.tasks.sync_images",
            "connector.tasks.retry_failed_product_sync",
        ],
    }
}

# Boot session — expose app version to desk
boot_session = "connector.boot.boot_session"

# Client-side scripts loaded on specific DocType forms
doctype_js = {
    "Magento Item Group": "public/js/magento_item_group.js",
    "Magento Settings":   "public/js/magento_settings.js",
    "Item":               "public/js/item.js",
    "Sales Order":        "public/js/sales_order.js",
    "Sales Invoice":      "public/js/sales_invoice.js",
}
