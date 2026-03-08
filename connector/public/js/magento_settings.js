// Client script for Magento Settings — attribute sets for Item Groups table.

function apply_attribute_set_options_to_grid(frm, items) {
    if (!items || items.length === 0) return;
    const grid = frm.fields_dict.magento_item_groups && frm.fields_dict.magento_item_groups.grid;
    if (!grid) return;

    const options_str = "\n" + items.map(
        (s) => `${s.attribute_set_id}|${(s.attribute_set_name || s.attribute_set_id || "").toString().trim()}`
    ).join("\n");
    grid.update_docfield("attribute_set_id", "options", options_str);

    // Inject options into existing <select> elements (refresh_field would wipe our docfield update)
    const $wrapper = grid.wrapper || grid.$wrapper;
    if ($wrapper && $wrapper.length) {
        const $selects = $wrapper.find('select[data-fieldname="attribute_set_id"]');
        $selects.each(function () {
            const $sel = $(this);
            $sel.empty();
            $sel.append($('<option value="">' + (__("Select...") || "Select...") + "</option>"));
            items.forEach(function (s) {
                const val = s.attribute_set_id;
                const label = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
                $sel.append($('<option value="' + val + '">' + frappe.utils.escape_html(label) + "</option>"));
            });
            const rowIdx = $sel.closest(".grid-row")?.attr("data-idx");
            if (rowIdx !== undefined && frm.doc.magento_item_groups && frm.doc.magento_item_groups[rowIdx]) {
                const current = frm.doc.magento_item_groups[rowIdx].attribute_set_id;
                if (current) $sel.val(current);
            }
        });
    }
}

function load_attribute_sets_into_grid(frm, show_message) {
    if (!frm.fields_dict.magento_item_groups) {
        if (show_message) frappe.msgprint({ message: __("Save the form once to load attribute sets."), indicator: "orange" });
        return;
    }
    frappe.call({
        method: "connector.api.magento_options.get_magento_attribute_sets",
        freeze: show_message,
        freeze_message: __("Fetching attribute sets from Magento..."),
        callback(r) {
            if (r.exc && show_message) {
                frappe.msgprint({ message: __("Failed to load attribute sets. See Error Log for details."), indicator: "red" });
                return;
            }
            if (!r.message) return;
            if (!r.message.ok) {
                if (show_message) {
                    frappe.msgprint({
                        message: (r.message.error || __("Could not load attribute sets from Magento.")) + "\n\n" +
                            __("Use 'Diagnose Attribute Sets' in Actions to see details."),
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
                        message: __("No attribute sets returned. Use 'Diagnose Attribute Sets' in Actions to see the raw Magento response."),
                        indicator: "orange",
                        title: __("Attribute Sets"),
                    });
                }
                return;
            }
            apply_attribute_set_options_to_grid(frm, items);
            if (show_message) {
                frappe.show_alert({
                    message: __("{0} attribute set(s) loaded. You can also use 'Pick Attribute Set' to assign by row.", [items.length]),
                    indicator: "green",
                });
            }
        },
    });
}

function open_pick_attribute_set_dialog(frm) {
    frappe.call({
        method: "connector.api.magento_options.get_magento_attribute_sets",
        freeze: true,
        freeze_message: __("Fetching attribute sets..."),
        callback(r) {
            if (r.exc || !r.message || !r.message.ok) {
                frappe.msgprint({
                    message: (r.message && r.message.error) || __("Could not fetch attribute sets."),
                    indicator: "red",
                    title: __("Attribute Sets"),
                });
                return;
            }
            const items = r.message.items || [];
            if (items.length === 0) {
                frappe.msgprint({
                    message: __("No attribute sets returned from Magento. Run 'Diagnose Attribute Sets' for details."),
                    indicator: "orange",
                });
                return;
            }
            const rows = (frm.doc.magento_item_groups || []).map(function (row, idx) {
                const label = (row.item_group || __("(no Item Group)")) + " — " + (__("Row") || "Row") + " " + (idx + 1);
                return { label: label, value: idx };
            });
            if (rows.length === 0) {
                frappe.msgprint({ message: __("Add at least one row in Item Groups to Sync first."), indicator: "orange" });
                return;
            }
            const attr_options = "\n" + items.map(function (s) {
                return s.attribute_set_id + "|" + (s.attribute_set_name || s.attribute_set_id);
            }).join("\n");
            const d = new frappe.ui.Dialog({
                title: __("Pick Attribute Set"),
                fields: [
                    {
                        fieldtype: "Select",
                        fieldname: "row_index",
                        label: __("Apply to row"),
                        options: rows.map(function (r) { return r.label; }).join("\n"),
                        reqd: 1,
                    },
                    {
                        fieldtype: "Select",
                        fieldname: "attribute_set_id",
                        label: __("Magento Attribute Set"),
                        options: attr_options,
                        reqd: 1,
                    },
                ],
                primary_action_label: __("Apply"),
                primary_action: function (values) {
                    const found = rows.find(function (r) { return r.label === values.row_index; });
                    if (!found) return;
                    const idx = found.value;
                    const chosen = items.find(function (s) {
                        return String(s.attribute_set_id) === String(values.attribute_set_id) ||
                            (s.attribute_set_name && s.attribute_set_name === values.attribute_set_id);
                    });
                    if (chosen && frm.doc.magento_item_groups[idx]) {
                        frm.doc.magento_item_groups[idx].attribute_set_id = chosen.attribute_set_id;
                        frm.doc.magento_item_groups[idx].attribute_set_name = chosen.attribute_set_name || "";
                        frm.refresh_field("magento_item_groups");
                        frappe.show_alert({ message: __("Attribute set applied to row."), indicator: "green" });
                    }
                    d.hide();
                },
            });
            d.show();
        },
    });
}

