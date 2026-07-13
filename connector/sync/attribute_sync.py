"""
Attribute Sync: ERPNext Item Attribute -> Magento Product Attribute (EAV)

Lets a user either MAP an ERPNext Item Attribute onto a Magento attribute
that already exists (avoiding a duplicate), or explicitly CREATE a new one
in Magento when no suitable match exists. Mapping/creation is always a
deliberate, manual action driven from Magento Settings — nothing here
creates a Magento attribute implicitly.

Once an attribute is mapped, pushing newly-added ERPNext values is safe to
automate: adding an option to an existing Magento attribute is purely
additive. This module NEVER deletes or edits an existing Magento attribute
option, and never deletes a Magento attribute — it only adds attributes and
options that don't already exist there.

Triggered by:
  - Item Attribute.on_update      (real-time — only for ALREADY-mapped attributes)
  - tasks.sync_attribute_options  (hourly catch-up, all mapped attributes)
  - Manual "Map" / "Create" / "Sync Now" actions from Magento Settings
"""

import re

import frappe
from connector.api.magento_client import MagentoClient, MagentoAPIError
from connector.connector.doctype.magento_attribute_mapping.magento_attribute_mapping import (
    get_map,
    upsert_map,
)
from connector.connector.doctype.magento_sync_log.magento_sync_log import create_log

# Magento EAV attribute codes: must start with a letter, only [a-z0-9_], max 30 chars.
ATTRIBUTE_CODE_MAX_LEN = 30
_INVALID_CODE_CHARS = re.compile(r"[^a-z0-9_]+")
_REPEAT_UNDERSCORES = re.compile(r"_+")


def _is_magento_enabled():
    try:
        return bool(frappe.db.get_single_value("Connector Settings", "enable_magento_integration"))
    except Exception:
        return True


def _is_sync_enabled():
    if not _is_magento_enabled():
        return False
    return bool(frappe.db.get_single_value("Magento Settings", "sync_enabled"))


def _slugify_attribute_code(item_attribute):
    """Derive a valid Magento attribute_code from an ERPNext Item Attribute name."""
    slug = _INVALID_CODE_CHARS.sub("_", (item_attribute or "").strip().lower())
    slug = _REPEAT_UNDERSCORES.sub("_", slug).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"erp_{slug}" if slug else "erp_attribute"
    slug = slug[:ATTRIBUTE_CODE_MAX_LEN].rstrip("_")
    return slug or "erp_attribute"


def _get_item_attribute_values(item_attribute):
    """Return ordered list of value labels defined in ERPNext for this Item Attribute."""
    rows = frappe.get_all(
        "Item Attribute Value",
        filters={"parent": item_attribute, "parenttype": "Item Attribute"},
        fields=["attribute_value"],
        order_by="idx asc",
    )
    return [row["attribute_value"] for row in rows if row.get("attribute_value")]


def _is_numeric_attribute(item_attribute):
    return bool(frappe.db.get_value("Item Attribute", item_attribute, "numeric_values"))


def _get_configured_attribute_set_ids():
    """Distinct Magento attribute_set_ids configured in Magento Settings' item groups."""
    settings = frappe.get_single("Magento Settings")
    ids = set()
    for row in settings.magento_item_groups or []:
        if row.get("attribute_set_id"):
            try:
                ids.add(int(row.attribute_set_id))
            except (TypeError, ValueError):
                continue
    return ids


def _ensure_attribute_in_configured_sets(client, attribute_code):
    """
    Add the attribute to every attribute set configured in Magento Settings,
    so it's actually usable/selectable on products in those sets.

    Additive only — this never removes the attribute from any set or group.
    Magento's "already assigned" error is treated as a no-op success. Each
    set is attempted independently so one failure doesn't block the others.
    """
    for attribute_set_id in _get_configured_attribute_set_ids():
        try:
            groups = client.get_attribute_set_groups(attribute_set_id)
            if not groups:
                continue
            group_id = groups[0].get("attribute_group_id")
            if not group_id:
                continue
            client.add_attribute_to_set(attribute_set_id, group_id, attribute_code)
        except MagentoAPIError as e:
            body = (e.response_body or "").lower()
            if "already" in body or "assigned" in body:
                continue
            frappe.logger("connector").warning(
                f"_ensure_attribute_in_configured_sets: could not add '{attribute_code}' "
                f"to set {attribute_set_id}: {e}"
            )
        except Exception as e:
            frappe.logger("connector").warning(
                f"_ensure_attribute_in_configured_sets: unexpected error adding '{attribute_code}' "
                f"to set {attribute_set_id}: {e}"
            )


