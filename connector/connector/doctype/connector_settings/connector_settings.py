import frappe
from frappe.model.document import Document


class ConnectorSettings(Document):
    def validate(self):
        if self.enforce_signed_push_verification and not self.enable_signed_push_verification:
            frappe.throw("Enable signed push verification before enabling strict enforcement.")

        if self.enable_signed_push_verification and not self.get_password("signed_push_secret"):
            frappe.throw("Shared signing secret is required when signed push verification is enabled.")

        if self.signature_tolerance_seconds and int(self.signature_tolerance_seconds) < 60:
            frappe.throw("Signature time window must be at least 60 seconds.")
