# ERPNext Connector

A Frappe/ERPNext custom app that provides **modular integration** capabilities:

- **Magento 2** — bidirectional sync of products, orders, inventory, images, and status
- **ERPNext Site Sync** — push products from this ERPNext instance to one or more remote ERPNext sites

Each integration module can be independently enabled or disabled from **Connector Settings**.

## What Gets Synced

### Magento Integration

| Data | Direction | Trigger |
|------|-----------|---------|
| Products (name, price, description, status, weight) | ERPNext → Magento | Real-time on Item save + hourly batch catch-up + every-30-min Pending/Failed map check |
| Product disables | ERPNext → Magento | Disable Magento immediately; remove Magento product within ~30 minutes |
| Product deletes | ERPNext → Magento | Real-time on Item trash (ERPNext is source of truth) |
| Inventory (sum of all warehouses) | ERPNext → Magento | Every 15 minutes |
| Base product image URL | Magento → ERPNext (`item_image`) | Every 30 minutes |
| Orders (new + updated) | Magento → ERPNext (Draft SO) | Every 10 minutes |
| Customers + Shipping Addresses | Magento → ERPNext | During order sync |
| Order status (submit/cancel) | ERPNext → Magento | Real-time on SO submit/cancel |
| Order status changes (complete/cancel) | Magento → ERPNext | Every 10 minutes |

### ERPNext Site Sync

| Data | Direction | Trigger |
|------|-----------|---------|
| Products (item_code, name, description, group, price, UOM, weight) | Local ERPNext → Remote ERPNext | Real-time on Item save + hourly catch-up |

> **Note:** ERPNext site sync currently supports push only. Pull from remote sites can be added in a future release.

---

## Installation / update

### 1. Get the app (first-time)

```bash
cd /path/to/frappe-bench

# From a local path:
bench get-app connector /path/to/magento_connector

# Or from git (once published):
bench get-app connector https://github.com/yourorg/connector
```

### 2. Install on your site (first-time)

```bash
bench --site <your-site> install-app connector
bench --site <your-site> migrate
bench --site <your-site> clear-cache
bench restart
```

### Update only this app (already installed)

`bench update` refreshes **all** apps. To update **only** `connector`:

```bash
cd /path/to/frappe-bench

# 1) Pull latest code for this app only
cd apps/connector
git pull
cd ../..

# 2) Reinstall Python package for this app (picks up new modules)
bench setup requirements connector
# or: ./env/bin/pip install -e apps/connector --quiet

# 3) Run patches / schema for this site
bench --site <your-site> migrate

# 4) Clear cache and reload processes
bench --site <your-site> clear-cache
bench restart
```

If the bench uses a specific branch:

```bash
cd apps/connector
git fetch origin
git checkout <branch>
git pull origin <branch>
cd ../..
```

Notes:

- `migrate` alone does **not** download new commits — you must `git pull` inside `apps/connector` first.
- Skip `bench setup requirements` if you only changed Python inside the app and did not change dependencies.
- Hard-refresh the browser (Ctrl+Shift+R) after updates if desk pages look stale.

### Automatic versioning

Every `git push` / remote sync bumps the patch version (`connector/__init__.py` and `setup.py`) and commits `chore: bump version to X.Y.Z`.

After cloning, enable the git hook and one-step push alias once:

```bash
python scripts/install_git_hooks.py
```

That sets `core.hooksPath=.githooks` and `alias.push` so a single `git push` bumps then publishes with a normal success exit code.

Skip a bump when needed: `SKIP_VERSION_BUMP=1 git push`.

### 3. Magento side setup (one-time, only if using Magento integration)
1. In Magento Admin → **System → Extensions → Integrations**
2. Click **Add New Integration**
3. Give it a name (e.g. "ERPNext Connector")
4. Under **API**, grant permissions for: **Catalog**, **Inventory**, **Sales**, **Customers**
5. Click **Save** then **Activate**
6. Copy the **Access Token**

For Magento **2.4.4+**, run this command on your Magento server to allow integration tokens as bearer:
```bash
bin/magento config:set oauth/consumer/enable_integration_as_bearer 1
bin/magento cache:flush
```

---

## Configuration

### Connector Settings (master switches)

Go to **Connector Settings** in the desk search bar:
- **Enable Magento Integration** — check to activate all Magento sync features
- **Enable ERPNext Site Sync** — check to activate product sync to remote ERPNext sites

### Magento Settings

1. Go to **Magento Settings**
2. Fill in:
   - **Magento Store URL** — e.g. `https://mystore.com`
   - **Integration Access Token** — paste from Magento Integrations
   - **ERPNext Price List** — select which price list to push to Magento
   - **Enable Sync** — check to activate
3. Click **Actions → Test Connection** to verify
4. Click **Actions → Sync All Products Now** for initial bulk push
5. Click **Actions → Sync Orders Now** to pull existing orders

### Remote ERPNext Sites

1. Go to **Remote ERPNext Site** list
2. Click **+ Add Remote ERPNext Site**
3. Fill in:
   - **Site Name** — a friendly label (e.g. "Production ERP")
   - **Site URL** — e.g. `https://erp.example.com`
   - **API Key** and **API Secret** — from the remote site's user API Access settings
   - **Enable Sync** — check to activate sync to this site
   - **Price List** — optional, to use a specific price list for product prices
