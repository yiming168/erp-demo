# Project Profile: NorthPeak / Clearwater Biotech ERP

## 1. Project Goal

Lightweight ERP for NorthPeak Biotech (probiotic manufacturer) covering sales, finance, logistics, samples, finished-goods inventory, raw-material inventory, and product formulas.

Single Flask application + single MySQL database. Two accounting entities share it:

- **NorthPeak Biotech** — main business entity
- **Clearwater Health Sciences Co., Ltd.** — shell company used for specific customer accounting

Do **not** split into two ERP systems unless explicitly requested.

> Note: this is a sanitized portfolio demo adapted from a real production ERP. Company/client
> names and demo data are fictional; the architecture and business rules below are real.

## 2. Tech Stack

- Language: Python / Flask
- Database: MySQL (Aiven), SSL required (`ca.pem`)
- DB access: SQLAlchemy with raw SQL via `text()`
- Naming: `snake_case`
- Migration: idempotent startup functions in `app.py` (`ensure_multi_company_schema`, `ensure_inventory_schema`, `ensure_samples_schema`, `migrate_sample_contracts_to_samples`, `migrate_to_unified_batches`). No Alembic.
- Templates: Jinja2, Bootstrap 5, `base.html` inheritance, navbar in `templates/partials/navbar.html`

## 3. Current Database Schema

All tables are created/extended idempotently on startup. Source of truth is `app.py`.

