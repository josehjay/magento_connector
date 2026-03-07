import frappe


def boot_session(bootinfo):
    bootinfo.connector_version = frappe.get_attr(
        "connector.__version__"
    )
