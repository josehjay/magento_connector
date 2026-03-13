// Sales Order — Magento integration panel
// Shows Magento order info ONLY when the SO was imported from Magento.

frappe.ui.form.on("Sales Order", {
    refresh: function (frm) {
        var is_magento_order = !!(frm.doc.magento_order_id || frm.doc.magento_increment_id);

        // Hide the entire Magento section for non-Magento orders
        _toggle_magento_section(frm, is_magento_order);

        if (!is_magento_order) return;

        _render_magento_banner(frm);
        _add_magento_buttons(frm);
    },
});

// ── Section visibility ──────────────────────────────────────────────────────

function _toggle_magento_section(frm, visible) {
    var magento_fields = [
        "magento_so_section_break",
        "magento_order_id",
        "magento_increment_id",
        "magento_order_status",
    ];
    magento_fields.forEach(function (f) {
        if (frm.fields_dict[f]) {
            frm.toggle_display(f, visible);
        }
    });
}

// ── Banner ──────────────────────────────────────────────────────────────────

function _render_magento_banner(frm) {
    // Remove any previously injected banner to avoid duplicates on re-render
    frm.layout && frm.layout.wrapper && frm.layout.wrapper.find(".magento-order-banner").remove();

    var status      = frm.doc.magento_order_status || "pending";
    var status_color = {
        "processing":      "#2490ef",
        "complete":        "#28a745",
        "canceled":        "#e74c3c",
        "closed":          "#6c757d",
        "holded":          "#fd7e14",
        "pending":         "#ffc107",
        "pending_payment": "#ffc107",
    }[status] || "#888";

    var order_ref = frappe.utils.escape_html(
        frm.doc.magento_increment_id || String(frm.doc.magento_order_id || "")
    );
    var map_url = "/app/magento-order-map?magento_increment_id="
        + encodeURIComponent(frm.doc.magento_increment_id || "");

    var html = `
        <div class="magento-order-banner" style="
            background:#f0f4ff;
            border-left:4px solid ${status_color};
            border-radius:4px;
            padding:8px 14px;
            margin:8px 0 12px 0;
            font-size:13px;
            display:flex;
            align-items:center;
            flex-wrap:wrap;
            gap:12px;
        ">
            <span style="font-weight:600;color:#1a1a2e;">
                🛒 Magento Order
                <span style="font-family:monospace;">#${order_ref}</span>
            </span>
            <span style="background:${status_color};color:#fff;padding:2px 8px;
                         border-radius:12px;font-size:11px;text-transform:uppercase;
                         letter-spacing:.05em;">
                ${frappe.utils.escape_html(status)}
            </span>
            <a href="${map_url}" target="_blank"
               style="font-size:12px;color:#2490ef;text-decoration:none;">
                View in Order Map ↗
            </a>
        </div>`;

    // Inject just above the Magento section if it exists, else at the form top
    var section = frm.fields_dict["magento_so_section_break"];
    if (section) {
        $(section.wrapper).before(html);
    } else {
        $(frm.layout.wrapper).prepend(html);
    }
}

// ── Buttons ─────────────────────────────────────────────────────────────────

function _add_magento_buttons(frm) {
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
                frappe.show_alert({
                    message:
                        "<b>Magento Status:</b> " + (data.magento_status || "—") + "<br>" +
                        "<b>Imported on:</b> "   + (data.imported_on      || "—") + "<br>" +
                        "<b>Last status sync:</b> " + (data.last_status_sync || "—"),
                    indicator: "blue",
                }, 8);
            }
        );
    }, __("Magento"));
}
