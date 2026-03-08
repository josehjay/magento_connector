// Client script for Magento Settings (Single DocType)
// Adds action buttons and loads Magento attribute sets for Item Group → Attribute Set.

function load_attribute_sets_into_grid(frm, show_message) {
    if (!frm.fields_dict.magento_item_groups || !frm.fields_dict.magento_item_groups.grid) {
        if (show_message) frappe.msgprint({ message: __("Save the form first to load attribute sets."), indicator: "orange" });
        return;
    }
    frappe.call({
        method: "connector.api.magento_options.get_magento_attribute_sets",
        freeze: show_message,
        freeze_message: __("Fetching attribute sets from Magento..."),
        callback(r) {
            if (r.exc && show_message) {
                frappe.msgprint({ message: __("Failed to load attribute sets."), indicator: "red" });
                return;
            }
            if (!r.message) return;
            if (!r.message.ok) {
                if (show_message) {
                    frappe.msgprint({
                        message: r.message.error || __("Could not load attribute sets from Magento."),
                        indicator: "orange",
                        title: __("Attribute Sets"),
                    });
                }
                return;
            }
            const items = r.message.items || [];
            if (items.length === 0) {
                if (show_message) {
                    frappe.msgprint({
                        message: __("No attribute sets returned from Magento. Check connection and API permissions."),
                        indicator: "orange",
                        title: __("Attribute Sets"),
                    });
                }
                return;
            }
            const opts = items.map(
                (s) => `${s.attribute_set_id}|${(s.attribute_set_name || s.attribute_set_id || "").toString().trim()}`
            );
            const options_str = "\n" + opts.join("\n");
            const grid = frm.fields_dict.magento_item_groups.grid;
            grid.update_docfield("attribute_set_id", "options", options_str);
            frm.refresh_field("magento_item_groups");
            if (show_message) {
                frappe.show_alert({
                    message: __("{0} attribute set(s) loaded.", [items.length]),
                    indicator: "green",
                });
            }
        },
    });
}

frappe.ui.form.on("Magento Settings", {
    refresh(frm) {
        // Load attribute set options for Item Groups table (dropdown in grid)
        // Defer so grid is rendered; user can also use "Load Attribute Sets" button
        setTimeout(() => load_attribute_sets_into_grid(frm, false), 500);

        frm.add_custom_button(__("Load Attribute Sets"), () => {
            load_attribute_sets_into_grid(frm, true);
        }, __("Actions"));

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
