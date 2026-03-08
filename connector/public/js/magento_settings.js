// Magento Settings — Item Groups to Sync: load attribute sets into grid and Pick Attribute Set.

(function () {
    "use strict";

    function build_attribute_set_options(items) {
        if (!items || !items.length) return "";
        return "\n" + items.map(function (s) {
            var name = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
            return s.attribute_set_id + "|" + name;
        }).join("\n");
    }

    function inject_options_into_select($select, items, currentVal) {
        if (!$select || !$select.length || !items || !items.length) return;
        var $sel = $select.eq(0);
        $sel.empty();
        $sel.append($("<option value=\"\">" + (__("Select…") || "Select…") + "</option>"));
        items.forEach(function (s) {
            var val = s.attribute_set_id;
            var label = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
            $sel.append($("<option value=\"" + val + "\">" + frappe.utils.escape_html(label) + "</option>"));
        });
        if (currentVal != null && currentVal !== "") $sel.val(String(currentVal));
    }

    function get_attribute_set_name(items, attributeSetId) {
        if (!items || !attributeSetId) return "";
        var found = items.find(function (s) { return String(s.attribute_set_id) === String(attributeSetId); });
        return found ? (found.attribute_set_name || "").toString().trim() : "";
    }

    function sync_row_attribute_set_names(frm) {
        var items = frm._magento_attribute_sets;
        if (!items || !frm.doc.magento_item_groups) return;
        var changed = false;
        frm.doc.magento_item_groups.forEach(function (row) {
            if (row.attribute_set_id && !row.attribute_set_name) {
                row.attribute_set_name = get_attribute_set_name(items, row.attribute_set_id);
                changed = true;
            } else if (row.attribute_set_id && row.attribute_set_name) {
                var name = get_attribute_set_name(items, row.attribute_set_id);
                if (name && row.attribute_set_name !== name) {
                    row.attribute_set_name = name;
                    changed = true;
                }
            }
        });
        if (changed) frm.refresh_field("magento_item_groups");
    }

    function apply_attribute_set_options_to_grid(frm, items) {
        if (!items || items.length === 0) return;
        var grid = frm.fields_dict.magento_item_groups && frm.fields_dict.magento_item_groups.grid;
        if (!grid) return;

        grid.update_docfield("attribute_set_id", "options", build_attribute_set_options(items));
        sync_row_attribute_set_names(frm);

        var $wrapper = grid.wrapper || grid.$wrapper;
        if ($wrapper && $wrapper.length) {
            $wrapper.find('select[data-fieldname="attribute_set_id"]').each(function () {
                var $sel = $(this);
                var $row = $sel.closest(".grid-row");
                var rowIdx = $row.length ? $row.attr("data-idx") : null;
                var current = (rowIdx != null && frm.doc.magento_item_groups && frm.doc.magento_item_groups[rowIdx])
                    ? frm.doc.magento_item_groups[rowIdx].attribute_set_id : null;
                inject_options_into_select($sel, items, current);
            });
        }
    }

    function apply_options_to_row_dialog(frm, items) {
        if (!items || !items.length) return;
        var grid = frm.fields_dict.magento_item_groups && frm.fields_dict.magento_item_groups.grid;
        var rowForm = grid && grid.grid_form;
        if (rowForm && rowForm.fields_dict && rowForm.fields_dict.attribute_set_id) {
            rowForm.set_df_property("attribute_set_id", "options", build_attribute_set_options(items));
            if (rowForm.refresh_field) rowForm.refresh_field("attribute_set_id");
            return;
        }
        var $container = $(".modal:visible, .slide-over:visible, .form-onboarding:visible");
        if (!$container.length) $container = $(document.body);
        var $selects = $container.find('select[data-fieldname="attribute_set_id"]');
        if (!$selects.length) $selects = $container.find("[data-fieldname=\"attribute_set_id\"] select");
        if (!$selects.length) $container.find(".frappe-control").each(function () {
            var $ctrl = $(this);
            if ($ctrl.find("label").text().indexOf("Magento Attribute Set") !== -1 || $ctrl.attr("data-fieldname") === "attribute_set_id") {
                var $s = $ctrl.find("select");
                if ($s.length) $selects = $selects.add($s);
            }
        });
        $selects.each(function () {
            inject_options_into_select($(this), items, $(this).val());
        });
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
                    sync_row_attribute_set_names(frm);
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
        magento_item_groups_attribute_set_id: function (frm, cdt, cdn) {
            var row = frappe.get_doc(cdt, cdn);
            var name = get_attribute_set_name(frm._magento_attribute_sets, row.attribute_set_id);
            if (name) frappe.model.set_value(cdt, cdn, "attribute_set_name", name);
        },
        magento_item_groups_on_form_rendered: function (frm, cdt, cdn) {
            function try_apply() {
                var items = frm._magento_attribute_sets;
                if (items && items.length) {
                    apply_options_to_row_dialog(frm, items);
                    return;
                }
                frappe.call({
                    method: "connector.api.magento_options.get_magento_attribute_sets",
                    callback: function (r) {
                        if (r.message && r.message.ok && r.message.items && r.message.items.length) {
                            frm._magento_attribute_sets = r.message.items;
                            apply_options_to_row_dialog(frm, r.message.items);
                        }
                    },
                });
            }
            setTimeout(try_apply, 0);
            setTimeout(try_apply, 150);
            setTimeout(try_apply, 400);
        },
        refresh: function (frm) {
            var cur_frm = frm;
            $(document).on("shown.bs.modal", ".modal", function onMagentoSettingsModalShown() {
                var $modal = $(this);
                if (!$modal.find("[data-fieldname=\"attribute_set_id\"]").length && !$modal.find("select[data-fieldname=\"attribute_set_id\"]").length) return;
                if (!cur_frm || !cur_frm.fields_dict.magento_item_groups) return;
                function do_apply() {
                    if (cur_frm._magento_attribute_sets && cur_frm._magento_attribute_sets.length) {
                        apply_options_to_row_dialog(cur_frm, cur_frm._magento_attribute_sets);
                        return;
                    }
                    frappe.call({
                        method: "connector.api.magento_options.get_magento_attribute_sets",
                        callback: function (r) {
                            if (r.message && r.message.ok && r.message.items && r.message.items.length) {
                                cur_frm._magento_attribute_sets = r.message.items;
                                apply_options_to_row_dialog(cur_frm, r.message.items);
                            }
                        },
                    });
                }
                setTimeout(do_apply, 50);
                setTimeout(do_apply, 300);
            });
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

    frappe.ui.form.on("Magento Item Group", {
        attribute_set_id: function (frm, cdt, cdn) {
            if (!frm._magento_attribute_sets) return;
            var row = frappe.get_doc(cdt, cdn);
            var name = get_attribute_set_name(frm._magento_attribute_sets, row.attribute_set_id);
            if (name) frappe.model.set_value(cdt, cdn, "attribute_set_name", name);
        },
    });
})();