```sql
-- 1. companies (ledger entity / selling entity / invoicing entity)
CREATE TABLE companies (
    company_id   INT AUTO_INCREMENT PRIMARY KEY,
    company_name VARCHAR(100) NOT NULL UNIQUE,
    short_name   VARCHAR(50),
    tax_id       VARCHAR(50),
    company_address TEXT,
    bank_info    TEXT,
    phone        VARCHAR(50),
    is_active    TINYINT(1) DEFAULT 1,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Seed rows: NorthPeak Biotech, Clearwater Health Sciences Co., Ltd.

-- 2. clients (customer companies)
CREATE TABLE clients (
    client_id       INT AUTO_INCREMENT PRIMARY KEY,
    company_name    VARCHAR(100) NOT NULL,
    company_address TEXT,
    tax_id          VARCHAR(50),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Note: Clearwater Health Sciences Co., Ltd. also exists here as a client (it buys from
-- NorthPeak as part of the resale chain -- see section 4).

-- 3. contacts (client contacts)
CREATE TABLE contacts (
    contact_id   INT AUTO_INCREMENT PRIMARY KEY,
    client_id    INT NOT NULL,
    contact_name VARCHAR(50) NOT NULL,
    cellphone    VARCHAR(20),
    title        VARCHAR(50),
    other_info   TEXT NULL,
    FOREIGN KEY (client_id) REFERENCES clients(client_id) ON DELETE CASCADE
);

-- 4. products (sales product master data)
CREATE TABLE products (
    product_id       INT AUTO_INCREMENT PRIMARY KEY,
    chinese_name     VARCHAR(100) NOT NULL,
    process_type     ENUM('Freeze Drying','Spray Drying','Blending Mix','Fluidized Bed Drying') DEFAULT 'Freeze Drying',
    description      TEXT
);
-- Note: the `chinese_name` column is a legacy name kept for backward compatibility --
-- it just holds the product's display name (now in English in this demo).

-- 4b. components (functional ingredients / raw-material master data)
CREATE TABLE components (
    component_id     INT AUTO_INCREMENT PRIMARY KEY,
    component_name   VARCHAR(100) NOT NULL,
    component_type   VARCHAR(20) NOT NULL DEFAULT 'Other',
    default_unit     VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',
    notes            TEXT NULL,
    is_active        TINYINT(1) NOT NULL DEFAULT 1,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_component_name (component_name)
);

-- 5. contracts (sales orders only, no samples)
CREATE TABLE contracts (
    contract_id        INT AUTO_INCREMENT PRIMARY KEY,
    -- added via ensure_column:
    company_id         INT NULL,       -- ledger entity (NorthPeak or Clearwater)
    source_contract_id INT NULL,       -- upstream ZT contract_id for the Clearwater resale chain
    contract_no        VARCHAR(50),
    client_id          INT NOT NULL,
    contact_id         INT,
    order_date         DATE NOT NULL,
    delivery_deadline  DATE,
    payment_method     VARCHAR(50),
    total_amount       DECIMAL(12,2) DEFAULT 0.00,
    shipping_address   TEXT,
    notes              TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(client_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(contact_id)
);

-- 6. items (order line items)
CREATE TABLE items (
    item_id        INT AUTO_INCREMENT PRIMARY KEY,
    contract_id    INT NOT NULL,
    product_id     INT NOT NULL,
    quantity       DECIMAL(10,2) NOT NULL,
    agreed_cfu     DECIMAL(10,2),
    unit_price     DECIMAL(10,2),
    packaging_spec VARCHAR(255) DEFAULT '1 kg/bag',
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

-- 7. shipments (sales shipments)
CREATE TABLE shipments (
    shipment_id      INT AUTO_INCREMENT PRIMARY KEY,
    item_id          INT NOT NULL,
    batch_id         INT NULL,           -- optional link to batches.batch_id (NULL while inventory is being built up)
    quantity_shipped DECIMAL(10,2) NOT NULL,
    tracking_no      VARCHAR(100),
    logistics_company VARCHAR(50),
    coa              VARCHAR(255),
    shipping_date    DATE DEFAULT (CURRENT_DATE),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 8. invoices (sales invoices)
CREATE TABLE invoices (
    invoice_id     INT AUTO_INCREMENT PRIMARY KEY,
    contract_id    INT NOT NULL,
    company_id     INT NULL,
    invoice_no     VARCHAR(100),
    invoice_amount DECIMAL(12,2) NOT NULL,
    invoice_date   DATE NULL,           -- NULL = not yet issued
    remark         TEXT,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

-- 9. receipts (sales payment receipts)
CREATE TABLE receipts (
    receipt_id     INT AUTO_INCREMENT PRIMARY KEY,
    contract_id    INT NOT NULL,
    company_id     INT NULL,
    payment_amount DECIMAL(12,2) NOT NULL,
    payment_date   DATE NULL,           -- NULL = not yet received
    remark         TEXT,
    FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
);

-- 10. shipping_addresses (client shipping addresses)
CREATE TABLE shipping_addresses (
    address_id INT NOT NULL AUTO_INCREMENT,
    client_id  INT NOT NULL,
    address    VARCHAR(255) NOT NULL,
    is_default TINYINT(1) DEFAULT 0,
    PRIMARY KEY (address_id),
    FOREIGN KEY (client_id) REFERENCES clients(client_id) ON DELETE CASCADE
);

-- 11. samples (flat single table, one row per product per sample batch)
CREATE TABLE samples (
    sample_id         INT AUTO_INCREMENT PRIMARY KEY,
    company_id        INT NOT NULL,
    sample_no         VARCHAR(50) NOT NULL,  -- NOT UNIQUE; rows with same sample_no = one batch
    client_id         INT NOT NULL,
    contact_id        INT NULL,
    product_id        INT NOT NULL,
    quantity          DECIMAL(10,3) NOT NULL,
    agreed_cfu        DECIMAL(10,2) NULL,
    packaging_spec    VARCHAR(255) DEFAULT '50g/bag',
    request_date      DATE NOT NULL,
    shipping_address  TEXT,
    shipping_date     DATE NULL,
    tracking_no       VARCHAR(100) NULL,
    logistics_company VARCHAR(50) NULL,
    coa               VARCHAR(255) NULL,
    notes             TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Header fields (company, client, date, address, notes) stored redundantly per row.
-- Edit/delete by sample_no (affects all rows with that sample_no).

-- 12. batches (unified inventory batches -- finished-goods batches + raw-material received lots)
-- batch_type='produced' → finished goods (replaces inventory_batches)
-- batch_type='received' → raw material received lot (replaces raw_material_batches)
-- shipments.batch_id references this table
CREATE TABLE batches (
    batch_id           INT AUTO_INCREMENT PRIMARY KEY,
    product_id         INT NULL,  -- produced finished-goods batch
    component_id       INT NULL,  -- received raw-material/component lot
    batch_no           VARCHAR(50) NOT NULL,
    batch_type         ENUM('produced','received') NOT NULL DEFAULT 'received',
    production_date    DATE NULL,
    received_date      DATE NULL,
    expiry_date        DATE NULL,
    quantity_total     DECIMAL(12,3) NOT NULL DEFAULT 0,
    quantity_available DECIMAL(12,3) NOT NULL DEFAULT 0,
    unit               VARCHAR(20) NOT NULL DEFAULT 'kg',  -- quantity unit; 'kg' for almost everything, but e.g. packaging (foil bags) uses 'pcs'
    actual_potency     DECIMAL(18,4) NULL,
    potency_unit       VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',
    supplier_id        INT NULL,  -- FK to suppliers.supplier_id (replaces old free-text `supplier` column)
    supplier_batch_no  VARCHAR(50) NULL,
    poi_id             INT NULL,  -- optional link to purchase_order_items.poi_id (no FK constraint, mirrors shipments.batch_id optionality)
    unit_cost          DECIMAL(10,2) NULL,  -- purchase unit price snapshot at receiving time
    status             ENUM('available','depleted','rejected') NOT NULL DEFAULT 'available',
    notes              TEXT NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_batch_no (batch_no)
);

-- 15. suppliers (supplier master data)
CREATE TABLE suppliers (
    supplier_id      INT AUTO_INCREMENT PRIMARY KEY,
    supplier_name    VARCHAR(100) NOT NULL,
    supplier_address TEXT NULL,
    tax_id           VARCHAR(50) NULL,
    contact_name     VARCHAR(50) NULL,
    contact_phone    VARCHAR(20) NULL,
    bank_info        TEXT NULL,
    notes            TEXT NULL,
    is_active        TINYINT(1) NOT NULL DEFAULT 1,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_supplier_name (supplier_name)
);

-- 16. purchase_orders (PO header) -- no status column, progress derived like contracts (see §6)
CREATE TABLE purchase_orders (
    po_id          INT AUTO_INCREMENT PRIMARY KEY,
    company_id     INT NULL,        -- purchasing entity (NorthPeak/Clearwater)
    supplier_id    INT NOT NULL,
    po_no          VARCHAR(50) NOT NULL,  -- auto-generated via generate_po_no(), unique (unlike contract_no)
    order_date     DATE NOT NULL,
    expected_date  DATE NULL,
    payment_method VARCHAR(50) NULL,
    total_amount   DECIMAL(12,2) NOT NULL DEFAULT 0.00,  -- = SUM(purchase_order_items.quantity*unit_price)
    notes          TEXT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_po_no (po_no),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
);

-- 17. purchase_order_items (PO line items -- component_id, not product_id: raw materials only)
CREATE TABLE purchase_order_items (
    poi_id             INT AUTO_INCREMENT PRIMARY KEY,
    po_id              INT NOT NULL,
    component_id       INT NOT NULL,
    quantity           DECIMAL(12,3) NOT NULL,
    unit_price         DECIMAL(10,2) NOT NULL,
    unit               VARCHAR(20) NOT NULL DEFAULT 'kg',
    spec_potency       DECIMAL(18,4) NULL,  -- intended spec/potency agreed at order time (not measured)
    spec_potency_unit  VARCHAR(30) NULL,    -- spec unit; auto-filled from components.default_unit when picking a material
    notes              VARCHAR(200) NULL,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    FOREIGN KEY (component_id) REFERENCES components(component_id)
);

-- 18. purchase_invoices (supplier invoice records, mirrors invoices)
CREATE TABLE purchase_invoices (
    purchase_invoice_id INT AUTO_INCREMENT PRIMARY KEY,
    po_id          INT NOT NULL,
    company_id     INT NULL,
    invoice_no     VARCHAR(100) NULL,
    invoice_amount DECIMAL(12,2) NOT NULL,
    invoice_date   DATE NULL,     -- NULL = not yet invoiced
    remark         TEXT NULL,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE
);

-- 19. purchase_payments (payables/payment records, mirrors receipts)
CREATE TABLE purchase_payments (
    payment_id     INT AUTO_INCREMENT PRIMARY KEY,
    po_id          INT NOT NULL,
    company_id     INT NULL,
    payment_amount DECIMAL(12,2) NOT NULL,
    payment_date   DATE NULL,     -- NULL = not yet paid
    remark         TEXT NULL,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE
);

-- 13. product_formulas (product formula header -- one per product, no versioning)
CREATE TABLE product_formulas (
    formula_id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL UNIQUE,
    notes      TEXT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 14. formula_items (formula line items -- target active-ingredient content spec)
CREATE TABLE formula_items (
    fi_id          INT AUTO_INCREMENT PRIMARY KEY,
    formula_id     INT NOT NULL,
    component_id   INT NOT NULL,  -- references components.component_id
    target_amount  DECIMAL(18,6) NOT NULL DEFAULT 0,  -- target content value (e.g., 200)
    unit           VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',  -- unit (billion CFU/g, U/g, g/kg, %, etc.)
    notes          VARCHAR(200) NULL,
    UNIQUE KEY uq_formula_product (formula_id, product_id)
);
-- target_amount = concentration of this active ingredient per gram of finished product.
-- Actual raw-material consumption per produced batch is recorded manually in production_consumptions
-- (below) at production time — NOT auto-calculated from potency; the formula is shown as a reference only.

-- 20. production_consumptions (production draw -- records which raw-material batches a
--     finished-goods batch consumed)
CREATE TABLE production_consumptions (
    pc_id              INT AUTO_INCREMENT PRIMARY KEY,
    produced_batch_id  INT NOT NULL,  -- batches.batch_id, batch_type='produced'
    raw_batch_id       INT NOT NULL,  -- batches.batch_id, batch_type='received'
    quantity_consumed  DECIMAL(12,3) NOT NULL,
    notes              VARCHAR(200) NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- No FK constraints: both columns point into the polymorphic batches table under
-- different batch_type values, same rationale as batches.poi_id / shipments.batch_id.
```

