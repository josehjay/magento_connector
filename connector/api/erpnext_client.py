"""
Remote ERPNext REST API Client
Handles authentication and CRUD operations for Items on remote ERPNext sites.
Uses Frappe token-based authentication (API Key + Secret).
"""

import time
import frappe
import requests


class ERPNextAPIError(Exception):
    """Raised when a remote ERPNext site returns a non-2xx response."""
    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ERPNextClient:
    """
    REST client for a remote ERPNext / Frappe site.
    Loads credentials from the 'Remote ERPNext Site' doctype.
    """

    MAX_RETRIES = 3
    RETRY_BACKOFF = 2

    def __init__(self, remote_site_name):
        site = frappe.get_doc("Remote ERPNext Site", remote_site_name)
        self.site_name = remote_site_name
        self.base_url = site.site_url.rstrip("/")
        self._api_key = site.get_password("api_key")
        self._api_secret = site.get_password("api_secret")

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"token {self._api_key}:{self._api_secret}",
        })

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method, endpoint, data=None, params=None):
        """Execute an HTTP request with retry on 429 rate-limiting."""
        url = f"{self.base_url}{endpoint}"
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    json=data,
                    params=params,
                    timeout=60,
                )
                if resp.status_code == 429:
                    wait = self.RETRY_BACKOFF ** attempt
                    frappe.logger("connector").warning(
                        f"Rate limited by {self.site_name}. Waiting {wait}s (attempt {attempt}/{self.MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    raise ERPNextAPIError(
                        f"Remote ERPNext API error [{resp.status_code}]: {resp.text[:500]}",
                        resp.status_code,
                        resp.text,
                    )
                return resp.json() if resp.text else {}
            except ERPNextAPIError:
                raise
            except requests.RequestException as e:
                if attempt == self.MAX_RETRIES:
                    raise ERPNextAPIError(
                        f"Request to {self.site_name} failed after {self.MAX_RETRIES} attempts: {e}"
                    )
                time.sleep(self.RETRY_BACKOFF ** attempt)

    def get(self, endpoint, params=None):
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint, data=None):
        return self._request("POST", endpoint, data=data)

    def put(self, endpoint, data=None):
        return self._request("PUT", endpoint, data=data)

    # ------------------------------------------------------------------
    # Auth verification
    # ------------------------------------------------------------------

    def get_logged_user(self):
        """GET /api/method/frappe.auth.get_logged_user — verifies credentials."""
        result = self.get("/api/method/frappe.auth.get_logged_user")
        return result.get("message", "")

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def get_item(self, item_code):
        """GET /api/resource/Item/{item_code}"""
        result = self.get(f"/api/resource/Item/{requests.utils.quote(item_code, safe='')}")
        return result.get("data", result)

    def create_item(self, payload):
        """POST /api/resource/Item — returns created Item dict."""
        result = self.post("/api/resource/Item", data=payload)
        return result.get("data", result)

    def update_item(self, item_code, payload):
        """PUT /api/resource/Item/{item_code} — returns updated Item dict."""
        result = self.put(
            f"/api/resource/Item/{requests.utils.quote(item_code, safe='')}",
            data=payload,
        )
        return result.get("data", result)

    def item_exists(self, item_code):
        """Return True if the item exists on the remote site."""
        try:
            self.get_item(item_code)
            return True
        except ERPNextAPIError as e:
            if e.status_code == 404:
                return False
            raise
