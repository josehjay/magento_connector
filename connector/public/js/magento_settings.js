// Magento Settings — Item Groups to Sync: attribute set picker and row name sync.

(function () {
    "use strict";

    function get_attribute_set_name(items, attributeSetId) {
        if (!items || !attributeSetId) return "";
        var id = String(attributeSetId);
        var found = items.find(function (s) { return String(s.attribute_set_id) === id; });
        return found ? (found.attribute_set_name || "").toString().trim() : "";
    }

    function sync_row_attribute_set_names(frm) {
        var items = frm._magento_attribute_sets;
        if (!items || !items.length || !frm.doc.magento_item_groups) return;
        var changed = false;
        frm.doc.magento_item_groups.forEach(function (row) {
            if (!row.attribute_set_id) return;
            var expected = get_attribute_set_name(items, row.attribute_set_id);
            if (expected && row.attribute_set_name !== expected) {
                row.attribute_set_name = expected;
                changed = true;
            }
        });
        if (changed) frm.refresh_field("magento_item_groups");
    }

    function open_pick_attribute_set_dialog(frm) {
        var do_open = function (items) {
            if (!items || items.length === 0) {
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
                        fieldname: "attribute_set",
                        label: __("Magento Attribute Set"),
                        options: attr_options,
                        reqd: 1,
                    },
                ],
                primary_action_label: __("Apply"),
                primary_action: function (values) {
                    var found_row = rows.find(function (r) { return r.label === values.row_index; });
                    if (!found_row) return;
                    var idx = found_row.value;
                    var chosen_id = String(values.attribute_set || "").split("|")[0];
                    var chosen = items.find(function (s) {
                        return String(s.attribute_set_id) === chosen_id;
                    });
                    if (chosen && frm.doc.magento_item_groups[idx]) {
                        var row = frm.doc.magento_item_groups[idx];
                        frappe.model.set_value(row.doctype, row.name, "attribute_set_id", chosen.attribute_set_id);
                        frappe.model.set_value(row.doctype, row.name, "attribute_set_name", chosen.attribute_set_name || "");
                        frm.refresh_field("magento_item_groups");
                        frappe.show_alert({ message: __("Attribute set applied — save the form to persist."), indicator: "green" });
                    }
                    d.hide();
                },
            });
            d.show();
        };

        if (frm._magento_attribute_sets && frm._magento_attribute_sets.length) {
            do_open(frm._magento_attribute_sets);
            return;
        }
        frappe.call({
            method: "connector.api.magento_options.get_magento_attribute_sets",
            freeze: true,
            freeze_message: __("Fetching attribute sets from Magento…"),
            callback: function (r) {
                if (r.exc || !r.message || !r.message.ok) {
                    frappe.show_alert({ message: __("Could not load attribute sets."), indicator: "orange" });
                    return;
                }
                frm._magento_attribute_sets = r.message.items || [];
                do_open(frm._magento_attribute_sets);
            },
        });
    }

    frappe.ui.form.on("Magento Settings", {
        refresh: function (frm) {
            // Pre-load attribute sets so names are backfilled immediately
            if (frm.fields_dict.magento_item_groups) {
                frappe.call({
                    method: "connector.api.magento_options.get_magento_attribute_sets",
                    callback: function (r) {
                        if (!r.exc && r.message && r.message.ok && r.message.items && r.message.items.length) {
                            frm._magento_attribute_sets = r.message.items;
                            sync_row_attribute_set_names(frm);
                        }
                    },
                });
            }

            frm.add_custom_button(__("Pick Attribute Set"), function () {
                open_pick_attribute_set_dialog(frm);
            }, __("Actions"));

            frm.add_custom_button(__("Reset Order Sync Cursor"), function () {
                frappe.confirm(
                    __("This will clear the Last Order Sync Time so the next sync fetches orders from the last 90 days. Continue?"),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "reset_order_sync_cursor",
                            callback: function () { frm.reload_doc(); },
                        });
                    }
                );
            }, __("Actions"));

            frm.add_custom_button(__("Test Order Import"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "test_order_import",
                    freeze: true,
                    freeze_message: __("Tracing order import chain — no records will be created…"),
                });
            }, __("Actions"));

            frm.add_custom_button(__("Purge Old Logs (30d)"), function () {
                frappe.confirm(
                    __("Delete all Magento Sync Log entries older than 30 days?"),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "purge_old_logs",
                            args: { days: 30 },
                        });
                    }
                );
            }, __("Actions"));

            frm.add_custom_button(__("Diagnose Sync"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "diagnose_sync",
                    freeze: true,
                    freeze_message: __("Running sync diagnostics — checking all prerequisites…"),
                });
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

            frm.add_custom_button(__("Sync Orders Now (Background)"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "trigger_order_sync",
                    callback: function () {
                        frappe.show_alert({ message: __("Order sync queued."), indicator: "blue" });
                    },
                });
            }, __("Actions"));

            frm.add_custom_button(__("Sync Images Now"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "trigger_image_sync",
                    freeze: true,
                    freeze_message: __("Running image sync — this may take a few minutes…"),
                });
            }, __("Actions"));

            frm.add_custom_button(__("Sync Orders Now"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "trigger_order_sync_now",
                    freeze: true,
                    freeze_message: __("Pulling orders from Magento…"),
                });
            }, __("Actions"));
        },
    });
})();