## 4. Multi-Company Accounting Logic

`company_id` identifies the selling/invoicing entity.

**Default views** show NorthPeak. Users can switch to Clearwater via a selector. Never default to "all companies" in financial or operational views.

**Clearwater resale chain (ledger / ZT)**

When Clearwater sells to a final customer, an upstream NorthPeak→Clearwater supply contract is auto-created:

```
NorthPeak → Clearwater  (ZT contract, contract_no: YYYYMMDDZT<N>)
Clearwater → Customer   (downstream contract, company_id=Clearwater)
```

- `ensure_primary_secondary_supply_contract(downstream_contract_id)` — creates upstream ZT contract; no-op for NorthPeak contracts.
- `is_secondary_downstream_contract(contract_id)` — True only if company=Clearwater AND client≠Clearwater.
- The upstream item's unit price is derived from the downstream price via `calculate_primary_secondary_unit_price()` (80% of the downstream unit price).
- Shipment mirroring onto the upstream contract is additionally gated by `RESALE_CLIENT_KEYWORD` (`sync_primary_secondary_internal_shipment` / `delete_synced_internal_shipment`): only downstream shipments whose client name contains this keyword get mirrored upstream as an `INTERNAL_TRANSFER_LOGISTICS_COMPANY`-tagged shipment. Other Clearwater sales still get a paired upstream contract (for accounting), just without shipment mirroring.
- ZT contract numbering: globally auto-incrementing, format `YYYYMMDDZT<N>`.
- **Samples do NOT use the ledger split** — sample quantities are too small to affect inventory or accounting.

