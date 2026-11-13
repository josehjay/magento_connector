"""
Remote ERPNext REST API Client
Handles authentication and CRUD operations for Items on remote ERPNext sites.
Uses Frappe token-based authentication (API Key + Secret).
"""

import json
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

        # Only reached when every attempt was rate-limited (429); without this
        # the method returned None and callers crashed on result.get().
        raise ERPNextAPIError(
            f"Request to {self.site_name} still rate-limited after {self.MAX_RETRIES} attempts",
            429,
        )

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

    # ------------------------------------------------------------------
    # Item Prices
    # ------------------------------------------------------------------

    def find_item_price(self, item_code, price_list):
        """
        GET /api/resource/Item Price filtered by item_code + price_list +
        selling=1. Returns {"name", "price_list_rate"} or None.
        """
        result = self.get(
            "/api/resource/Item Price",
            params={
                "filters": json.dumps([
                    ["item_code", "=", item_code],
                    ["price_list", "=", price_list],
                    ["selling", "=", 1],
                ]),
                "fields": json.dumps(["name", "price_list_rate"]),
                "limit_page_length": 1,
            },
        )
        rows = result.get("data") if isinstance(result, dict) else None
        return rows[0] if rows else None

    def create_item_price(self, item_code, price_list, rate):
        """POST /api/resource/Item Price — selling price for item on price_list."""
        result = self.post(
            "/api/resource/Item Price",
            data={
                "item_code": item_code,
                "price_list": price_list,
                "price_list_rate": rate,
                "selling": 1,
            },
        )
        return result.get("data", result)

    def update_item_price(self, name, rate):
        """PUT /api/resource/Item Price/{name} — change price_list_rate."""
        result = self.put(
            f"/api/resource/Item Price/{requests.utils.quote(name, safe='')}",
            data={"price_list_rate": rate},
        )
        return result.get("data", result)

    def upsert_item_price(self, item_code, price_list, rate):
        """
        Create or update the remote selling Item Price. Returns "created",
        "updated" or "unchanged". The remote site must already have a Price
        List with the same name, or the create fails with a link error.
        """
        rate = float(rate)
        existing = self.find_item_price(item_code, price_list)
        if existing:
            current = existing.get("price_list_rate")
            if current is not None and abs(float(current) - rate) < 1e-9:
                return "unchanged"
            self.update_item_price(existing["name"], rate)
            return "updated"
        self.create_item_price(item_code, price_list, rate)
        return "created"