# ---------------------------------------------------------------------------
# Overview / discovery (for the mapping UI in Magento Settings)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_attribute_map_overview():
    """
    Return every ERPNext Item Attribute with its current Magento mapping
    state, for the Attribute Mapping dialog in Magento Settings.
    """
    attributes = frappe.get_all(
        "Item Attribute",
        fields=["name", "numeric_values"],
        order_by="name asc",
    )
    maps = {
        row["item_attribute"]: row
        for row in frappe.get_all(
            "Magento Attribute Mapping",
            fields=[
                "item_attribute", "magento_attribute_code", "magento_attribute_id",
                "frontend_label", "mapping_type", "status", "last_synced_on", "sync_error",
            ],
        )
    }

    out = []
    for attr in attributes:
        name = attr["name"]
        mapping = maps.get(name) or {}
        is_numeric = bool(attr.get("numeric_values"))
        value_count = 0 if is_numeric else len(_get_item_attribute_values(name))
        out.append({
            "item_attribute": name,
            "numeric_values": is_numeric,
            "value_count": value_count,
            "mapped": bool(mapping.get("magento_attribute_code")),
            "magento_attribute_code": mapping.get("magento_attribute_code"),
            "magento_attribute_id": mapping.get("magento_attribute_id"),
            "mapping_type": mapping.get("mapping_type"),
            "status": mapping.get("status") or "Not Mapped",
            "last_synced_on": mapping.get("last_synced_on"),
            "sync_error": mapping.get("sync_error"),
        })
    return out


@frappe.whitelist()
def get_magento_attributes_for_mapping():
    """
    List existing Magento product attributes so the user can map an ERPNext
    Item Attribute onto one of them instead of creating a duplicate.
    """
    try:
        client = MagentoClient()
        return {"ok": True, "items": client.get_product_attributes()}
    except MagentoAPIError as e:
        return {"ok": False, "error": str(e), "items": []}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Connector Attribute Sync: list Magento attributes")
        return {"ok": False, "error": str(e), "items": []}


