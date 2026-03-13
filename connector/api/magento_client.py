"""
Magento 2 REST API Client
Handles authentication, token refresh, and all API operations.
"""

import time
import frappe
import requests
from datetime import datetime, timedelta


class MagentoAPIError(Exception):
    """Raised when Magento returns a non-2xx response."""
    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class MagentoClient:
    """
    Thin wrapper around the Magento 2 REST API.
    Loads credentials from the 'Magento Settings' singleton.
    Handles token caching and auto-refresh.
    """

    MAX_RETRIES = 3
    RETRY_BACKOFF = 2  # seconds between retries

    def __init__(self):
        settings = frappe.get_single("Magento Settings")
        self.base_url = settings.magento_url.rstrip("/")
        self.store_code = settings.magento_store_code or "default"
        self.api_base = f"{self.base_url}/rest/{self.store_code}/V1"
        self.use_integration_token = bool(settings.use_integration_token)

        if self.use_integration_token:
            self._token = settings.get_password("access_token")
        else:
            self._token = self._get_or_refresh_admin_token(settings)

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        })

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_or_refresh_admin_token(self, settings):
        """Return cached admin token or fetch a fresh one."""
        expiry = settings.token_expiry
        cached = settings.get_password("cached_token") if settings.cached_token else None

        if cached and expiry:
            if isinstance(expiry, str):
                expiry = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S.%f")
            if expiry > datetime.now() + timedelta(minutes=5):
                return cached

        url = f"{self.base_url}/rest/V1/integration/admin/token"
        resp = requests.post(
            url,
            json={
                "username": settings.admin_username,
                "password": settings.get_password("admin_password"),
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise MagentoAPIError(
                f"Failed to obtain admin token: {resp.text}",
                resp.status_code,
                resp.text,
            )
        token = resp.json()

        expiry_dt = datetime.now() + timedelta(hours=4)
        frappe.db.set_single_value("Magento Settings", "cached_token", token)
        frappe.db.set_single_value("Magento Settings", "token_expiry", expiry_dt)
        frappe.db.commit()
        return token

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method, endpoint, data=None, params=None, timeout=None):
        """Execute an HTTP request with retry on rate-limit (429) and timeouts."""
        url = f"{self.api_base}{endpoint}"
        req_timeout = timeout or (10, 120)  # (connect_timeout, read_timeout)
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    json=data,
                    params=params,
                    timeout=req_timeout,
                )
                if resp.status_code == 429:
                    wait = self.RETRY_BACKOFF ** attempt
                    frappe.logger("connector").warning(
                        f"Rate limited by Magento. Waiting {wait}s (attempt {attempt}/{self.MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    raise MagentoAPIError(
                        f"Magento API error [{resp.status_code}]: {resp.text}",
                        resp.status_code,
                        resp.text,
                    )
                return resp.json() if resp.text else {}
            except MagentoAPIError:
                raise
            except requests.RequestException as e:
                if attempt == self.MAX_RETRIES:
                    raise MagentoAPIError(f"Request failed after {self.MAX_RETRIES} attempts: {e}")
                time.sleep(self.RETRY_BACKOFF ** attempt)

    def get(self, endpoint, params=None, timeout=None):
        return self._request("GET", endpoint, params=params, timeout=timeout)

    def post(self, endpoint, data=None):
        return self._request("POST", endpoint, data=data)

    def put(self, endpoint, data=None):
        return self._request("PUT", endpoint, data=data)

    def delete(self, endpoint):
        return self._request("DELETE", endpoint)

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_product(self, sku):
        """GET /V1/products/{sku}"""
        return self.get(f"/products/{requests.utils.quote(sku, safe='')}")

    def create_product(self, payload):
        """POST /V1/products — returns created product dict."""
        return self.post("/products", data={"product": payload})

    def update_product(self, sku, payload):
        """PUT /V1/products/{sku} — returns updated product dict."""
        return self.put(
            f"/products/{requests.utils.quote(sku, safe='')}",
            data={"product": payload},
        )

    def update_product_global_scope(self, sku, payload):
        """
        PUT /rest/all/V1/products/{sku} — update attributes in the 'all' store scope.

        In Magento, 'name' (and other store-view-scoped attributes) are NOT updated
        globally when you PUT to a specific store code endpoint (e.g. /rest/default/).
        Sending the same payload to /rest/all/ ensures the change is applied across
        all store views. Price is a global attribute and does not need this.
        """
        url = f"{self.base_url}/rest/all/V1/products/{requests.utils.quote(sku, safe='')}"
        try:
            resp = self.session.request(
                "PUT",
                url,
                json={"product": payload},
                timeout=60,
            )
            if resp.status_code >= 400:
                frappe.logger("connector").warning(
                    f"Global-scope product update [{resp.status_code}] for {sku}: {resp.text[:300]}"
                )
        except Exception as e:
            frappe.logger("connector").warning(
                f"Global-scope product update request failed for {sku}: {e}"
            )

    def update_stock(self, sku, qty):
        """
        PUT /V1/products/{sku}/stockItems/1
        Sets qty and is_in_stock based on qty > 0.
        """
        qty = max(0, float(qty))
        return self.put(
            f"/products/{requests.utils.quote(sku, safe='')}/stockItems/1",
            data={
                "stockItem": {
                    "qty": qty,
                    "is_in_stock": qty > 0,
                    "manage_stock": True,
                }
            },
        )

    def product_exists(self, sku):
        """Return True if the product SKU exists in Magento."""
        try:
            self.get_product(sku)
            return True
        except MagentoAPIError as e:
            if e.status_code == 404:
                return False
            raise

    def delete_product(self, sku):
        """DELETE /V1/products/{sku} — remove product from Magento (if API supports it)."""
        self._request("DELETE", f"/products/{requests.utils.quote(sku, safe='')}")

    # ------------------------------------------------------------------
    # Configurable products (template = parent, variants = children)
    # ------------------------------------------------------------------

    def add_child_to_configurable(self, parent_sku, child_sku):
        """
        Link a simple product (variant) to a configurable product.
        POST /V1/configurable-products/{parentSku}/child
        """
        self.post(
            f"/configurable-products/{requests.utils.quote(parent_sku, safe='')}/child",
            data={"childSku": child_sku},
        )

    def get_configurable_children(self, parent_sku):
        """
        GET /V1/configurable-products/{parentSku}/children
        Returns list of child product dicts (each has 'sku' etc.).
        """
        try:
            result = self.get(
                f"/configurable-products/{requests.utils.quote(parent_sku, safe='')}/children"
            )
            return result if isinstance(result, list) else []
        except MagentoAPIError as e:
            if e.status_code == 404:
                return []
            raise

    # ------------------------------------------------------------------
    # Product media
    # ------------------------------------------------------------------

    def get_product_media(self, sku):
        """GET /V1/products/{sku}/media — returns list of media entries."""
        return self.get(f"/products/{requests.utils.quote(sku, safe='')}/media")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def get_orders(self, updated_after=None, page=1, page_size=20):
        """
        GET /V1/orders with optional updated_at filter.
        Uses a longer timeout (5 min) because the orders endpoint is
        heavy on large Magento catalogs.
        """
        params = {
            "searchCriteria[pageSize]": page_size,
            "searchCriteria[currentPage]": page,
            "searchCriteria[sortOrders][0][field]": "updated_at",
            "searchCriteria[sortOrders][0][direction]": "ASC",
        }
        if updated_after:
            if isinstance(updated_after, datetime):
                updated_after = updated_after.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(updated_after, str):
                # Strip microseconds / timezone suffix — Magento expects YYYY-MM-DD HH:MM:SS
                updated_after = updated_after.split(".")[0].split("+")[0].strip()
            params["searchCriteria[filterGroups][0][filters][0][field]"] = "updated_at"
            params["searchCriteria[filterGroups][0][filters][0][value]"] = updated_after
            params["searchCriteria[filterGroups][0][filters][0][conditionType]"] = "gt"

        result = self.get("/orders", params=params, timeout=(15, 300))
        return result.get("items", [])

    def get_all_new_orders(self, updated_after=None):
        """Paginate through all orders updated after the given datetime."""
        all_orders = []
        page = 1
        page_size = 20
        while True:
            batch = self.get_orders(updated_after=updated_after, page=page, page_size=page_size)
            if not batch:
                break
            all_orders.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return all_orders

    def update_order_status(self, order_id, status, comment="", notify_customer=False):
        """
        Change the Magento order status and record an informational comment.

        Two-step approach (required because the comments endpoint validates
        that the target status is already in the order's status history):

          Step 1 — POST /V1/orders to patch the order entity's state + status.
                   This forces the transition (e.g. new/pending → processing)
                   without going through Magento's invoice/shipment workflow.
                   Requires Magento_Sales::actions_edit ACL.

          Step 2 — POST /V1/orders/{id}/comments to add a history entry.
                   The status field is included ONLY if step 1 succeeded;
                   otherwise the comment is informational (no status label),
                   which avoids the "status not in history" 400 error.
        """
        logger = frappe.logger("connector")

        # ── Step 1: force state + status transition ────────────────────────
        entity_patched = False
        try:
            self.post(
                "/orders",
                data={
                    "entity": {
                        "entity_id": int(order_id),
                        "state":  status,
                        "status": status,
                    }
                },
            )
            entity_patched = True
            logger.info(
                f"update_order_status: entity patch succeeded for "
                f"order {order_id} → state/status={status}"
            )
        except MagentoAPIError as exc:
            logger.warning(
                f"update_order_status: entity patch failed for order {order_id} "
                f"(HTTP {exc.status_code}): {exc}. "
                f"Will post informational comment without status change."
            )

        # ── Step 2: add history comment ────────────────────────────────────
        comment_body: dict = {
            "comment":               comment,
            "is_customer_notified":  1 if notify_customer else 0,
            "is_visible_on_front":   0,
        }
        # Only include status label when we know the entity is already in that status;
        # otherwise Magento rejects the comment with a 400.
        if entity_patched:
            comment_body["status"] = status

        return self.post(
            f"/orders/{order_id}/comments",
            data={"statusHistory": comment_body},
        )

    def update_order_entity_status(self, order_id, status):
        """
        POST /V1/orders — directly patch an order entity's state and status.
        Low-level helper used by the diagnostic test tool.
        Requires Magento_Sales::actions_edit ACL permission.
        """
        return self.post(
            "/orders",
            data={
                "entity": {
                    "entity_id": int(order_id),
                    "state":  status,
                    "status": status,
                }
            },
        )

    def cancel_order(self, order_id):
        """POST /V1/orders/{id}/cancel"""
        return self.post(f"/orders/{order_id}/cancel")

    def get_order(self, order_id):
        """GET /V1/orders/{id} — returns full order dict including items."""
        return self.get(f"/orders/{order_id}")

    def create_shipment(self, order_id, items, tracks=None, notify=True):
        """
        POST /V1/order/{orderId}/ship — create a Magento shipment.

        items  : [{"order_item_id": int, "qty": float}, ...]
        tracks : [{"track_number": str, "title": str, "carrier_code": str}, ...]
        Returns the new Magento shipment_id (int) on success.

        Note: uses /order/ (singular) not /orders/ — this is the Magento fulfillment path.
        """
        body = {"items": items, "notify": 1 if notify else 0}
        if tracks:
            body["tracks"] = tracks
        return self.post(f"/order/{order_id}/ship", data=body)

    def create_invoice(self, order_id, items, capture=False, notify=False):
        """
        POST /V1/order/{orderId}/invoice — create a Magento invoice.

        items   : [{"order_item_id": int, "qty": float}, ...]
        capture : set True to capture payment online.
        Returns the new Magento invoice_id (int) on success.
        """
        return self.post(
            f"/order/{order_id}/invoice",
            data={
                "items": items,
                "capture": capture,
                "notify": 1 if notify else 0,
            },
        )

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    def get_customer(self, customer_id):
        """GET /V1/customers/{id}"""
        return self.get(f"/customers/{customer_id}")

    # ------------------------------------------------------------------
    # Attribute sets, categories, product attributes (for Item form options)
    # ------------------------------------------------------------------

    def get_attribute_sets(self):
        """
        GET attribute sets for catalog_product (product attribute sets).
        Tries /products/attribute-sets/sets/list first (common in Magento 2), then /eav/attribute-sets/list.
        Returns list of dicts with attribute_set_id, attribute_set_name.
        """
        params = {
            "searchCriteria[pageSize]": 200,
        }
        items = []

        # Try 1: Magento 2 products attribute sets (common endpoint)
        try:
            result = self.get("/products/attribute-sets/sets/list", params=params)
            items = self._parse_attribute_sets_response(result)
        except MagentoAPIError:
            pass

        # Try 2: EAV attribute sets with entity type filter
        if not items:
            try:
                params["searchCriteria[filterGroups][0][filters][0][field]"] = "entity_type_code"
                params["searchCriteria[filterGroups][0][filters][0][value]"] = "catalog_product"
                result = self.get("/eav/attribute-sets/list", params=params)
                items = self._parse_attribute_sets_response(result)
            except MagentoAPIError:
                pass

        # Try 3: Global path (no store code) for some Magento setups
        if not items:
            try:
                base_global = f"{self.base_url}/rest/V1"
                url = f"{base_global}/products/attribute-sets/sets/list"
                resp = self.session.request("GET", url, params={"searchCriteria[pageSize]": 200}, timeout=30)
                if resp.status_code == 200 and resp.text:
                    items = self._parse_attribute_sets_response(resp.json())
                else:
                    url = f"{base_global}/eav/attribute-sets/list"
                    resp = self.session.request("GET", url, params=params, timeout=30)
                    if resp.status_code == 200 and resp.text:
                        items = self._parse_attribute_sets_response(resp.json())
            except Exception:
                pass

        return items

    def _parse_attribute_sets_response(self, result):
        """Parse Magento attribute sets list from various response shapes."""
        if not result:
            return []
        items = result.get("items", result) if isinstance(result, dict) else result
        if not isinstance(items, list):
            items = [items] if items else []
        out = []
        for x in items:
            if not isinstance(x, dict):
                continue
            try:
                aid = x.get("attribute_set_id")
                if aid is None:
                    continue
                name = x.get("attribute_set_name") or x.get("name") or str(aid)
                if isinstance(name, str):
                    name = name[:255]
                else:
                    name = str(aid)
                out.append({"attribute_set_id": int(aid), "attribute_set_name": name})
            except (TypeError, ValueError):
                continue
        return out

    def _flatten_category_tree(self, node, path_prefix=""):
        """Recursively flatten a category node with children_data into a list."""
        out = []
        if not isinstance(node, dict):
            return out
        name = node.get("name", str(node.get("id", "")))
        path = path_prefix + name
        out.append({
            "id": int(node.get("id", 0)),
            "name": name,
            "path": path,
            "level": int(node.get("level", 0)),
        })
        for child in node.get("children_data", []) or []:
            out.extend(self._flatten_category_tree(child, path + " > "))
        return out

    def get_categories(self):
        """
        GET category tree and return flat list of {id, name, path} for dropdowns.
        Uses /V1/categories/list or flattens tree from /V1/categories.
        """
        try:
            params = {"searchCriteria[pageSize]": 500}
            result = self.get("/categories/list", params=params)
        except MagentoAPIError:
            result = None
        if result is not None:
            items = result.get("items", result) if isinstance(result, dict) else result
            if not isinstance(items, list):
                items = [items] if items else []
            out = []
            for x in items:
                if isinstance(x, dict):
                    out.append({
                        "id": int(x.get("id", 0)),
                        "name": x.get("name", str(x.get("id", ""))),
                        "path": x.get("path", ""),
                        "level": int(x.get("level", 0)),
                    })
            if out:
                return out
        try:
            result = self.get("/categories")
            if isinstance(result, dict) and result.get("id") is not None:
                return self._flatten_category_tree(result)
            return self._flatten_category_tree(result) if result else []
        except MagentoAPIError:
            return []

    def get_product_attributes(self):
        """
        GET product attribute codes (for custom attributes dropdown).
        Returns list of dicts with attribute_code, frontend_label.
        """
        params = {"searchCriteria[pageSize]": 500}
        try:
            result = self.get("/products/attributes", params=params)
        except MagentoAPIError:
            return []
        items = result.get("items", result) if isinstance(result, dict) else result
        if not isinstance(items, list):
            items = [items] if items else []
        return [
            {
                "attribute_code": x.get("attribute_code", ""),
                "frontend_label": x.get("default_frontend_label", "") or x.get("attribute_code", ""),
            }
            for x in items
            if x.get("attribute_code")
        ]