function run_diagnose_attribute_sets(frm) {
    frappe.call({
        method: "connector.api.magento_options.get_magento_attribute_sets_debug",
        freeze: true,
        freeze_message: __("Calling Magento..."),
        callback(r) {
            if (r.exc) {
                frappe.msgprint({ message: __("Request failed. Check Error Log."), indicator: "red" });
                return;
            }
            const m = r.message || {};
            let msg = "";
            msg += (m.ok ? __("Success: ") : __("Failed: ")) + (m.error || (m.debug && m.debug.parsed_count !== undefined ? __("{0} sets parsed.", [m.debug.parsed_count]) : "")) + "\n\n";
            if (m.debug) {
                msg += __("Base URL: ") + (m.debug.base_url || "") + "\n";
                msg += __("Store: ") + (m.debug.store_code || "") + "\n";
                if (m.debug.endpoints_tried) msg += __("Endpoints tried: ") + m.debug.endpoints_tried.join(", ") + "\n";
                if (m.debug.parsed_count !== undefined) msg += __("Parsed count: ") + m.debug.parsed_count + "\n";
                if (m.debug.status_code) msg += __("HTTP status: ") + m.debug.status_code + "\n";
                if (m.debug.response_preview) msg += __("Response preview: ") + m.debug.response_preview + "\n";
                if (m.debug.exception_type) msg += __("Exception: ") + m.debug.exception_type + "\n";
            }
            frappe.msgprint({
                title: __("Attribute Sets — Diagnose"),
                message: msg,
                indicator: m.ok ? "green" : "orange",
            });
        },
    });
}

frappe.ui.form.on("Magento Settings", {
    refresh(frm) {
        setTimeout(function () { load_attribute_sets_into_grid(frm, false); }, 500);

        frm.add_custom_button(__("Load Attribute Sets"), function () {
            load_attribute_sets_into_grid(frm, true);
        }, __("Actions"));

        frm.add_custom_button(__("Pick Attribute Set"), function () {
            open_pick_attribute_set_dialog(frm);
        }, __("Actions"));

        frm.add_custom_button(__("Diagnose Attribute Sets"), function () {
            run_diagnose_attribute_sets(frm);
        }, __("Actions"));

        frm.add_custom_button(__("Test Connection"), function () {
            frappe.call({
                doc: frm.doc,
                method: "test_connection",
                freeze: true,
                freeze_message: __("Testing Magento connection..."),
            });
        }, __("Actions"));

        frm.add_custom_button(__("Sync All Products Now"), function () {
            frappe.confirm(
                __("This will queue a full product sync. Continue?"),
                function () {
                    frappe.call({
                        doc: frm.doc,
                        method: "trigger_full_product_sync",
                        callback: function () {
                            frappe.show_alert({ message: __("Full product sync queued."), indicator: "blue" });
                        },
                    });
                }
            );
        }, __("Actions"));

        frm.add_custom_button(__("Sync Orders Now"), function () {
            frappe.call({
                doc: frm.doc,
                method: "trigger_order_sync",
                callback: function () {
                    frappe.show_alert({ message: __("Order sync queued."), indicator: "blue" });
                },
            });
        }, __("Actions"));
    },
});