4. Click **Test Connection** to verify

---

## Usage

### Products (Magento)
- Create/update Items in ERPNext normally
- Check **Sync to Magento** on the Item form (defaults to checked)
- The item is automatically pushed to Magento on save
- Use the **Magento → Push to Magento** button for manual push
- Deleting an Item in ERPNext deletes (or disables) it in Magento and clears the map
- Disabling an Item skips all further sync; Magento is disabled immediately, then removed on the next 30-minute cleanup
- Catch-up runs **hourly in batches** (Pending / Failed / unsynced). Every **30 minutes** the Magento Product Map is re-checked for Pending and Failed rows
- Open **Connector** in the desk sidebar for **Magento Product Map**, settings, and sync logs

### Products (ERPNext Sites)
- Check **Sync to ERPNext Sites** on the Item form
- The item is automatically pushed to all enabled remote sites on save
- Use the **ERPNext Sync → Push to ERPNext Sites** button for manual push
- Monitor sync status in the **Remote Site Product Map** list

### Inventory
- Update stock via ERPNext Stock Entry, Purchase Receipt, etc. as normal
- Magento stock is automatically updated every 15 minutes

### Orders
- New Magento orders appear in ERPNext as **Draft Sales Orders** every 10 minutes
- Review and submit them manually in ERPNext
- Submitting the Sales Order notifies Magento the order is "processing"
- Cancelling the Sales Order cancels it in Magento

### Images
- Manage product images and categories in Magento Admin
- The base product image URL is automatically synced back to ERPNext every 30 minutes

---

## Monitoring

- **Connector** workspace (desk sidebar) — Magento Product Map, Magento Settings, sync logs
- **Magento Sync Log** — operation history (Success logs omit bulky payloads by default)
- **Sync Log Cleanup** (Magento Settings) — retention defaults: Success 7 days, all statuses 30 days; daily job at 02:30; **Maintenance → Clean Sync Logs Now**
- **Remote Site Product Map** — per-item per-site sync status and last sync time
- **Magento Product Map** — per-item Magento sync status (filter by Pending / Failed / Synced)
- **Scheduled Job Log** — Frappe's built-in scheduler log
- **Error Log** — Frappe's error log for unhandled exceptions

---

## File Structure

```
connector/
├── setup.py
├── pyproject.toml
├── requirements.txt
├── connector/
│   ├── hooks.py                    ← doc_events + scheduler_events
│   ├── tasks.py                    ← scheduler entry points
│   ├── connector/
│   │   └── doctype/
│   │       ├── connector_settings/ ← enable/disable integration modules
│   │       ├── magento_settings/   ← Magento credentials + config
│   │       ├── magento_sync_log/   ← audit log for all sync ops
│   │       ├── magento_product_map/ ← item ↔ Magento product ID
│   │       ├── magento_order_map/  ← Magento order ↔ Sales Order
│   │       ├── remote_erpnext_site/ ← remote ERPNext site credentials
│   │       └── remote_site_product_map/ ← item ↔ remote site mapping
│   ├── api/
│   │   ├── magento_client.py       ← Magento 2 HTTP client
│   │   └── erpnext_client.py       ← Remote ERPNext HTTP client
│   ├── sync/
│   │   ├── product_sync.py         ← ERPNext → Magento products
│   │   ├── inventory_sync.py       ← ERPNext → Magento stock
│   │   ├── order_sync.py           ← Magento → ERPNext orders
│   │   ├── customer_sync.py        ← Magento → ERPNext customers
│   │   ├── image_sync.py           ← Magento → ERPNext images
│   │   ├── status_sync.py          ← ERPNext ↔ Magento order status
│   │   └── erpnext_product_sync.py ← ERPNext → Remote ERPNext items
│   ├── fixtures/
│   │   └── custom_field.json       ← injected fields on Item, SO, Customer
│   └── public/js/
│       ├── magento_settings.js
│       └── item.js
```

---

## Running Tests

```bash
bench run-tests --app connector
```

---

## Troubleshooting

| Problem | Solution |
|---------|---------|
| "Connection failed" on Magento Test Connection | Check URL has no trailing slash, verify access token |
| Products not appearing in Magento | Check Item has "Sync to Magento" checked; check Magento Sync Log for errors |
| Orders not importing | Verify sync is enabled; check Last Order Sync Time in settings; check Magento Sync Log |
| Wrong prices pushed | Verify the correct Price List is selected in Magento Settings and item prices exist for that list |
| Images not syncing | Ensure images are uploaded in Magento first; check the item is in Magento Product Map |
| Magento 2.4.4 token error | Run `bin/magento config:set oauth/consumer/enable_integration_as_bearer 1` |
| Remote ERPNext connection failed | Verify Site URL, API Key, and API Secret; ensure the remote user has Item read/write permissions |
| Items not syncing to remote sites | Check "Sync to ERPNext Sites" on the Item; check Connector Settings has ERPNext sync enabled; check Remote Site Product Map for errors |