## 5. Sample Logic

Samples are completely separate from sales contracts. They live in a single flat `samples` table — one row per product per sample batch.

- **NorthPeak samples**: `SP-YYYY-NNN` numbering (`company_id = NorthPeak`)
- **Clearwater samples**: `GQ-YYYY-NNN` numbering (`company_id = Clearwater`) -- prefix is a legacy code, not an abbreviation of the current entity name
- Both sequences auto-increment independently per year via `generate_sample_no(date, company_id)`
- Multiple rows share the same `sample_no` when one batch contains multiple products
- All header info (client, date, address, notes) stored redundantly in each row — no separate header table
- Edit/delete operate on `sample_no` (all rows with that `sample_no`)
- Shipping info (date, tracking_no, logistics_company) stored directly on each row
- No inventory deduction for samples; no ZT / ledger split regardless of which entity sends them

**Startup migration**: `ensure_samples_schema()` auto-detects old two-table schema (samples + sample_items) and flattens it on first run.

## 6. Contract and Finance Rules

- `contract_no` is **not** unique — one paper contract may span multiple DB records.
- `contracts.total_amount` = subtotal for that record = sum of `items.quantity * items.unit_price`.
- No `contracts.status` column. Progress is derived:
  - Shipment: `SUM(shipments.quantity_shipped)` vs `items.quantity`
  - Receipt: `SUM(receipts.payment_amount WHERE payment_date IS NOT NULL)` vs `contracts.total_amount`
  - Invoice: `SUM(invoices.invoice_amount WHERE invoice_date IS NOT NULL)` vs `contracts.total_amount`
