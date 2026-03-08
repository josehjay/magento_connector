// Magento Item Group (row in Magento Settings): load Magento attribute sets into dropdown
// when the row is opened in the "Editing Row" dialog.

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
