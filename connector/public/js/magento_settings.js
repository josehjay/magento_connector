// Magento Settings — Item Groups to Sync: load attribute sets into grid and Pick Attribute Set.

(function () {
    "use strict";

    function apply_attribute_set_options_to_grid(frm, items) {
        if (!items || items.length === 0) return;
        var grid = frm.fields_dict.magento_item_groups && frm.fields_dict.magento_item_groups.grid;
        if (!grid) return;

        var options_str = "\n" + items.map(function (s) {
            var name = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
            return s.attribute_set_id + "|" + name;
        }).join("\n");
        grid.update_docfield("attribute_set_id", "options", options_str);

        var $wrapper = grid.wrapper || grid.$wrapper;
        if ($wrapper && $wrapper.length) {
            $wrapper.find('select[data-fieldname="attribute_set_id"]').each(function () {
                var $sel = $(this);
                $sel.empty();
                $sel.append($("<option value=\"\">" + (__("Select…") || "Select…") + "</option>"));
                items.forEach(function (s) {
                    var val = s.attribute_set_id;
                    var label = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
                    $sel.append($("<option value=\"" + val + "\">" + frappe.utils.escape_html(label) + "</option>"));
                });
                var $row = $sel.closest(".grid-row");
                var rowIdx = $row.length ? $row.attr("data-idx") : null;
                if (rowIdx !== undefined && rowIdx !== null && frm.doc.magento_item_groups && frm.doc.magento_item_groups[rowIdx]) {
                    var current = frm.doc.magento_item_groups[rowIdx].attribute_set_id;
                    if (current != null && current !== "") $sel.val(String(current));
                }
            });
        }
    }

    function fetch_and_apply_attribute_sets(frm, callback) {
        if (!frm.fields_dict.magento_item_groups) {
            if (callback) callback(null);
            return;
        }
        frappe.call({
            method: "connector.api.magento_options.get_magento_attribute_sets",
            callback: function (r) {
                var items = null;
                if (!r.exc && r.message && r.message.ok && r.message.items && r.message.items.length) {
                    items = r.message.items;
                    frm._magento_attribute_sets = items;
                    apply_attribute_set_options_to_grid(frm, items);
                } else if (r.message && !r.message.ok) {
                    // Error already logged on server
                }
                if (callback) callback(items);
            },
        });
    }

    function open_pick_attribute_set_dialog(frm) {
        frappe.call({
            method: "connector.api.magento_options.get_magento_attribute_sets",
            freeze: true,
            freeze_message: __("Fetching attribute sets…"),
            callback: function (r) {
                if (r.exc || !r.message || !r.message.ok) {
                    frappe.show_alert({ message: __("Could not load attribute sets."), indicator: "orange" });
                    return;
                }
                var items = r.message.items || [];
                if (items.length === 0) {
                    frappe.show_alert({ message: __("No attribute sets returned from Magento."), indicator: "orange" });
                    return;
                }
                var rows = (frm.doc.magento_item_groups || []).map(function (row, idx) {
                    var label = (row.item_group || __("(no Item Group)")) + " — " + __("Row") + " " + (idx + 1);
                    return { label: label, value: idx };
                });
                if (rows.length === 0) {
                    frappe.msgprint({ message: __("Add at least one row in Item Groups to Sync first."), indicator: "orange" });
                    return;
                }
                var attr_options = "\n" + items.map(function (s) {
                    return s.attribute_set_id + "|" + (s.attribute_set_name || s.attribute_set_id);
                }).join("\n");
                var d = new frappe.ui.Dialog({
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
                        var found = rows.find(function (r) { return r.label === values.row_index; });
                        if (!found) return;
                        var idx = found.value;
                        var chosen = items.find(function (s) {
                            return String(s.attribute_set_id) === String(values.attribute_set_id) ||
                                (s.attribute_set_name && String(s.attribute_set_name) === String(values.attribute_set_id));
                        });
                        if (!chosen) {
                            chosen = items.find(function (s) { return String(s.attribute_set_id) === String(values.attribute_set_id); });
                        }
                        if (chosen && frm.doc.magento_item_groups[idx]) {
                            frm.doc.magento_item_groups[idx].attribute_set_id = chosen.attribute_set_id;
                            frm.doc.magento_item_groups[idx].attribute_set_name = chosen.attribute_set_name || "";
                            frm.refresh_field("magento_item_groups");
                            apply_attribute_set_options_to_grid(frm, items);
                            frappe.show_alert({ message: __("Attribute set applied."), indicator: "green" });
                        }
                        d.hide();
                    },
                });
                d.show();
            },
        });
    }

    frappe.ui.form.on("Magento Settings", {
        refresh: function (frm) {
            if (frm.fields_dict.magento_item_groups) {
                setTimeout(function () { fetch_and_apply_attribute_sets(frm); }, 400);
                setTimeout(function () {
                    if (frm._magento_attribute_sets && frm._magento_attribute_sets.length) {
                        apply_attribute_set_options_to_grid(frm, frm._magento_attribute_sets);
                    }
                }, 1200);
                setTimeout(function () {
                    if (frm._magento_attribute_sets && frm._magento_attribute_sets.length) {
                        apply_attribute_set_options_to_grid(frm, frm._magento_attribute_sets);
                    }
                }, 2500);
            }
            frm.add_custom_button(__("Pick Attribute Set"), function () {
                open_pick_attribute_set_dialog(frm);
            }, __("Actions"));
            frm.add_custom_button(__("Test Connection"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "test_connection",
                    freeze: true,
                    freeze_message: __("Testing Magento connection…"),
                });
            }, __("Actions"));
            frm.add_custom_button(__("Sync All Products Now"), function () {
                frappe.confirm(__("This will queue a full product sync. Continue?"), function () {
                    frappe.call({
                        doc: frm.doc,
                        method: "trigger_full_product_sync",
                        callback: function () {
                            frappe.show_alert({ message: __("Full product sync queued."), indicator: "blue" });
                        },
                    });
                });
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
})();
