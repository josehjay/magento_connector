// Sales Order — Magento integration panel
// Shows Magento order info ONLY when the SO was imported from Magento.
//
// The banner uses Frappe's own headline + indicator pill (theme-aware, dark
// mode safe) instead of injected, hand-styled DOM. The dashboard headline is
// cleared on every form refresh, so re-rendering never duplicates it.

(function () {
    "use strict";

    var MAGENTO_SECTION_FIELDS = [
        "magento_so_section_break",
        "magento_order_id",
        "magento_increment_id",
        "magento_order_status",
    ];

    // Magento order state → Frappe indicator colour.
    var STATUS_COLORS = {
        processing: "blue",
        complete: "green",
        canceled: "red",
        closed: "gray",
        holded: "orange",
        pending: "yellow",
        pending_payment: "yellow",
    };

    frappe.ui.form.on("Sales Order", {
        refresh: function (frm) {
            var is_magento_order = !!(frm.doc.magento_order_id || frm.doc.magento_increment_id);

            // Hide the entire Magento section for non-Magento orders
            toggle_magento_section(frm, is_magento_order);

            if (!is_magento_order) return;

            render_magento_headline(frm);
            add_magento_buttons(frm);
        },
    });

    // ── Section visibility ──────────────────────────────────────────────────

    function toggle_magento_section(frm, visible) {
        MAGENTO_SECTION_FIELDS.forEach(function (f) {
            if (frm.fields_dict[f]) {
                frm.toggle_display(f, visible);
            }
        });
    }

    // ── Headline ────────────────────────────────────────────────────────────

    function render_magento_headline(frm) {
        if (!frm.dashboard || !frm.dashboard.set_headline_alert) return;

        var esc = frappe.utils.escape_html;
        var status = String(frm.doc.magento_order_status || "pending");
        var color = STATUS_COLORS[status.toLowerCase()] || "gray";
        var order_ref = frm.doc.magento_increment_id || String(frm.doc.magento_order_id || "");
        var map_url = "/app/magento-order-map?sales_order=" + encodeURIComponent(frm.doc.name || "");

        var html =
            '<div class="flex align-center flex-wrap" style="gap: var(--padding-sm, 8px);">' +
                "<span>" + esc(__("Magento Order #{0}", [order_ref])) + "</span>" +
                '<span class="indicator-pill ' + color + '">' + esc(status.replace(/_/g, " ")) + "</span>" +
                '<a href="' + map_url + '">' + esc(__("View in Order Map")) + "</a>" +
            "</div>";

        frm.dashboard.set_headline_alert(html);
    }

    // ── Buttons ─────────────────────────────────────────────────────────────

    function add_magento_buttons(frm) {
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
                    var esc = frappe.utils.escape_html;
                    frappe.show_alert({
                        message:
                            "<b>Magento Status:</b> " + esc(data.magento_status || "—") + "<br>" +
                            "<b>Imported on:</b> "   + esc(data.imported_on      || "—") + "<br>" +
                            "<b>Last status sync:</b> " + esc(data.last_status_sync || "—"),
                        indicator: "blue",
                    }, 8);
                }
            );
        }, __("Magento"));
    }
})();
