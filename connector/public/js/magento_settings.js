// Client script for Magento Settings (Single DocType)
// Adds action buttons for testing connection and triggering manual syncs.

frappe.ui.form.on("Magento Settings", {
    refresh(frm) {
        frm.add_custom_button(__("Test Connection"), () => {
            frappe.call({
                doc: frm.doc,
                method: "test_connection",
                freeze: true,
                freeze_message: __("Testing Magento connection..."),
            });
        }, __("Actions"));

        frm.add_custom_button(__("Sync All Products Now"), () => {
            frappe.confirm(
                __("This will queue a full product sync. Continue?"),
                () => {
                    frappe.call({
                        doc: frm.doc,
                        method: "trigger_full_product_sync",
                        callback() {
                            frappe.show_alert({
                                message: __("Full product sync queued."),
                                indicator: "blue",
                            });
                        },
                    });
                }
            );
        }, __("Actions"));

        frm.add_custom_button(__("Sync Orders Now"), () => {
            frappe.call({
                doc: frm.doc,
                method: "trigger_order_sync",
                callback() {
                    frappe.show_alert({
                        message: __("Order sync queued."),
                        indicator: "blue",
                    });
                },
            });
        }, __("Actions"));
    },
});
