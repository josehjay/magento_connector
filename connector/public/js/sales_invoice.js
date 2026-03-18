// Sales Invoice — Magento payment pre-fill
//
// Adds a "Fetch Magento Payment" button inside the standard "Make" dropdown
// on submitted Sales Invoices that still have an outstanding balance.
// The button fetches the Magento order's recorded payment details, shows a
// review dialog pre-filled with those values, and on confirmation creates a
// draft Payment Entry ready for the user to submit.

frappe.ui.form.on("Sales Invoice", {
    refresh: function (frm) {
        // Only on submitted invoices with an outstanding balance
        if (frm.doc.docstatus !== 1) return;
        if ((frm.doc.outstanding_amount || 0) <= 0) return;

        var sales_orders = _linked_sales_orders(frm);
        if (!sales_orders.length) return;

        // Show "Fetch Magento Payment" only when at least one linked SO is Magento-originated.
        frappe.db.get_list("Sales Order", {
            fields: ["name"],
            filters: [
                ["name", "in", sales_orders],
                ["magento_order_id", "is", "set"],
            ],
            limit: 1,
        }).then(function (rows) {
            if (!rows || !rows.length) return;
            frm.add_custom_button(__("Fetch Magento Payment"), function () {
                _fetch_and_open_dialog(frm);
            }, __("Make"));
        });
    },
});

function _linked_sales_orders(frm) {
    var unique = Object.create(null);
    (frm.doc.items || []).forEach(function (item) {
        if (item.sales_order) {
            unique[item.sales_order] = true;
        }
    });
    return Object.keys(unique);
}

// ── Step 1: call server ──────────────────────────────────────────────────────

function _fetch_and_open_dialog(frm) {
    frappe.call({
        method: "connector.sync.payment_sync.get_magento_payment_details",
        args: { sales_invoice: frm.doc.name },
        freeze: true,
        freeze_message: __("Fetching payment details from Magento…"),
        callback: function (r) {
            var data = r && r.message;
            if (!data) {
                frappe.msgprint({
                    title: __("Magento Payment"),
                    message: __("No response from server."),
                    indicator: "red",
                });
                return;
            }
            if (!data.ok) {
                frappe.show_alert({
                    message: data.reason || __("Could not fetch payment details from Magento."),
                    indicator: "orange",
                }, 8);
                return;
            }
            _open_review_dialog(frm, data);
        },
    });
}

// ── Step 2: review dialog ────────────────────────────────────────────────────

function _open_review_dialog(frm, payment) {
    var d = new frappe.ui.Dialog({
        title: __("Review Magento Payment Details"),
        size: "large",
        fields: [
            // ── Info section (read-only) ──────────────────────────────────
            {
                fieldtype: "Section Break",
                label: __("Magento Order Info"),
                collapsible: 0,
            },
            {
                fieldtype: "Data",
                fieldname: "magento_order_ref",
                label: __("Magento Order #"),
                default: payment.increment_id || String(payment.magento_order_id),
                read_only: 1,
            },
            {
                fieldtype: "Data",
                fieldname: "payment_method_display",
                label: __("Payment Method"),
                default: payment.method_label || payment.method_code || __("(unknown)"),
                read_only: 1,
            },
            {
                fieldtype: "Column Break",
            },
            {
                fieldtype: "Currency",
                fieldname: "magento_paid_amount",
                label: __("Amount Recorded in Magento"),
                default: payment.paid_amount,
                read_only: 1,
                options: payment.currency,
            },
            {
                fieldtype: "Currency",
                fieldname: "erpnext_outstanding",
                label: __("Outstanding in ERPNext"),
                default: payment.outstanding_amount,
                read_only: 1,
                options: frm.doc.currency,
            },

            // ── Editable payment entry fields ─────────────────────────────
            {
                fieldtype: "Section Break",
                label: __("Payment Entry Details"),
                description: __(
                    "Review and adjust these values before creating the Payment Entry."
                ),
            },
            {
                fieldtype: "Link",
                fieldname: "mode_of_payment",
                label: __("Mode of Payment"),
                options: "Mode of Payment",
                default: payment.mode_of_payment || "",
                reqd: 1,
                description: __(
                    payment.mode_of_payment
                        ? "Auto-mapped from Magento payment method — change if needed."
                        : "Could not auto-map the Magento method. Please select manually."
                ),
            },
            {
                fieldtype: "Currency",
                fieldname: "paid_amount",
                label: __("Amount to Receive"),
                default: payment.suggested_amount,
                reqd: 1,
                options: frm.doc.currency,
                description: __("Adjust for partial payments."),
            },
            {
                fieldtype: "Column Break",
            },
            {
                fieldtype: "Data",
                fieldname: "reference_no",
                label: __("Reference / Transaction #"),
                default: payment.reference_no || "",
                reqd: 1,
                description: __("Transaction ID, cheque number, or Magento order number."),
            },
            {
                fieldtype: "Date",
                fieldname: "reference_date",
                label: __("Reference Date"),
                default: payment.reference_date || frappe.datetime.get_today(),
                reqd: 1,
            },
            {
                fieldtype: "Section Break",
            },
            {
                fieldtype: "Small Text",
                fieldname: "remarks",
                label: __("Remarks"),
                default: payment.remarks || "",
            },
        ],

        primary_action_label: __("Create Payment Entry"),
        primary_action: function (values) {
            d.hide();
            _create_payment_entry(frm, values);
        },

        secondary_action_label: __("Cancel"),
        secondary_action: function () {
            d.hide();
        },
    });

    d.show();

    // Warn if Magento paid more than what ERPNext has outstanding
    if (payment.paid_amount > payment.outstanding_amount + 0.01) {
        frappe.show_alert({
            message: __(
                "Note: Magento recorded {0} but ERPNext outstanding is {1}. " +
                "The amount has been capped at the outstanding balance.",
                [
                    format_currency(payment.paid_amount, frm.doc.currency),
                    format_currency(payment.outstanding_amount, frm.doc.currency),
                ]
            ),
            indicator: "blue",
        }, 10);
    }
}

// ── Step 3: create Payment Entry ─────────────────────────────────────────────

function _create_payment_entry(frm, values) {
    // Use ERPNext's built-in helper to get a properly populated PE document
    frappe.call({
        method: "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
        args: {
            dt: "Sales Invoice",
            dn: frm.doc.name,
        },
        freeze: true,
        freeze_message: __("Preparing Payment Entry…"),
        callback: function (r) {
            if (r.exc || !r.message) {
                frappe.msgprint({
                    title: __("Error"),
                    message: __("Could not prepare the Payment Entry. Please create it manually."),
                    indicator: "red",
                });
                return;
            }

            var doc = r.message;

            // Overlay with the values the user confirmed in the dialog
            doc.mode_of_payment = values.mode_of_payment;
            doc.paid_amount      = values.paid_amount;
            doc.received_amount  = values.paid_amount;  // same account currency assumed
            doc.reference_no     = values.reference_no;
            doc.reference_date   = values.reference_date;
            doc.remarks          = values.remarks;

            // Sync into the local model cache and navigate to the new form
            var synced = frappe.model.sync(doc);
            if (synced && synced.length) {
                frappe.set_route("Form", synced[0].doctype, synced[0].name);
            } else {
                frappe.msgprint({
                    title: __("Error"),
                    message: __("Payment Entry was prepared but could not be opened automatically."),
                    indicator: "orange",
                });
            }
        },
    });
}