# ---------------------------------------------------------------------------
# Map to an existing Magento attribute (nothing is created in Magento)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def map_attribute_to_existing(item_attribute, magento_attribute_code):
    """
    Map an ERPNext Item Attribute to a Magento attribute that already exists.
    Never creates or renames anything in Magento — only records the mapping,
    ensures the attribute is usable on the configured attribute sets, and
    pushes any ERPNext values Magento doesn't already have as new options.
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Manager can map attributes.", frappe.PermissionError)

    if not frappe.db.exists("Item Attribute", item_attribute):
        frappe.throw(f"Item Attribute '{item_attribute}' does not exist.")

    magento_attribute_code = (magento_attribute_code or "").strip()
    if not magento_attribute_code:
        frappe.throw("Please choose a Magento attribute to map to.")

    try:
        client = MagentoClient()
    except Exception as exc:
        frappe.throw(f"Cannot connect to Magento: {exc}")

    try:
        attribute = client.get_attribute(magento_attribute_code)
    except MagentoAPIError as e:
        frappe.throw(f"Could not find Magento attribute '{magento_attribute_code}': {e}")

    upsert_map(
        item_attribute,
        magento_attribute_code,
        magento_attribute_id=attribute.get("attribute_id"),
        frontend_label=attribute.get("default_frontend_label") or magento_attribute_code,
        mapping_type="Mapped to Existing",
        status="Mapped",
    )

    try:
        _ensure_attribute_in_configured_sets(client, magento_attribute_code)
    except Exception:
        frappe.logger("connector").warning(
            f"map_attribute_to_existing: could not ensure '{magento_attribute_code}' is in configured sets."
        )

    result = sync_attribute_options(item_attribute)
    create_log(
        operation="Attribute Map",
        status="Failed" if result.get("failed") else "Success",
        doctype_name="Item Attribute",
        document_name=item_attribute,
        magento_id=magento_attribute_code,
        response_payload=result,
    )
    result["magento_attribute_code"] = magento_attribute_code
    return result


# ---------------------------------------------------------------------------
# Create a brand-new Magento attribute
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_attribute_in_magento(item_attribute):
    """
    Create a new Magento product attribute for this ERPNext Item Attribute.
    Guards against accidental duplicates: if an attribute with the derived
    code already exists in Magento, this maps to it instead of creating a
    second one.
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Manager can create attributes.", frappe.PermissionError)

    if not frappe.db.exists("Item Attribute", item_attribute):
        frappe.throw(f"Item Attribute '{item_attribute}' does not exist.")

    existing_map = get_map(item_attribute)
    if existing_map and existing_map.get("magento_attribute_code"):
        frappe.throw(
            f"'{item_attribute}' is already mapped to Magento attribute "
            f"'{existing_map['magento_attribute_code']}'. Unmap it first if you want to recreate it."
        )

    attribute_code = _slugify_attribute_code(item_attribute)
    is_numeric = _is_numeric_attribute(item_attribute)
    values = [] if is_numeric else _get_item_attribute_values(item_attribute)

    try:
        client = MagentoClient()
    except Exception as exc:
        frappe.throw(f"Cannot connect to Magento: {exc}")

    # Duplicate guard: an attribute with this code may already exist in Magento
    # (created directly there, or a previous run that partially completed).
    try:
        existing_attribute = client.get_attribute(attribute_code)
    except MagentoAPIError as e:
        if e.status_code != 404:
            frappe.throw(f"Error checking for existing Magento attribute: {e}")
        existing_attribute = None

    if existing_attribute:
        frappe.msgprint(
            f"A Magento attribute with code '{attribute_code}' already exists — "
            f"mapping to it instead of creating a duplicate.",
            indicator="orange",
        )
        upsert_map(
            item_attribute,
            attribute_code,
            magento_attribute_id=existing_attribute.get("attribute_id"),
            frontend_label=existing_attribute.get("default_frontend_label") or attribute_code,
            mapping_type="Mapped to Existing",
            status="Mapped",
        )
        _ensure_attribute_in_configured_sets(client, attribute_code)
        result = sync_attribute_options(item_attribute)
        result["magento_attribute_code"] = attribute_code
        result["reused_existing"] = True
        return result

    payload = {
        "attribute_code": attribute_code,
        "frontend_input": "text" if is_numeric else "select",
        "default_frontend_label": item_attribute,
        "is_required": False,
        "is_unique": False,
        "scope": "global",
    }
    if values:
        payload["options"] = [{"label": v} for v in values]

    try:
        created = client.create_attribute(payload)
    except MagentoAPIError as e:
        upsert_map(
            item_attribute, attribute_code, mapping_type="Created New",
            status="Failed", sync_error=str(e)[:500],
        )
        create_log(
            operation="Attribute Map", status="Failed",
            doctype_name="Item Attribute", document_name=item_attribute,
            magento_id=attribute_code, error_message=str(e), request_payload=payload,
        )
        frappe.throw(f"Failed to create Magento attribute: {e}")

    upsert_map(
        item_attribute,
        attribute_code,
        magento_attribute_id=created.get("attribute_id"),
        frontend_label=item_attribute,
        mapping_type="Created New",
        status="Synced",
    )

    _ensure_attribute_in_configured_sets(client, attribute_code)

    create_log(
        operation="Attribute Map", status="Success",
        doctype_name="Item Attribute", document_name=item_attribute,
        magento_id=attribute_code, request_payload=payload, response_payload=created,
    )

    return {
        "magento_attribute_code": attribute_code,
        "magento_attribute_id": created.get("attribute_id"),
        "added": list(values),
        "skipped_existing": 0,
        "failed": 0,
        "message": f"Created Magento attribute '{attribute_code}' with {len(values)} option(s).",
    }


# ---------------------------------------------------------------------------
# Push new values as Magento options (additive-only, safe to automate)
# ---------------------------------------------------------------------------

