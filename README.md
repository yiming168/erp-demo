# ERP Demo — Enterprise Data Management System

A lightweight ERP covering sales, finance, shipping, samples, finished-goods inventory, raw-material inventory, and product formula management.

> This is a sanitized portfolio demo adapted from a real production ERP project: company names and client data have been replaced with fictional information, and login has been replaced with a demo-mode notice banner (see "Access Control" below).

## Module Status

| Module | Status | Notes |
|---|---|---|
| Order Management | ✅ | Two ledger entities (Company A / Company B), internal-transfer ledger auto-linked |
| Client Management | ✅ | Clients, contacts, multiple shipping addresses |
| Product Management | ✅ | Product list + formula editor entry point |
| Shipment Management | ✅ | Linked to order items, optionally linked to a finished-goods batch |
| Finished Goods Inventory | ✅ | Production receipts can record raw-material consumption (auto-deducts raw-material stock); shipments auto-deduct (batch link currently optional) |
| Raw Material Inventory | ✅ | Component master data + received batches, potency records |
| Procurement | ✅ | Supplier master data, purchase orders (with unit price), receiving linked to purchase orders, supplier invoices/payments |
| Product Formulas | ✅ | One BOM per product, raw-material usage per kg of finished product |
| Sample Management | ✅ | Standalone samples/sample_items table, SP-/GQ- numbering, no ledger logic |
| Finance | ✅ | Invoicing, payment receipts, filterable by ledger entity |
| Statistics | ✅ | Sales statistics + procurement statistics (by supplier/material, monthly trend) + raw-material inventory valuation |
| Smart Query | ✅ | |

**Inventory constraint note**: the batch field on shipments is currently optional; it will become a required link once finished-goods inventory data is fully populated.

**Sample note**: samples use a standalone `samples` + `sample_items` table, entirely separate from sales contracts. Sample quantities are small enough that they don't affect inventory and aren't run through ledger accounting.

---

## Getting Started

```bash
# 1. Clone
git clone <this-repo-url>
cd erpDemo

# 2. Create a virtual environment
python -m venv venv                    # Windows
python3 -m venv venv                   # Mac/Linux

# 3. Activate it
.\venv\Scripts\Activate.ps1            # Windows PowerShell
source venv/bin/activate               # Mac/Linux

# 4. Install dependencies
pip install -r requirements.txt

# 5. Configure environment variables (see .env section below)

# 6. Run
python app.py
# Visit http://127.0.0.1:5000
```

## .env File

```env
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=your_db_host
DB_PORT=3306
DB_NAME=your_db_name

FLASK_SECRET_KEY=replace-with-a-long-random-string
SESSION_COOKIE_SECURE=true

# Optional: enables the "Smart Query" page's AI query feature (a free Gemini API key works)
GEMINI_API_KEY=your-gemini-api-key

# Demo-mode notice banner at the top of every page; on by default. Turn off and wire up
# real OAuth/access control for a production deployment.
DEMO_MODE=true
```

## Access Control

This demo **does not implement real login** — every page is publicly accessible, with a notice banner at the top explaining that a production deployment would gate access behind Google OAuth + an access password (as the original project did). This is intentional: visitors can explore the features without registering or logging in, while the banner is honest about what a real deployment's security model looks like.

## Day-to-Day Use

```powershell
.\venv\Scripts\Activate.ps1
git pull        # when there are updates
python app.py
```

## Recovering from a Broken Branch

```bash
# Realign the local branch with remote main
git fetch origin main --prune
git reset --hard origin/main
```

## Database Migrations

No Alembic. Schema changes are applied automatically at startup via the `ensure_*` functions in `app.py`, and are idempotent/safe to re-run. See `AGENTS.md` for details.
