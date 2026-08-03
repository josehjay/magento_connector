// Magento Settings — Item Groups attribute set picker + action buttons

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

    // ── Product map rebuild (read-only Magento) ───────────────────────────

    function format_rebuild_progress_html(prog) {
        if (!prog || prog.status === "idle") {
            return "<p class='text-muted'>" + __("No rebuild in progress.") + "</p>";
        }
        var status = prog.status || "unknown";
        var status_class =
            status === "complete" ? "text-success"
                : status === "failed" ? "text-danger"
                    : status === "stale" ? "text-warning"
                        : "text-info";
        var lines = [
            "<p><strong>" + __("Rebuild progress") + "</strong> " +
                "<span class='" + status_class + "'>(" + status + ")</span></p>",
            "<ul style='margin-bottom:8px'>",
            "<li>" + __("Processed") + ": <strong>" + (prog.processed || 0) + " / " + (prog.total || 0) +
                "</strong> (" + (prog.percent || 0) + "%)</li>",
            "<li>" + __("Mapped") + ": " + (prog.mapped || 0) + "</li>",
            "<li>" + __("Not in Magento") + ": " + (prog.skipped_not_in_magento || 0) + "</li>",
            "<li>" + __("Already mapped") + ": " + (prog.skipped_existing || 0) + "</li>",
            "<li>" + __("Failed") + ": " + (prog.failed || 0) + "</li>",
            "<li>" + __("Remaining") + ": " + (prog.remaining || 0) + "</li>",
            "</ul>",
        ];
        if (prog.message) {
            lines.push("<p class='text-muted' style='font-size:11px'>" + frappe.utils.escape_html(prog.message) + "</p>");
        }
        if (prog.last_error) {
            lines.push("<p class='text-danger' style='font-size:11px'>" +
                __("Last error") + ": " + frappe.utils.escape_html(prog.last_error) + "</p>");
        }
        if (prog.started_at) {
            lines.push("<p class='text-muted' style='font-size:11px'>" +
                __("Started") + ": " + prog.started_at +
                (prog.last_updated ? " · " + __("Updated") + ": " + prog.last_updated : "") +
                "</p>");
        }
        return lines.join("");
    }

    function fetch_rebuild_progress(frm, $target, on_done) {
        frappe.call({
            doc: frm.doc,
            method: "get_product_map_rebuild_progress",
            callback: function (r) {
                if (r.message && $target) {
                    $target.html(format_rebuild_progress_html(r.message));
                }
                if (on_done) on_done(r.message || {});
            },
        });
    }

    function open_product_map_rebuild_dialog(frm) {
        var test_passed = false;
        var progress_timer = null;

        function stop_progress_poll() {
            if (progress_timer) {
                clearInterval(progress_timer);
                progress_timer = null;
            }
        }

        function start_progress_poll($target) {
            stop_progress_poll();
            progress_timer = setInterval(function () {
                fetch_rebuild_progress(frm, $target, function (prog) {
                    if (prog.status !== "running") {
                        stop_progress_poll();
                    }
                });
            }, 5000);
        }

        var precautions_html =
            "<div style='font-size:12px;line-height:1.5'>" +
            "<p><strong>What this does</strong></p>" +
            "<ul>" +
            "<li>Reads existing Magento products by SKU (GET only) — <strong>does not change Magento</strong></li>" +
            "<li>Recreates <em>Magento Product Map</em> rows and Item <em>magento_product_id</em> in ERPNext</li>" +
            "<li>Items already mapped (<em>Synced</em>) are excluded from the rebuild and not sent to Magento</li>" +
            "<li>Only items with <em>Sync to Magento</em> checked are included</li>" +
            "</ul>" +
            "<p><strong>Precautions</strong></p>" +
            "<ul>" +
            "<li>Run <em>Test Connection</em> first</li>" +
            "<li>SKU in Magento must match ERPNext <em>item_code</em> exactly</li>" +
            "<li>Test with 5 items before rebuilding all maps</li>" +
            "<li>This is <strong>not</strong> a product push — use <em>Sync All Products Now</em> only if ERPNext should overwrite Magento data</li>" +
            "<li>Only System Manager can run rebuild</li>" +
            "</ul>" +
            "</div>";

        var d = new frappe.ui.Dialog({
            title: __("Rebuild Product Maps from Magento"),
            size: "large",
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "precautions",
                    options: precautions_html,
                },
                {
                    fieldtype: "Section Break",
                    label: __("Step 1 — Preview"),
                },
                {
                    fieldtype: "HTML",
                    fieldname: "preview_result",
                    options: "<p class='text-muted'>" + __("Click Preview below to see how many items need rebuild.") + "</p>",
                },
                {
                    fieldtype: "Section Break",
                    label: __("Step 2 — Test (sample)"),
                },
                {
                    fieldtype: "Int",
                    fieldname: "sample_size",
                    label: __("Test sample size"),
                    default: 5,
                    description: __("Number of items to rebuild in the pilot test (1–20)."),
                },
                {
                    fieldtype: "Check",
                    fieldname: "dry_run",
                    label: __("Dry run only (no ERPNext changes)"),
                    default: 0,
                    description: __("Check to verify Magento lookups without writing map rows."),
                },
                {
                    fieldtype: "HTML",
                    fieldname: "test_status",
                    options: "<p class='text-muted'>" + __("Run test after preview.") + "</p>",
                },
                {
                    fieldtype: "Section Break",
                    label: __("Progress"),
                },
                {
                    fieldtype: "HTML",
                    fieldname: "rebuild_progress",
                    options: "<p class='text-muted'>" + __("Refresh to see rebuild progress.") + "</p>",
                },
                {
                    fieldtype: "Section Break",
                    label: __("Step 3 — Rebuild all"),
                },
                {
                    fieldtype: "Data",
                    fieldname: "confirm_phrase",
                    label: __('Type "REBUILD MAPS" to confirm full rebuild'),
                    description: __("Required after a successful test run (valid 24 hours)."),
                },
            ],
            primary_action_label: __("Close"),
            primary_action: function () {
                stop_progress_poll();
                d.hide();
            },
        });

        d.onhide = function () {
            stop_progress_poll();
        };

        function run_preview() {
            frappe.call({
                doc: frm.doc,
                method: "preview_product_map_rebuild",
                freeze: true,
                freeze_message: __("Analyzing eligible items…"),
            });
        }

        function run_test() {
            var values = d.get_values();
            if (!values) return;

            var sample_size = parseInt(values.sample_size, 10) || 5;
            if (sample_size < 1 || sample_size > 20) {
                frappe.msgprint(__("Sample size must be between 1 and 20."));
                return;
            }

            var msg = values.dry_run
                ? __("Run dry-run test on {0} item(s)? No ERPNext data will change.", [sample_size])
                : __("Run test rebuild on {0} item(s)? This writes map rows in ERPNext only.", [sample_size]);

            frappe.confirm(msg, function () {
                frappe.call({
                    doc: frm.doc,
                    method: "test_rebuild_product_maps",
                    args: {
                        sample_size: sample_size,
                        dry_run: values.dry_run ? 1 : 0,
                    },
                    freeze: true,
                    freeze_message: __("Testing product map rebuild…"),
                    callback: function (r) {
                        if (r.exc || !r.message) return;
                        var res = r.message;
                        test_passed = !!res.test_passed && !res.dry_run;
                        var s = res.summary || {};
                        var status_html =
                            "<p><strong>" + __("Test summary") + "</strong></p>" +
                            "<ul>" +
                            "<li>" + __("Mapped") + ": " + (s.mapped || 0) + "</li>" +
                            "<li>" + __("Skipped (already mapped)") + ": " + (s.skipped_existing || 0) + "</li>" +
                            "<li>" + __("Skipped (not in Magento)") + ": " + (s.skipped_not_in_magento || 0) + "</li>" +
                            "<li>" + __("Failed") + ": " + (s.failed || 0) + "</li>" +
                            "</ul>";
                        if (res.test_passed && !res.dry_run) {
                            status_html += "<p class='text-success'>" +
                                __("Test passed — full rebuild is unlocked for 24 hours.") + "</p>";
                        } else if (res.dry_run) {
                            status_html += "<p class='text-muted'>" +
                                __("Dry run complete. Uncheck dry run and run again to write maps.") + "</p>";
                        } else if ((s.failed || 0) > 0) {
                            status_html += "<p class='text-danger'>" +
                                __("Test did not pass. Fix failures before rebuilding all maps.") + "</p>";
                        }
                        d.fields_dict.test_status.$wrapper.html(status_html);
                    },
                });
            });
        }

        function run_full_rebuild() {
            frappe.call({
                doc: frm.doc,
                method: "get_product_map_rebuild_status",
                callback: function (status_r) {
                    var status = (status_r.message || {});
                    if (!status.passed && !test_passed) {
                        frappe.msgprint({
                            title: __("Test Required"),
                            message: __(
                                "Run a successful test rebuild (5 items, no failures) before rebuilding all maps."
                            ),
                            indicator: "orange",
                        });
                        return;
                    }

                    var values = d.get_values();
                    if (!values) return;

                    if ((values.confirm_phrase || "").trim() !== "REBUILD MAPS") {
                        frappe.msgprint({
                            title: __("Confirmation Required"),
                            message: __('Type exactly "REBUILD MAPS" in the confirmation field.'),
                            indicator: "orange",
                        });
                        return;
                    }

                    frappe.confirm(
                        __(
                            "Queue a background rebuild for all items missing product maps? " +
                            "Magento is read-only; only ERPNext map rows are created."
                        ),
                        function () {
                            frappe.call({
                                doc: frm.doc,
                                method: "trigger_full_product_map_rebuild",
                                args: { confirm_phrase: values.confirm_phrase.trim() },
                                freeze: true,
                                freeze_message: __("Queueing full rebuild…"),
                                callback: function (r) {
                                    if (!r.exc) {
                                        fetch_rebuild_progress(frm, d.fields_dict.rebuild_progress.$wrapper, function (prog) {
                                            if (prog.status === "running") {
                                                start_progress_poll(d.fields_dict.rebuild_progress.$wrapper);
                                            }
                                        });
                                    }
                                },
                            });
                        }
                    );
                },
            });
        }

        d.show();

        var $footer = d.$wrapper.find(".modal-footer");
        $footer.find(".btn-modal-secondary").remove();
        $('<button type="button" class="btn btn-default btn-sm">' + __("Preview") + "</button>")
            .prependTo($footer)
            .on("click", run_preview);
        $('<button type="button" class="btn btn-default btn-sm">' + __("Refresh Progress") + "</button>")
            .prependTo($footer)
            .on("click", function () {
                fetch_rebuild_progress(frm, d.fields_dict.rebuild_progress.$wrapper, function (prog) {
                    if (prog.status === "running") {
                        start_progress_poll(d.fields_dict.rebuild_progress.$wrapper);
                    } else {
                        stop_progress_poll();
                    }
                });
            });
        $('<button type="button" class="btn btn-warning btn-sm">' + __("Resume Rebuild") + "</button>")
            .insertBefore($footer.find(".btn-modal-primary"))
            .on("click", function () {
                frappe.confirm(
                    __("Resume the rebuild from the last saved position?"),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "resume_product_map_rebuild",
                            freeze: true,
                            freeze_message: __("Resuming rebuild…"),
                            callback: function (r) {
                                if (!r.exc) {
                                    fetch_rebuild_progress(frm, d.fields_dict.rebuild_progress.$wrapper, function (prog) {
                                        if (prog.status === "running") {
                                            start_progress_poll(d.fields_dict.rebuild_progress.$wrapper);
                                        }
                                    });
                                }
                            },
                        });
                    }
                );
            });
        $('<button type="button" class="btn btn-primary btn-sm">' + __("Run Test") + "</button>")
            .insertBefore($footer.find(".btn-modal-primary"))
            .on("click", run_test);
        $('<button type="button" class="btn btn-danger btn-sm">' + __("Rebuild All Maps") + "</button>")
            .insertBefore($footer.find(".btn-modal-primary"))
            .on("click", run_full_rebuild);

        fetch_rebuild_progress(frm, d.fields_dict.rebuild_progress.$wrapper, function (prog) {
            if (prog.status === "running") {
                start_progress_poll(d.fields_dict.rebuild_progress.$wrapper);
            }
        });

        frappe.call({
            doc: frm.doc,
            method: "get_product_map_rebuild_status",
            callback: function (r) {
                if (r.message && r.message.passed) {
                    test_passed = true;
                    d.fields_dict.test_status.$wrapper.html(
                        "<p class='text-success'>" +
                        __("Prior test passed by {0} at {1}. Full rebuild is available.", [
                            r.message.user,
                            r.message.at || "—",
                        ]) +
                        "</p>"
                    );
                }
            },
        });
    }

    // ── Attribute Mapping (ERPNext Item Attribute -> Magento attribute) ────

    function escape_html(s) {
        return frappe.utils.escape_html(s == null ? "" : String(s));
    }

    function render_attribute_rows(rows) {
        if (!rows || !rows.length) {
            return "<tr><td colspan='5' class='text-muted'>" + __("No Item Attributes found.") + "</td></tr>";
        }
        return rows.map(function (row) {
            var status_class =
                row.status === "Synced" ? "text-success"
                    : row.status === "Failed" ? "text-danger"
                        : row.status === "Mapped" ? "text-info"
                            : "text-muted";
            var magento_col = row.mapped
                ? escape_html(row.magento_attribute_code) + (row.mapping_type ? " <span class='text-muted'>(" + escape_html(row.mapping_type) + ")</span>" : "")
                : "<span class='text-muted'>" + __("— not mapped —") + "</span>";
            var value_col = row.numeric_values ? __("numeric (no options)") : (row.value_count + " " + __("value(s)"));

            var actions = "";
            if (!row.mapped) {
                actions =
                    "<button type='button' class='btn btn-xs btn-default attr-map-existing' data-attr='" + escape_html(row.item_attribute) + "'>" +
                    __("Map to Existing") + "</button> " +
                    "<button type='button' class='btn btn-xs btn-primary attr-create-new' data-attr='" + escape_html(row.item_attribute) + "'>" +
                    __("Create New") + "</button>";
            } else {
                actions =
                    "<button type='button' class='btn btn-xs btn-default attr-sync-now' data-attr='" + escape_html(row.item_attribute) + "'>" +
                    __("Sync Options Now") + "</button>";
            }

            var error_row = row.sync_error
                ? "<div class='text-danger' style='font-size:11px'>" + escape_html(row.sync_error) + "</div>"
                : "";

            return (
                "<tr>" +
                "<td>" + escape_html(row.item_attribute) + "</td>" +
                "<td>" + value_col + "</td>" +
                "<td>" + magento_col + "</td>" +
                "<td class='" + status_class + "'>" + escape_html(row.status) + error_row + "</td>" +
                "<td>" + actions + "</td>" +
                "</tr>"
            );
        }).join("");
    }

    function load_attribute_overview(frm, $wrapper) {
        $wrapper.html("<p class='text-muted'>" + __("Loading…") + "</p>");
        frappe.call({
            doc: frm.doc,
            method: "get_attribute_map_overview",
            callback: function (r) {
                var rows = r.message || [];
                $wrapper.html(
                    "<table class='table table-bordered' style='font-size:12px'>" +
                    "<thead><tr>" +
                    "<th>" + __("ERPNext Item Attribute") + "</th>" +
                    "<th>" + __("Values") + "</th>" +
                    "<th>" + __("Magento Attribute") + "</th>" +
                    "<th>" + __("Status") + "</th>" +
                    "<th>" + __("Actions") + "</th>" +
                    "</tr></thead><tbody>" +
                    render_attribute_rows(rows) +
                    "</tbody></table>"
                );
            },
        });
    }

    function prompt_map_to_existing(frm, item_attribute, on_done) {
        frappe.call({
            doc: frm.doc,
            method: "get_magento_attributes_for_mapping",
            freeze: true,
            freeze_message: __("Loading Magento attributes…"),
            callback: function (r) {
                var res = r.message || {};
                if (!res.ok || !res.items || !res.items.length) {
                    frappe.msgprint({
                        message: __("Could not load Magento attributes: {0}", [res.error || __("unknown error")]),
                        indicator: "orange",
                    });
                    return;
                }
                var options = "\n" + res.items.map(function (a) {
                    return a.attribute_code + "|" + a.attribute_code + " — " + (a.frontend_label || a.attribute_code);
                }).join("\n");

                var d = new frappe.ui.Dialog({
                    title: __("Map {0} to an Existing Magento Attribute", [item_attribute]),
                    fields: [
                        {
                            fieldtype: "Select",
                            fieldname: "magento_attribute",
                            label: __("Magento Attribute"),
                            options: options,
                            reqd: 1,
                            description: __("Choose an attribute that already exists in Magento to avoid creating a duplicate."),
                        },
                    ],
                    primary_action_label: __("Map"),
                    primary_action: function (values) {
                        var code = String(values.magento_attribute || "").split("|")[0];
                        if (!code) return;
                        d.hide();
                        frappe.call({
                            doc: frm.doc,
                            method: "map_item_attribute_to_existing",
                            args: { item_attribute: item_attribute, magento_attribute_code: code },
                            freeze: true,
                            freeze_message: __("Mapping attribute and syncing options…"),
                            callback: function () {
                                if (on_done) on_done();
                            },
                        });
                    },
                });
                d.show();
            },
        });
    }

    function confirm_create_new(frm, item_attribute, on_done) {
        frappe.confirm(
            __(
                "Create a new Magento attribute for '{0}'? This will POST a new EAV attribute " +
                "(and its current values as options) to Magento. If an attribute with the same " +
                "generated code already exists there, this will map to it instead of duplicating it.",
                [item_attribute]
            ),
            function () {
                frappe.call({
                    doc: frm.doc,
                    method: "create_item_attribute_in_magento",
                    args: { item_attribute: item_attribute },
                    freeze: true,
                    freeze_message: __("Creating attribute in Magento…"),
                    callback: function () {
                        if (on_done) on_done();
                    },
                });
            }
        );
    }

    function sync_now(frm, item_attribute, on_done) {
        frappe.call({
            doc: frm.doc,
            method: "sync_item_attribute_options_now",
            args: { item_attribute: item_attribute },
            freeze: true,
            freeze_message: __("Syncing new options to Magento…"),
            callback: function () {
                if (on_done) on_done();
            },
        });
    }

    function open_attribute_mapping_dialog(frm) {
        var info_html =
            "<div style='font-size:12px;line-height:1.5;margin-bottom:8px'>" +
            "<p><strong>" + __("How this works") + "</strong></p>" +
            "<ul>" +
            "<li>" + __("Unmapped attributes are never touched automatically — map or create them here deliberately.") + "</li>" +
            "<li>" + __("\"Map to Existing\" links to a Magento attribute that already exists — nothing is created, avoiding duplicates.") + "</li>" +
            "<li>" + __("\"Create New\" creates a brand-new Magento attribute (guarded against accidental duplicates by code).") + "</li>" +
            "<li>" + __("Once mapped, new ERPNext values sync automatically (hourly) and via \"Sync Options Now\" — always additive, never deleting an existing option or attribute.") + "</li>" +
            "</ul>" +
            "</div>";

        var d = new frappe.ui.Dialog({
            title: __("Map / Create Magento Attributes"),
            size: "extra-large",
            fields: [
                { fieldtype: "HTML", fieldname: "info", options: info_html },
                { fieldtype: "HTML", fieldname: "attribute_table", options: "<p class='text-muted'>" + __("Loading…") + "</p>" },
            ],
            primary_action_label: __("Close"),
            primary_action: function () { d.hide(); },
        });

        var $wrapper = d.fields_dict.attribute_table.$wrapper;

        function refresh() {
            load_attribute_overview(frm, $wrapper);
        }

        $wrapper.on("click", ".attr-map-existing", function () {
            prompt_map_to_existing(frm, $(this).data("attr"), refresh);
        });
        $wrapper.on("click", ".attr-create-new", function () {
            confirm_create_new(frm, $(this).data("attr"), refresh);
        });
        $wrapper.on("click", ".attr-sync-now", function () {
            sync_now(frm, $(this).data("attr"), refresh);
        });

        d.show();
        refresh();

        var $footer = d.$wrapper.find(".modal-footer");
        $('<button type="button" class="btn btn-default btn-sm">' + __("Refresh") + "</button>")
            .prependTo($footer)
            .on("click", refresh);
        $('<button type="button" class="btn btn-default btn-sm">' + __("Sync All Mapped Now") + "</button>")
            .insertBefore($footer.find(".btn-modal-primary"))
            .on("click", function () {
                frappe.call({
                    doc: frm.doc,
                    method: "sync_all_attribute_options_now",
                    callback: refresh,
                });
            });
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

            frm.add_custom_button(__("Rebuild Product Maps"), function () {
                open_product_map_rebuild_dialog(frm);
            }, __("Products"));

            frm.add_custom_button(__("Map Rebuild Progress"), function () {
                frappe.call({
                    doc: frm.doc,
                    method: "get_product_map_rebuild_progress",
                    callback: function (r) {
                        var html = format_rebuild_progress_html(r.message || {});
                        frappe.msgprint({
                            title: __("Product Map Rebuild Progress"),
                            message: html,
                            wide: true,
                        });
                    },
                });
            }, __("Products"));

            frm.add_custom_button(__("Map / Create Attributes"), function () {
                open_attribute_mapping_dialog(frm);
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
            frm.add_custom_button(__("Clean Sync Logs Now"), function () {
                var allDays = cint(frm.doc.sync_log_retention_days) || 30;
                var successDays = cint(frm.doc.success_log_retention_days) || 7;
                frappe.confirm(
                    __(
                        "Run Magento Sync Log cleanup using current settings?<br><br>" +
                        "• Scrub request/response payloads from Success rows<br>" +
                        "• Delete Success/Skipped older than {0} day(s)<br>" +
                        "• Delete all statuses older than {1} day(s)<br><br>" +
                        "Save Magento Settings first if you just changed retention values.",
                        [successDays, allDays]
                    ),
                    function () {
                        frappe.call({
                            doc: frm.doc,
                            method: "purge_old_logs",
                            freeze: true,
                            freeze_message: __("Cleaning Magento Sync Logs…"),
                        });
                    }
                );
            }, __("Maintenance"));
        },
    });
})();