def sync_attribute_options(item_attribute):
    """
    For an already-mapped Item Attribute, push any ERPNext values that Magento
    doesn't have yet as new attribute options. Never touches, edits, or
    removes an existing Magento option or attribute.
    """
    mapping = get_map(item_attribute)
    if not mapping or not mapping.get("magento_attribute_code"):
        return {"skipped": True, "reason": "not_mapped", "added": [], "skipped_existing": 0, "failed": 0}

    attribute_code = mapping["magento_attribute_code"]

    if _is_numeric_attribute(item_attribute):
        # Numeric attributes have no discrete option list to sync.
        upsert_map(item_attribute, attribute_code, status="Synced")
        return {"skipped": True, "reason": "numeric_attribute", "added": [], "skipped_existing": 0, "failed": 0}

    local_values = _get_item_attribute_values(item_attribute)
    if not local_values:
        return {"added": [], "skipped_existing": 0, "failed": 0}

    try:
        client = MagentoClient()
        existing_options = client.get_attribute_options(attribute_code)
    except Exception as e:
        upsert_map(item_attribute, attribute_code, status="Failed", sync_error=str(e)[:500])
        create_log(
            operation="Attribute Map", status="Failed",
            doctype_name="Item Attribute", document_name=item_attribute,
            magento_id=attribute_code, error_message=str(e),
        )
        return {"added": [], "skipped_existing": 0, "failed": len(local_values), "error": str(e)}

    existing_labels = {
        (opt.get("label") or "").strip().lower()
        for opt in existing_options
        if opt.get("label")
    }

    added, failed = [], 0
    for value in local_values:
        if value.strip().lower() in existing_labels:
            continue
        try:
            client.add_attribute_option(attribute_code, value)
            added.append(value)
            existing_labels.add(value.strip().lower())
        except MagentoAPIError as e:
            failed += 1
            frappe.logger("connector").warning(
                f"sync_attribute_options: failed to add option '{value}' to '{attribute_code}': {e}"
            )

    status = "Failed" if failed and not added else "Synced"
    upsert_map(
        item_attribute, attribute_code,
        status=status,
        sync_error="" if not failed else f"{failed} option(s) failed to sync",
    )

    if added or failed:
        create_log(
            operation="Attribute Map",
            status="Success" if not failed else "Failed",
            doctype_name="Item Attribute",
            document_name=item_attribute,
            magento_id=attribute_code,
            response_payload={"added": added, "failed": failed},
        )

    return {
        "added": added,
        "skipped_existing": len(local_values) - len(added) - failed,
        "failed": failed,
    }


@frappe.whitelist()
def sync_attribute_options_now(item_attribute):
    """Manually trigger an options sync for one mapped attribute (whitelisted wrapper)."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Only System Manager can sync attributes.", frappe.PermissionError)
    return sync_attribute_options(item_attribute)


def sync_all_mapped_attributes():
    """
    Catch-up sweep: push any newly-added ERPNext values for every already
    -mapped attribute. Never touches unmapped attributes — those require a
    deliberate map/create action first.
    """
    if not _is_sync_enabled():
        return

    mapped = frappe.get_all(
        "Magento Attribute Mapping",
        filters={"status": ["in", ["Mapped", "Synced"]]},
        pluck="item_attribute",
    )
    if not mapped:
        return

    success = failed = 0
    for item_attribute in mapped:
        try:
            result = sync_attribute_options(item_attribute)
            if result.get("failed"):
                failed += 1
            else:
                success += 1
        except Exception:
            failed += 1
            frappe.log_error(
                frappe.get_traceback(), f"Connector Attribute Sync Failed: {item_attribute}"
            )

    frappe.logger("connector").info(
        f"sync_all_mapped_attributes: {success} ok, {failed} failed out of {len(mapped)}."
    )


# ---------------------------------------------------------------------------
# Real-time hook — only acts on attributes already mapped
# ---------------------------------------------------------------------------

def on_item_attribute_save(doc, method):
    """
    Hook: Item Attribute on_update.
    Only already-mapped attributes are auto-synced (additive-only, safe).
    Unmapped attributes are left for a deliberate map/create action so a
    Magento attribute is never created implicitly.
    """
    try:
        if not _is_sync_enabled():
            return

        item_attribute = doc.get("name")
        if not item_attribute:
            return

        mapping = get_map(item_attribute)
        if not mapping or not mapping.get("magento_attribute_code"):
            return

        frappe.enqueue(
            "connector.sync.attribute_sync.sync_attribute_options",
            queue="default",
            timeout=120,
            job_id=f"magento_attribute_option_sync_{item_attribute}",
            deduplicate=True,
            enqueue_after_commit=True,
            item_attribute=item_attribute,
        )
    except Exception:
        try:
            frappe.log_error(
                frappe.get_traceback(), "Connector Hook Failed: Item Attribute.on_item_attribute_save"
            )
        except Exception:
            pass