- `contracts` stores sales orders only; samples live in `samples`.

## 6b. Unit Conventions

All potency / activity values in this system use the following units consistently:

| Type | Unit | Notes |
|---|---|---|
| Probiotic strains | billion CFU/g | 1 billion = 1×10⁹ CFU/g |
| Enzymes | U/g | enzyme units per gram |
| Fillers / excipients | g/kg | grams per kg of finished product |
| Functional ingredients | % | percent by weight |

**Never use a bare number alone** — always pair it with its unit so the scale is unambiguous.
The `POTENCY_UNITS` tuple in `app.py` defines the dropdown options: `('billion CFU/g', 'U/g', 'g/kg', '%', 'mg/g', 'IU/g')`.

## 7. Inventory Rules

**Unified batches (`batches` table)**

- `batch_type='produced'` → finished goods batches (formerly `inventory_batches`)
- `batch_type='received'` → raw material received lots (formerly `raw_material_batches`)
- `supplier_id` belongs to `batches`, not `products`; the same product/material may be purchased from different suppliers in different batches. (Prior to the procurement module, this was a free-text `supplier` VARCHAR column — migrated to a `suppliers` FK by `ensure_procurement_schema()`.)
- Shipments optionally link to a batch via `shipments.batch_id` (NULL allowed while inventory is being populated).
- When linked, `quantity_available` decrements on shipment create; restores on delete.
- `status` auto-sets to `'depleted'` when `quantity_available` ≤ 0.

**Components / raw materials (`components`)**

- `products` is sales product master data only.
- `components` stores strains, enzymes, excipients, carriers, and functional ingredients.
- `component_type` values include `'Strain'`, `'Enzyme'`, `'Excipient'`, `'Functional'`, `'Blend'`, `'Other'`.
- Soft-delete components by setting `is_active=0`.
- Raw-material received lots use `batches.component_id`.
- Finished-goods produced batches use `batches.product_id`.
- Potency units by type: Strain→billion CFU/g, Enzyme→U/g, Excipient→g/kg, Functional→%.
- `batches.unit` is the **quantity** unit (independent of `potency_unit`, which is a concentration unit). Defaults to `'kg'` everywhere — finished-goods (`batch_type='produced'`) batches always stay `'kg'` (no UI to change it). Raw-material (`batch_type='received'`) batches expose it on `new_rm_batch`/`edit_rm_batch` so non-weight items (e.g. packaging like foil bags, counted in `pcs`) can be tracked correctly; `raw_materials.html`, the production-consumption raw-batch picker, and the procurement-statistics inventory valuation table all display it instead of assuming kg.

