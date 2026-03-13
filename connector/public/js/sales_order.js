// Sales Order — Magento integration panel
// Shows Magento order info and status when the SO was created from a Magento order.

frappe.ui.form.on("Sales Order", {
    refresh: function (frm) {
        // Only show Magento panel when this SO has a Magento order link
        if (!frm.doc.magento_increment_id && !frm.doc.magento_order_id) {
            return;
        }

        _render_magento_banner(frm);
        _add_magento_buttons(frm);
    },
});

function _render_magento_banner(frm) {
    // Remove any previously injected banner to avoid duplicates on re-render
    frm.fields_dict.magento_so_section_break &&
        $(frm.fields_dict.magento_so_section_break.wrapper)
            .find(".magento-order-banner")
            .remove();

    var status      = frm.doc.magento_order_status || "unknown";
    var status_color = {
        "processing": "#2490ef",
        "complete":   "#28a745",
        "canceled":   "#e74c3c",
        "closed":     "#6c757d",
        "holded":     "#fd7e14",
        "pending":    "#ffc107",
        "pending_payment": "#ffc107",
    }[status] || "#555";

    var html = `
        <div class="magento-order-banner" style="
            background: #f0f4ff;
            border-left: 4px solid ${status_color};
            border-radius: 4px;
            padding: 8px 14px;
            margin: 8px 0 12px 0;
            font-size: 13px;
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        ">
            <span style="font-weight:600;color:#1a1a2e;">
                🛒 Magento Order
                <span style="font-family:monospace;">#${frappe.utils.escape_html(frm.doc.magento_increment_id || frm.doc.magento_order_id || "")}</span>
            </span>
            <span style="background:${status_color};color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;text-transform:uppercase;letter-spacing:0.05em;">
                ${frappe.utils.escape_html(status)}
            </span>
            <a href="/app/magento-order-map?magento_increment_id=${encodeURIComponent(frm.doc.magento_increment_id || '')}"
               target="_blank"
               style="font-size:12px;color:#2490ef;text-decoration:none;">
                View in Order Map ↗
            </a>
        </div>
    `;

    // Inject into the Magento section header if it exists, otherwise prepend to form body
    var section = frm.fields_dict.magento_so_section_break;
    if (section) {
        $(section.wrapper).prepend(html);
    } else {
        $(frm.layout.wrapper).prepend(html);
    }
}

function _add_magento_buttons(frm) {
    // Refresh status from Magento Order Map (no API call needed)
    frm.add_custom_button(__("Refresh Magento Status"), function () {
        frappe.db.get_value(
            "Magento Order Map",
            { magento_increment_id: frm.doc.magento_increment_id },
            ["magento_status", "imported_on", "last_status_sync"],
            function (data) {
                if (!data) {
                    frappe.show_alert({
                        message: __("No Magento Order Map entry found for this order."),
                        indicator: "orange",
                    });
                    return;
                }
                var msg =
                    "<b>Magento Status:</b> " + (data.magento_status || "—") + "<br>" +
                    "<b>Imported on:</b> " + (data.imported_on || "—") + "<br>" +
                    "<b>Last status sync:</b> " + (data.last_status_sync || "—");
                frappe.show_alert({ message: msg, indicator: "blue" }, 8);
            }
        );
    }, __("Magento"));
}
