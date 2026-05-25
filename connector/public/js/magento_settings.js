// Magento Settings — Item Groups attribute set picker, attribute mapping builder, and action buttons

(function () {
    "use strict";

    // ── Attribute Set helpers ──────────────────────────────────────────────

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

    // ── Attribute Mapping builder ──────────────────────────────────────────

    var SOURCE_DESCRIPTIONS = {
        "Item Field":     __("ERPNext Item field name (e.g. item_code, item_name, weight_per_unit, description)"),
        "Item Barcode":   __("Barcode type filter (e.g. EAN-13, UPC-A). Leave blank to use the first barcode regardless of type."),
        "Item Attribute": __("Item Attribute name used in variants (e.g. Color, Size). Only populated for variant items."),
        "Custom Value":   __("Literal text to always send as-is to Magento for every item in this attribute set."),
    };

    function render_attributes_table(frm, $wrapper, attrs, row_idx) {
        if (!attrs || !attrs.length) {
            $wrapper.html('<p class="text-muted">' + __("No attributes found for this set.") + "</p>");
            return;
        }

        var html = '<div style="max-height:360px;overflow-y:auto;">';
        html += '<input type="text" class="form-control input-sm mb-2" id="attr-search" placeholder="' + __("Filter attributes…") + '" style="margin-bottom:6px;">';
        html += '<table class="table table-bordered table-condensed" style="font-size:12px;">';
        html += "<thead><tr>";
        html += "<th>" + __("Attribute Code") + "</th>";
        html += "<th>" + __("Label") + "</th>";
        html += "<th>" + __("ERPNext Source") + "</th>";
        html += "<th>" + __("ERPNext Field / Value") + "</th>";
        html += "<th></th>";
        html += "</tr></thead><tbody id='attr-tbody'>";

        attrs.forEach(function (attr, i) {
            var code = frappe.utils.escape_html(attr.attribute_code || "");
            var label = frappe.utils.escape_html(attr.frontend_label || attr.attribute_code || "");
            var source_opts = ["Item Field", "Item Barcode", "Item Attribute", "Custom Value"].map(function (s) {
                return '<option value="' + s + '">' + __(s) + "</option>";
            }).join("");
            html += '<tr data-code="' + code + '" data-label="' + label.toLowerCase() + '">';
            html += "<td><code>" + code + "</code></td>";
            html += "<td>" + label + "</td>";
            html += '<td><select class="form-control input-sm src-select" style="min-width:110px;">' + source_opts + "</select></td>";
            html += '<td><input type="text" class="form-control input-sm field-input" style="min-width:110px;" placeholder="' + __("field name…") + '"></td>';
            html += '<td><button class="btn btn-xs btn-primary add-mapping-btn" data-idx="' + row_idx + '" data-code="' + code + '">' + __("+ Add") + "</button></td>";
            html += "</tr>";
        });

        html += "</tbody></table></div>";
        $wrapper.html(html);

        // Live filter
        $wrapper.find("#attr-search").on("input", function () {
            var q = ($(this).val() || "").toLowerCase().trim();
            $wrapper.find("#attr-tbody tr").each(function () {
                var match = !q || $(this).data("code").toLowerCase().indexOf(q) !== -1
                                || $(this).data("label").toLowerCase().indexOf(q) !== -1;
                $(this).toggle(match);
            });
        });

        // Source select → update field placeholder
        $wrapper.find(".src-select").on("change", function () {
            var src = $(this).val();
            var $input = $(this).closest("tr").find(".field-input");
            $input.attr("placeholder", SOURCE_DESCRIPTIONS[src] || __("field name…"));
        });

        // Add button → insert a mapping row into the Item Group's attribute_mappings
        $wrapper.find(".add-mapping-btn").on("click", function () {
            var $btn = $(this);
            var code = $btn.data("code");
            var idx = parseInt($btn.data("idx"), 10);
            var $tr = $btn.closest("tr");
            var src = $tr.find(".src-select").val() || "Item Field";
            var field_val = $tr.find(".field-input").val() || "";

            var item_group_row = frm.doc.magento_item_groups[idx];
            if (!item_group_row) {
                frappe.show_alert({ message: __("Row not found. Save and try again."), indicator: "orange" });
                return;
            }

            var new_row = frappe.model.add_child(
                item_group_row,
                "Magento Attribute Mapping",
                "attribute_mappings"
            );
            frappe.model.set_value(new_row.doctype, new_row.name, "magento_attribute_code", code);
            frappe.model.set_value(new_row.doctype, new_row.name, "erpnext_source", src);
            frappe.model.set_value(new_row.doctype, new_row.name, "erpnext_field", field_val);
            frappe.model.set_value(new_row.doctype, new_row.name, "enabled", 1);
            frm.refresh_field("magento_item_groups");
            frappe.show_alert({ message: __("Mapping added: ") + code, indicator: "green" });
            $btn.prop("disabled", true).text(__("Added ✓"));
        });
    }

    function open_build_mappings_dialog(frm) {
        var ig_rows = (frm.doc.magento_item_groups || []).map(function (row, idx) {
            var name = (row.item_group || __("(no group)"));
            var set_name = row.attribute_set_name ? (" — " + row.attribute_set_name) : "";
            return { label: name + set_name, value: idx, row: row };
        });

        if (!ig_rows.length) {
            frappe.msgprint({ message: __("Add at least one row in Item Groups to Sync first, then pick an attribute set for it."), indicator: "orange" });
            return;
        }

        var d = new frappe.ui.Dialog({
            title: __("Build Attribute Mappings"),
            size: "extra-large",
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "intro_html",
                    options: '<p class="text-muted small">' +
                        __("Select an Item Group row, then load its Magento attribute set. " +
                           "Click <b>+ Add</b> next to any attribute to insert it into that row's mapping table. " +
                           "Fill in the ERPNext source and field before adding, or edit the row afterwards.") +
                        "</p>",
                },
                {
                    fieldtype: "Select",
                    fieldname: "row_index",
                    label: __("Item Group Row"),
                    options: ig_rows.map(function (r) { return r.label; }).join("\n"),
                    reqd: 1,
                },
                {
                    fieldtype: "Button",
                    fieldname: "load_btn",
                    label: __("Load Magento Attributes for this Set"),
                },
                {
                    fieldtype: "HTML",
                    fieldname: "attrs_html",
                },
            ],
            primary_action_label: __("Done"),
            primary_action: function () { d.hide(); },
        });

        d.fields_dict.load_btn.$input.on("click", function () {
            var label = d.get_value("row_index");
            var found = ig_rows.find(function (r) { return r.label === label; });
            if (!found) return;

            if (!found.row.attribute_set_id) {
                frappe.show_alert({ message: __("Pick an attribute set for this row first (use 'Pick Attribute Set')."), indicator: "orange" });
                return;
            }

            frappe.call({
                method: "connector.api.magento_options.get_magento_attributes_for_set",
                args: { attribute_set_id: found.row.attribute_set_id },
                freeze: true,
                freeze_message: __("Loading attributes for ") + (found.row.attribute_set_name || found.row.attribute_set_id) + "…",
                callback: function (r) {
                    var $w = d.fields_dict.attrs_html.$wrapper;
                    if (r.exc || !r.message || !r.message.ok) {
                        $w.html('<p class="text-danger">' + __("Could not load attributes. Check connection.") + "</p>");
                        return;
                    }
                    render_attributes_table(frm, $w, r.message.items || [], found.value);
                },
            });
        });

        d.show();
    }

    // ── Main form handler ─────────────────────────────────────────────────

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

            // ── Connection group ─────────────────────────────────────────
            frm.add_custom_button(__("Test Connection"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "test_connection",
                    freeze: true,
                    freeze_message: __("Testing Magento connection…"),
                });
            }, __("Connection"));

            frm.add_custom_button(__("Diagnose Sync"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "diagnose_sync",
                    freeze: true,
                    freeze_message: __("Running diagnostics — checking all prerequisites…"),
                });
            }, __("Connection"));

            frm.add_custom_button(__("Signature Verification Status"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "view_signature_verification_status",
                    freeze: true,
                    freeze_message: __("Reading signature verification diagnostics…"),
                });
            }, __("Connection"));

            frm.add_custom_button(__("Reset Signature Counters"), function () {
                frappe.confirm(
                    __("Reset signature verification diagnostics counters now?"),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "reset_signature_verification_counters",
                            freeze: true,
                            freeze_message: __("Resetting signature diagnostics counters…"),
                        });
                    }
                );
            }, __("Connection"));

            // ── Products group ───────────────────────────────────────────
            frm.add_custom_button(__("Pick Attribute Set"), function () {
                open_pick_attribute_set_dialog(frm);
            }, __("Products"));

            frm.add_custom_button(__("Build Attribute Mappings"), function () {
                open_build_mappings_dialog(frm);
            }, __("Products"));

            frm.add_custom_button(__("Sync All Products Now"), function () {
                frappe.confirm(__("Queue a full product sync? This runs in the background."), function () {
                    frappe.call({
                        doc: frm.doc,
                        method: "trigger_full_product_sync",
                        callback: function () {
                            frappe.show_alert({ message: __("Full product sync queued."), indicator: "blue" });
                        },
                    });
                });
            }, __("Products"));

            frm.add_custom_button(__("Sync Images Now"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "trigger_image_sync",
                    freeze: true,
                    freeze_message: __("Running image sync — may take a few minutes…"),
                });
            }, __("Products"));

            // ── Orders group ─────────────────────────────────────────────
            // "Pull Orders from Magento" is now a 4-hour safety-net reconciliation sweep.
            // Real-time order creation is handled by the Magento Kitabu_ErpNextConnector push module.
            frm.add_custom_button(__("Pull Orders from Magento"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "trigger_order_sync_now",
                    freeze: true,
                    freeze_message: __("Reconciling orders with Magento — this is a safety-net sweep…"),
                });
            }, __("Orders"));

            frm.add_custom_button(__("Reset Order Sync Cursor"), function () {
                frappe.confirm(
                    __("This will clear the Last Order Sync Time so the next pull fetches orders from the last 90 days. Continue?"),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "reset_order_sync_cursor",
                            callback: function () { frm.reload_doc(); },
                        });
                    }
                );
            }, __("Orders"));

            frm.add_custom_button(__("Test Order Import"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "test_order_import",
                    freeze: true,
                    freeze_message: __("Tracing order import chain — no records will be created…"),
                });
            }, __("Orders"));

            frm.add_custom_button(__("View Recent Order Log"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "view_recent_push_log",
                });
            }, __("Orders"));

            // ── Status Sync group ────────────────────────────────────────
            frm.add_custom_button(__("Test Status Sync"), function () {
                frappe.prompt(
                    [
                        {
                            fieldtype: "Link",
                            fieldname: "sales_order",
                            label: __("Sales Order"),
                            options: "Sales Order",
                            reqd: 1,
                            description: __(
                                "Enter a Magento-imported Sales Order name. " +
                                "This will immediately push 'processing' status to Magento " +
                                "and show you the exact result (or error)."
                            ),
                        },
                    ],
                    function (values) {
                        frappe.call({
                            doc: frm.doc,
                            method: "test_status_sync",
                            args: { sales_order: values.sales_order },
                            freeze: true,
                            freeze_message: __("Pushing status to Magento…"),
                        });
                    },
                    __("Test Status Sync"),
                    __("Run Test")
                );
            }, __("Status"));

            // ── Maintenance group ────────────────────────────────────────
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
            }, __("Maintenance"));
        },
    });
})();
