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
    },
    "Sales Order": {
        "on_submit": "connector.sync.status_sync.on_sales_order_submit",
        "on_cancel": "connector.sync.status_sync.on_sales_order_cancel",
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
        # Magento full product sync once daily at 1 AM
        "0 1 * * *": [
            "connector.tasks.full_product_sync",
        ],
        # Image URL sync and retry failed products every 30 minutes
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
}