**Product formulas (`product_formulas` + `formula_items`)**

- One formula per product, no versioning (edits overwrite)
- `formula_items.component_id` references `components.component_id`
- `formula_items.target_amount` = concentration of each active ingredient per gram of finished product (e.g., 200 billion CFU/g of strain A)
- The formula is a **target specification**, not a fixed recipe. Converting it into an actual raw-material mass would require the specific raw batch's real potency — genuinely batch-specific, so this is **not** auto-calculated anywhere (see §7c: raw-material consumption is manually entered, formula is shown only as a reference).
- Edit via `/product-formula/<product_id>`; "Formula" button on products page

## 7b. Procurement Rules

Mirrors the sales side (`contracts`→`items`→`shipments`→`invoices`→`receipts`) with `suppliers`→`purchase_orders`→`purchase_order_items`→(batches via `poi_id`)→`purchase_invoices`/`purchase_payments`.

- `purchase_orders` has **no status column** — receiving/invoiced/paid progress is derived via LEFT JOIN + SUM at query time in `purchase_orders()`/`purchase_invoices()`/`purchase_payments()`, same convention as §6.
- Unlike `contract_no`, `purchase_orders.po_no` **is unique** and auto-generated by `generate_po_no()` (format `PO-YYYYMMDD-NNN`) — a PO is a discrete document, not a paper form that can span multiple rows.
- `purchase_order_items.component_id` references `components` (raw materials only) — procurement never orders finished `products`.
- `batches.poi_id` optionally links a received batch back to the purchase-order line it fulfills (no FK constraint declared, same as `shipments.batch_id` — enforced only in application code). Receiving without a PO is still allowed; `poi_id` stays NULL.
- `batches.unit_cost` is a snapshot of the purchase unit price at receiving time, stored directly on the batch (not derived via `poi_id` join) because received quantity may differ from ordered quantity, and PO-less receiving still needs a cost basis.
- Suppliers are soft-deleted (`is_active=0`), matching `components`, not `clients`' hard-delete pattern.
- `delete_purchase_order()` blocks if any batches/invoices/payments reference the PO; `edit_purchase_order()` blocks removing a line item that already has a received batch (mirrors `edit_order()`'s shipment-count guard).
- `purchase_order_items.spec_potency`/`spec_potency_unit` record the **intended** spec at order time (e.g. buying at "4000 U/g") — purely informational, entered by the buyer, not validated against anything. This is distinct from `batches.actual_potency`, which is the **measured** value recorded when the lot is actually received; the two are never reconciled automatically.
- **Statistics (`/statistics`) has a Procurement Statistics tab**: purchase spend by supplier/material, monthly trend, and a raw-material inventory valuation snapshot (`quantity_available × unit_cost`, unfiltered by year/company since `batches` has no `company_id`). This is **spend/valuation only, not COGS or margin** — no statistics feature reads `production_consumptions` (§7c) yet, and `formula_items.target_amount` is still an active-ingredient concentration (not a mass ratio), so a real per-product cost/margin figure would need new statistics work even though raw-material consumption is now tracked. Don't add margin/profit metrics without accounting for both.

## 7c. Production Rules

`production_consumptions` links a produced batch (`batches.batch_type='produced'`) to the raw-material batches (`batches.batch_type='received'`) consumed to make it — created/edited/deleted as part of `new_batch()`/`edit_batch()`/`delete_batch()`, not a standalone page.

- **Manual entry, formula is reference only**: the user picks raw-material batches and types the consumed quantity themselves. The product's formula (if one exists) is shown as a read-only hint (`/api/products/<id>/formula`) — it does **not** auto-calculate or validate the entered quantity, and the raw-batch picker is not filtered to only the formula's components.
- **Optional**: a produced batch can have zero consumption rows (repackaging, products without tracked formulas/raw batches, etc.) — not enforced.
- **No FK constraint** on `production_consumptions.produced_batch_id`/`raw_batch_id` — both point into the same polymorphic `batches` table under different `batch_type` values, same rationale as `batches.poi_id`/`shipments.batch_id`. Consistency is enforced only by the `WHERE batch_type='received' AND status='available'` guard in `_validate_consumption_rows()`.
- Deducting/restoring raw-batch quantity on create/edit/delete mirrors the `shipments` pattern exactly (`quantity_available -= qty`, auto-flip to `'depleted'` via inline `CASE`; restore on delete/edit before re-applying). `edit_batch()` uses a full "restore all old rows, delete them, re-validate and re-apply the submitted rows" replace rather than a per-row diff, since nothing downstream references `production_consumptions` rows (unlike `items`, which `shipments` can reference).
- `delete_rm_batch()` blocks deleting a raw-material batch that has `production_consumptions` rows referencing it (mirrors `delete_batch()`'s shipment-count guard for produced batches).
- Real raw-material consumption per produced batch is now recorded, so `SUM(production_consumptions.quantity_consumed * raw batch's batches.unit_cost)` grouped by `produced_batch_id` is a legitimate **actual** cost input for a future COGS/margin feature — unlike the formula-based estimate, which remains a theoretical approximation.

## 8. Key Helper Functions

| Function | Purpose |
|---|---|
| `beijing_today()` | Current date in Beijing timezone |
| `get_company_id_by_name(name)` | Look up company_id by name |
| `get_default_company_id()` | Returns NorthPeak's company_id |
| `ensure_column(table, col, defn)` | Safe additive schema migration |
| `generate_sample_no(date, company_id)` | SP-/GQ- prefix + year + 3-digit seq, reads from `samples` table |
| `generate_internal_transfer_contract_no()` | Global ZT sequence |
| `generate_po_no()` | `PO-YYYYMMDD-NNN` per-day sequence for `purchase_orders.po_no` |
| `ensure_primary_secondary_supply_contract(id)` | Creates upstream ZT contract for Clearwater sales |
| `sync_primary_secondary_internal_shipment(shipment_id)` | Mirrors shipment to upstream ZT contract (gated by `RESALE_CLIENT_KEYWORD`) |
| `_validate_consumption_rows(...)` | Read-only validation of raw-material consumption form rows (existence + sufficient stock), used by `new_batch()`/`edit_batch()` |
| `_apply_consumption_rows(produced_batch_id, rows)` | Deducts raw batches and inserts `production_consumptions` rows for already-validated data |

## 9. Navigation Structure

```
Orders | Clients | Products | Shipments | Finished Goods | Raw Materials | Procurement | Samples | Finance | Statistics | Smart Query
```

`Procurement` bundles 4 sub-pages behind one nav item (mirrors how `Finance` bundles invoicing + receipts): `/purchase-orders` (landing page), `/suppliers`, `/purchase-invoices`, `/purchase-payments`, cross-linked via an in-page `btn-group`, all sharing `active_page = "procurement"`.

`active_page` variable in each template controls navbar highlighting.

## 10. Coding Conventions

- All DB queries use `text()` with named parameters — never f-string SQL.
- `db.session.execute(...).mappings().all()` for multi-row, `.mappings().first()` for single row.
- `db.session.rollback()` in every except block before returning an error.
- Templates extend `base.html`; scripts go in `{% block scripts %}`.
- No Alembic — add columns via `ensure_column()`, add tables via `CREATE TABLE IF NOT EXISTS` inside `ensure_*` functions.
- New module checklist: schema in `ensure_inventory_schema()` or new `ensure_*` function → routes before `if __name__ == '__main__':` → navbar link in `partials/navbar.html`.
