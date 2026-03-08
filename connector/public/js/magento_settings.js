// Client script for Magento Settings (Single DocType)
// Adds action buttons and loads Magento attribute sets for Item Group → Attribute Set.

frappe.ui.form.on("Magento Settings", {
    refresh(frm) {
        // Load attribute set options for Item Groups table (options set on grid column)
        if (frm.fields_dict.magento_item_groups) {
            frappe.call({
                method: "connector.api.magento_options.get_magento_attribute_sets",
                callback(r) {
                    if (r.message && r.message.ok && r.message.items && r.message.items.length) {
                        const opts = r.message.items.map(
                            (s) => `${s.attribute_set_id}|${s.attribute_set_name || s.attribute_set_id}`
                        );
                        frm.fields_dict.magento_item_groups.grid.update_docfield(
                            "attribute_set_id",
                            "options",
                            "\n" + opts.join("\n")
                        );
                    }
                },
            });
        }

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
