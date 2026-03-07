// Client script for ERPNext Item form
// Adds a "Push to Magento" button when the item has sync_to_magento enabled.

frappe.ui.form.on("Item", {
    refresh(frm) {
        if (frm.doc.sync_to_magento && !frm.doc.__islocal) {
            frm.add_custom_button(__("Push to Magento"), () => {
                frappe.call({
                    method: "connector.sync.product_sync.push_item_to_magento",
                    args: { item_code: frm.doc.item_code },
                    freeze: true,
                    freeze_message: __("Pushing to Magento..."),
                    callback(r) {
                        if (!r.exc) {
                            frappe.show_alert({
                                message: __("Item pushed to Magento successfully."),
                                indicator: "green",
                            });
                            frm.reload_doc();
                        }
                    },
                });
            }, __("Magento"));

            if (frm.doc.magento_product_id) {
                frm.set_intro(
                    __("Magento Product ID: {0}", [frm.doc.magento_product_id]),
                    "blue"
                );
            }
        }

        if (frm.doc.sync_to_erpnext_sites && !frm.doc.__islocal) {
            frm.add_custom_button(__("Push to ERPNext Sites"), () => {
                frappe.call({
                    method: "connector.sync.erpnext_product_sync.push_item_to_all_sites",
                    args: { item_code: frm.doc.item_code },
                    freeze: true,
                    freeze_message: __("Pushing to remote ERPNext sites..."),
                    callback(r) {
                        if (!r.exc) {
                            frappe.show_alert({
                                message: __("Item push to ERPNext sites queued."),
                                indicator: "green",
                            });
                        }
                    },
                });
            }, __("ERPNext Sync"));
        }
    },
});
