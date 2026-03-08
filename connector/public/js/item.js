// Client script for ERPNext Item form
// Magento Integration: Push buttons, Magento Config tab (attribute set, categories, custom attributes).

frappe.ui.form.on("Item", {
    refresh(frm) {
        // Magento Config tab: load attribute sets, categories, attributes from Magento
        load_magento_config_options(frm);

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

function load_magento_config_options(frm) {
    if (!frm.fields_dict.magento_config_section) return;

    frappe.call({
        method: "connector.api.magento_options.get_magento_attribute_sets",
        callback(r) {
            if (r.message && r.message.ok && r.message.items && r.message.items.length) {
                const items = r.message.items;
                const opts = items.map((s) => `${s.attribute_set_id}|${s.attribute_set_name || s.attribute_set_id}`);
                frm.set_df_property("magento_attribute_set_id", "options", "\n" + opts.join("\n"));
                const currentId = frm.doc.magento_attribute_set_id;
                if (currentId) {
                    const match = items.find((s) => String(s.attribute_set_id) === String(currentId));
                    if (match) frm.set_value("magento_attribute_set_name", match.attribute_set_name || "");
                }
            } else if (r.message && !r.message.ok && r.message.error) {
                console.warn("Connector: Magento attribute sets failed:", r.message.error);
            }
        },
    });
}

frappe.ui.form.on("Item", {
    form_render(frm) {
        if (frm.fields_dict.magento_categories) {
            const $catGrid = frm.get_field("magento_categories").$wrapper;
            if ($catGrid.find(".btn-magento-add-category").length === 0) {
                $catGrid.find(".grid-add-row").after(
                    $('<button type="button" class="btn btn-default btn-sm btn-magento-add-category" style="margin-left: 8px;">Add from Magento</button>')
                        .on("click", () => open_magento_category_dialog(frm))
                );
            }
        }
        if (frm.fields_dict.magento_custom_attributes) {
            const $attrGrid = frm.get_field("magento_custom_attributes").$wrapper;
            if ($attrGrid.find(".btn-magento-add-attr").length === 0) {
                $attrGrid.find(".grid-add-row").after(
                    $('<button type="button" class="btn btn-default btn-sm btn-magento-add-attr" style="margin-left: 8px;">Add from Magento</button>')
                        .on("click", () => open_magento_attribute_dialog(frm))
                );
            }
        }
    },
});

function open_magento_category_dialog(frm) {
    frappe.call({
        method: "connector.api.magento_options.get_magento_categories",
        callback(r) {
            if (!r.message || !r.message.ok || !r.message.items || !r.message.items.length) {
                frappe.msgprint({
                    title: __("Magento Categories"),
                    message: r.message && r.message.error ? r.message.error : __("No categories returned from Magento."),
                    indicator: "orange",
                });
                return;
            }
            const items = r.message.items.map((c) => ({
                label: (c.path || c.name || "") + " (ID: " + c.id + ")",
                value: c.id,
                name: c.name,
            }));
            frappe.prompt(
                {
                    fieldtype: "Select",
                    fieldname: "category",
                    label: __("Magento Category"),
                    options: items.map((i) => i.label).join("\n"),
                    reqd: 1,
                },
                (values) => {
                    const chosen = items.find((i) => i.label === values.category);
                    if (chosen && !frm.doc.magento_categories.some((row) => row.category_id === chosen.value)) {
                        frm.add_child("magento_categories", { category_id: chosen.value, category_name: chosen.name || chosen.value });
                        frm.refresh_field("magento_categories");
                    }
                },
                __("Add Magento Category"),
                __("Add")
            );
        },
    });
}

function open_magento_attribute_dialog(frm) {
    frappe.call({
        method: "connector.api.magento_options.get_magento_product_attributes",
        callback(r) {
            if (!r.message || !r.message.ok || !r.message.items || !r.message.items.length) {
                frappe.msgprint({
                    title: __("Magento Attributes"),
                    message: r.message && r.message.error ? r.message.error : __("No attributes returned from Magento."),
                    indicator: "orange",
                });
                return;
            }
            const items = r.message.items;
            const options = items.map((a) => a.attribute_code + (a.frontend_label ? " - " + a.frontend_label : "")).join("\n");
            frappe.prompt(
                [
                    {
                        fieldtype: "Select",
                        fieldname: "attribute_code",
                        label: __("Attribute Code"),
                        options: options,
                        reqd: 1,
                    },
                    {
                        fieldtype: "Small Text",
                        fieldname: "attribute_value",
                        label: __("Value"),
                    },
                ],
                (values) => {
                    const code = values.attribute_code.split(" - ")[0];
                    const row = frm.add_child("magento_custom_attributes", {
                        attribute_code: code,
                        attribute_value: values.attribute_value || "",
                    });
                    frm.refresh_field("magento_custom_attributes");
                },
                __("Add Magento Custom Attribute"),
                __("Add")
            );
        },
    });
}
