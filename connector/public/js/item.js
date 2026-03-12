// Client script for ERPNext Item form — Magento tab: Push to Magento / Push to ERPNext Sites.
// When "Sync to Magento" is on, saving the form automatically queues an update to Magento.
// Use "Push to Magento" to send the current saved data immediately (save first if you changed name, description, etc.).

frappe.ui.form.on("Item", {
    refresh(frm) {
        if (frm.doc.sync_to_magento && !frm.doc.__islocal) {
            frm.add_custom_button(__("Push to Magento"), () => {
                if (frm.is_dirty()) {
                    frappe.msgprint({
                        title: __("Save First"),
                        message: __("Please save your changes first. Then click Push to Magento to send the updated details (name, description, price, etc.) to Magento."),
                        indicator: "blue",
                    });
                    return;
                }
                frappe.call({
                    method: "connector.sync.product_sync.push_item_to_magento",
                    args: { item_code: frm.doc.item_code },
                    freeze: true,
                    freeze_message: __("Pushing to Magento and pulling data back…"),
                    callback(r) {
                        if (!r.exc) {
                            var result = r.message || {};
                            var parts = [__("Item pushed to Magento.")];
                            if (result.image && result.image.image_updated) {
                                parts.push(__("Image synced from Magento."));
                            } else if (result.image && result.image.image_url) {
                                parts.push(__("Image already up to date."));
                            } else if (result.image && result.image.error === "no_media_in_magento") {
                                parts.push(__("No image found in Magento for this product."));
                            }
                            frappe.show_alert({
                                message: parts.join(" "),
                                indicator: "green",
                            });
                            frm.reload_doc();
                        }
                    },
                });
            }, __("Magento"));

            if (frm.doc.magento_product_id) {
                frm.set_intro(
                    __("Magento Product ID: {0}. Changes are synced to Magento when you save, or click Push to Magento.", [frm.doc.magento_product_id]),
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
