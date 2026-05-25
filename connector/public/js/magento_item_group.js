// Magento Item Group — attribute set picker and attribute mapping helpers

(function () {
    "use strict";

    // Descriptions for the erpnext_field input, keyed by erpnext_source value.
    var FIELD_HINTS = {
        "Item Field":     "ERPNext Item field name, e.g. item_code, item_name, weight_per_unit, description",
        "Item Barcode":   "Barcode type to match (e.g. EAN-13, UPC-A). Leave blank to use the first barcode.",
        "Item Attribute": "Item Attribute name for variant items, e.g. Color, Size.",
        "Custom Value":   "Literal text sent to Magento exactly as typed.",
    };

    // ── Magento Item Group row: load attribute set options ─────────────────

    frappe.ui.form.on("Magento Item Group", {
        refresh: function (frm) {
            if (!frm.fields_dict.attribute_set_id) return;
            frappe.call({
                method: "connector.api.magento_options.get_magento_attribute_sets",
                callback: function (r) {
                    if (r.message && r.message.ok && r.message.items && r.message.items.length) {
                        var opts = "\n" + r.message.items.map(function (s) {
                            var name = (s.attribute_set_name || s.attribute_set_id || "").toString().trim();
                            return s.attribute_set_id + "|" + name;
                        }).join("\n");
                        frm.set_df_property("attribute_set_id", "options", opts);
                        frm.refresh_field("attribute_set_id");
                    }
                },
            });
        },
    });

    // ── Magento Attribute Mapping child: contextual field hint ─────────────

    frappe.ui.form.on("Magento Attribute Mapping", {
        erpnext_source: function (frm, cdt, cdn) {
            var row = locals[cdt][cdn];
            var hint = FIELD_HINTS[row.erpnext_source] || "";
            // Update the description shown under erpnext_field in the grid row
            frm.set_df_property("erpnext_field", "description", hint, cdt, cdn);
            // Refresh the child table so the description renders
            if (frm.fields_dict && frm.fields_dict.attribute_mappings) {
                frm.refresh_field("attribute_mappings");
            }
        },
    });

})();
