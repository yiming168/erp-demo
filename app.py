# --- Routes ---

import os
import datetime
import json
import re
import urllib.error
import urllib.request
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text
from dotenv import load_dotenv
from decimal import Decimal, ROUND_HALF_UP
from werkzeug.middleware.proxy_fix import ProxyFix

# Load credentials from .env
load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY') or os.getenv('SECRET_KEY') or os.urandom(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true'

# Configure the database connection (SSL required)
ssl_args = {'ssl': {'ca': 'ca.pem'}}

# Build the connection string
db_user = os.getenv('DB_USER')
db_pass = os.getenv('DB_PASSWORD')
db_host = os.getenv('DB_HOST')
db_port = os.getenv('DB_PORT')
db_name = os.getenv('DB_NAME')

app.config['SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': ssl_args}

db = SQLAlchemy(app)

# This is a public portfolio demo: real Google OAuth + rotating access-password
# gating (used in the production deployment this was adapted from) is replaced
# with a static notice banner — see inject_auth_user() / base.html.
DEMO_MODE = os.getenv('DEMO_MODE', 'true').lower() not in ('0', 'false', 'no')

ALLOWED_PROCESS_TYPES = (
    'Freeze Drying',
    'Spray Drying',
    'Blending Mix',
    'Fluidized Bed Drying',
)

AI_QUERY_TABLES = {
    'companies',
    'clients',
    'contacts',
    'products',
    'components',
    'contracts',
    'items',
    'shipments',
    'invoices',
    'receipts',
    'shipping_addresses',
    'batches',
    'samples',
}

AI_QUERY_MAX_ROWS = 200
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

CRM_SCHEMA_PROMPT = """
You are generating read-only MySQL SQL for a NorthPeak Biotech ERP.
Return JSON only.

Tables:
clients(client_id, company_name, company_address, tax_id, created_at)
contacts(contact_id, client_id, contact_name, cellphone, title, other_info)
products(product_id, chinese_name, process_type, description)
components(component_id, component_name, component_type, default_unit, notes, is_active, created_at)
contracts(contract_id, company_id, source_contract_id, contract_no, client_id, contact_id, order_date, delivery_deadline, payment_method, total_amount, shipping_address, notes, created_at)
items(item_id, contract_id, product_id, quantity, agreed_cfu, unit_price, packaging_spec)
shipments(shipment_id, item_id, batch_id, quantity_shipped, tracking_no, logistics_company, coa, shipping_date)
batches(batch_id, product_id, component_id, batch_no, batch_type, production_date, received_date, expiry_date, quantity_total, quantity_available, actual_potency, potency_unit, supplier, supplier_batch_no, status, notes, created_at)
companies(company_id, company_name, short_name, tax_id, company_address, bank_info, phone, is_active, created_at)
invoices(invoice_id, contract_id, company_id, invoice_no, invoice_amount, invoice_date, remark)
receipts(receipt_id, contract_id, company_id, payment_amount, payment_date, remark)
shipping_addresses(address_id, client_id, address, is_default)
samples(sample_id, company_id, sample_no, client_id, contact_id, product_id, quantity, agreed_cfu, packaging_spec, shipping_address, request_date, shipping_date, tracking_no, logistics_company, coa, notes, created_at)

Business rules:
- contract_no is not unique. A paper contract may cover multiple contract_id rows.
- contracts.total_amount is the amount for one shipment batch, not the grand total of contract_no.
- Paid amount only counts receipts where payment_date IS NOT NULL.
- Invoiced amount only counts invoices where invoice_date IS NOT NULL.
- contracts stores sales orders only; samples live in the samples table.
- Shipment progress is derived from shipments.quantity_shipped versus items.quantity.
- company_id separates the two ledger entities (NorthPeak Biotech and Clearwater Health Sciences) for accounting/reporting.
- For resale chains, contracts.source_contract_id links a Clearwater customer sale to the upstream NorthPeak-to-Clearwater supply sale.
- batches.quantity_available tracks remaining stock; shipments must link to a batch and deduct from it.
- batches.batch_type='produced' for finished goods linked by product_id; 'received' for raw material lots linked by component_id.
- components stores strains, enzymes, excipients, and functional ingredients.
- components.default_unit is only the default unit for new formula items and received batches.

SQL rules:
- Generate one MySQL SELECT statement only.
- Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE, CALL, SET, or multi-statement SQL.
- Prefer explicit JOINs and readable aliases.
- Use clear English column aliases when useful.
- Add LIMIT 200 unless the query is a small aggregate.

JSON shape:
{
  "sql": "SELECT ...",
  "title": "short English title",
  "chart_type": "bar|line|pie|none",
  "label_column": "column alias/name for labels or null",
  "value_column": "numeric column alias/name for values or null"
}
"""


@app.context_processor
def inject_demo_notice():
    return {'demo_mode': DEMO_MODE}


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def rows_to_json(rows):
    return [
        {key: json_safe(value) for key, value in row.items()}
        for row in rows
    ]


def call_gemini_json(prompt):
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise ValueError('GEMINI_API_KEY is not configured. Please add a Gemini API key to .env first.')

    url = (
        f'https://generativelanguage.googleapis.com/v1beta/models/'
        f'{GEMINI_MODEL}:generateContent?key={api_key}'
    )
    payload = {
        'contents': [
            {
                'role': 'user',
                'parts': [{'text': prompt}]
            }
        ],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json'
        }
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            body = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='ignore')
        raise ValueError(f'Gemini API call failed: HTTP {exc.code} {detail}') from exc
    except urllib.error.URLError as exc:
        raise ValueError(f'Gemini API network connection failed: {exc.reason}') from exc

    try:
        text_response = body['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError) as exc:
        raise ValueError(f'Gemini API returned an unexpected format: {body}') from exc

    cleaned = text_response.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    return json.loads(cleaned)


def validate_ai_sql(sql):
    if not sql or not isinstance(sql, str):
        raise ValueError('Gemini did not return any SQL.')

    cleaned = sql.strip()
    cleaned = re.sub(r';+\s*$', '', cleaned)
    lowered = cleaned.lower()

    if ';' in cleaned:
        raise ValueError('Security restriction: multi-statement SQL is not allowed.')
    if not re.match(r'^select\s', lowered):
        raise ValueError('Security restriction: smart query only allows SELECT.')
    if re.search(r'--|#|/\*|\*/', cleaned):
        raise ValueError('Security restriction: comments are not allowed in SQL.')

    blocked = (
        'insert', 'update', 'delete', 'drop', 'alter', 'create', 'truncate',
        'replace', 'call', 'set', 'grant', 'revoke', 'load', 'outfile'
    )
    if re.search(r'\b(' + '|'.join(blocked) + r')\b', lowered):
        raise ValueError('Security restriction: SQL contains a non-read-only keyword.')

    referenced_tables = {
        match.group(1).lower()
        for match in re.finditer(r'\b(?:from|join)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?', cleaned, re.I)
    }
    unknown_tables = referenced_tables - AI_QUERY_TABLES
    if unknown_tables:
        raise ValueError(f"Security restriction: access to these tables is not allowed: {', '.join(sorted(unknown_tables))}")

    if not re.search(r'\blimit\s+\d+\b', lowered):
        cleaned = f'{cleaned} LIMIT {AI_QUERY_MAX_ROWS}'

    return cleaned


def build_chart_data(rows, label_column, value_column):
    if not rows or not label_column or not value_column:
        return None
    if label_column not in rows[0] or value_column not in rows[0]:
        return None

    labels = []
    values = []
    for row in rows:
        value = row.get(value_column)
        if isinstance(value, (int, float)):
            labels.append(str(row.get(label_column, '')))
            values.append(value)
    if not labels:
        return None
    return {'labels': labels[:50], 'values': values[:50]}


def parse_product_form():
    process_type = request.form.get('process_type') or 'Freeze Drying'
    if process_type not in ALLOWED_PROCESS_TYPES:
        allowed_values = ' / '.join(ALLOWED_PROCESS_TYPES)
        raise ValueError(f"Invalid process type, must be one of: {allowed_values}")


    return {
        'chinese_name': request.form['chinese_name'],
        'process_type': process_type,
        'description': request.form.get('description') or None,
    }

POTENCY_UNITS = ('billion CFU/g', 'U/g', 'g/kg', '%', 'mg/g', 'IU/g')

PRODUCT_TYPES = ('Probiotic Powder', 'Enzyme Preparation', 'Auxiliary Material', 'Functional Ingredient')

LEDGER_COMPANIES = (
    {
        'company_name': 'NorthPeak Biotech',
        'short_name': 'NorthPeak',
    },
    {
        'company_name': 'Clearwater Health Sciences Co., Ltd.',
        'short_name': 'Clearwater',
    },
)
PRIMARY_LEDGER_NAME = LEDGER_COMPANIES[0]['company_name']
SECONDARY_LEDGER_NAME = LEDGER_COMPANIES[1]['company_name']
RESALE_CLIENT_KEYWORD = 'Meridian BioGrowth'
INTERNAL_TRANSFER_LOGISTICS_COMPANY = 'Internal Transfer'


def get_client_id_by_name(company_name):
    return db.session.execute(text("""
        SELECT client_id
        FROM clients
        WHERE company_name = :company_name
        ORDER BY client_id
        LIMIT 1
    """), {'company_name': company_name}).scalar()


def ensure_client_exists(company_name):
    db.session.execute(text("""
        INSERT INTO clients (company_name)
        SELECT :company_name
        WHERE NOT EXISTS (
            SELECT 1 FROM clients WHERE company_name = :company_name
        )
    """), {'company_name': company_name})
    return get_client_id_by_name(company_name)


def beijing_today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()


def generate_internal_transfer_contract_no():
    # Global sequence: search all ZT contracts across all dates
    rows = db.session.execute(text("""
        SELECT contract_no FROM contracts
        WHERE contract_no REGEXP '^[0-9]{8}ZT[0-9]+$'
    """)).scalars().all()
    max_number = 0
    pattern = re.compile(r'^\d{8}ZT(\d+)$')
    for contract_no in rows:
        m = pattern.match(contract_no or '')
        if m:
            max_number = max(max_number, int(m.group(1)))
    return f"{beijing_today().strftime('%Y%m%d')}ZT{max_number + 1}"


def generate_sample_no(order_date=None, company_id=None):
    if order_date:
        year = order_date.year if hasattr(order_date, 'year') else int(str(order_date)[:4])
    else:
        year = beijing_today().year
    secondary_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    prefix = f'GQ-{year}-' if (company_id and company_id == secondary_id) else f'SP-{year}-'
    rows = db.session.execute(text("""
        SELECT DISTINCT sample_no FROM samples WHERE sample_no LIKE :prefix
    """), {'prefix': f'{prefix}%'}).scalars().all()
    max_seq = 0
    pattern = re.compile(rf'^{re.escape(prefix)}(\d+)$')
    for no in rows:
        m = pattern.match(no or '')
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f'{prefix}{max_seq + 1:03d}'


def generate_internal_transfer_tracking_no(downstream_shipment_id):
    return f"NB{beijing_today().strftime('%Y%m%d')}-{downstream_shipment_id}"


def generate_batch_no(batch_type='received'):
    prefix = 'RM' if batch_type == 'received' else 'FG'
    date_str = beijing_today().strftime('%Y%m%d')
    pattern = f'{prefix}-{date_str}-%'
    last = db.session.execute(text(
        "SELECT batch_no FROM batches WHERE batch_no LIKE :p ORDER BY batch_no DESC LIMIT 1"
    ), {'p': pattern}).scalar()
    seq = 1
    if last:
        try:
            seq = int(last.rsplit('-', 1)[-1]) + 1
        except ValueError:
            pass
    return f'{prefix}-{date_str}-{seq:03d}'


def generate_po_no():
    date_str = beijing_today().strftime('%Y%m%d')
    pattern = f'PO-{date_str}-%'
    last = db.session.execute(text(
        "SELECT po_no FROM purchase_orders WHERE po_no LIKE :p ORDER BY po_no DESC LIMIT 1"
    ), {'p': pattern}).scalar()
    seq = 1
    if last:
        try:
            seq = int(last.rsplit('-', 1)[-1]) + 1
        except ValueError:
            pass
    return f'PO-{date_str}-{seq:03d}'


def calculate_primary_secondary_unit_price(downstream_unit_price):
    downstream_price = Decimal(downstream_unit_price or 0)
    return (downstream_price * Decimal('0.8')).quantize(
        Decimal('0.1'),
        rounding=ROUND_HALF_UP
    )


def is_secondary_downstream_contract(contract_id):
    secondary_company_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    secondary_client_id = get_client_id_by_name(SECONDARY_LEDGER_NAME)
    if not secondary_company_id:
        return False

    contract = db.session.execute(text("""
        SELECT company_id, client_id
        FROM contracts
        WHERE contract_id = :contract_id
        LIMIT 1
    """), {'contract_id': contract_id}).mappings().fetchone()
    if not contract:
        return False
    return (
        contract['company_id'] == secondary_company_id
        and contract['client_id'] != secondary_client_id
    )


def sync_primary_secondary_supply_contract(downstream_contract_id, upstream_contract_id):
    primary_company_id = get_company_id_by_name(PRIMARY_LEDGER_NAME)
    secondary_company_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    secondary_client_id = ensure_client_exists(SECONDARY_LEDGER_NAME)

    downstream_contract = db.session.execute(text("""
        SELECT *
        FROM contracts
        WHERE contract_id = :contract_id
          AND company_id = :company_id
        LIMIT 1
    """), {
        'contract_id': downstream_contract_id,
        'company_id': secondary_company_id
    }).mappings().fetchone()
    if not downstream_contract:
        return None

    upstream_contract = db.session.execute(text("""
        SELECT contract_id
        FROM contracts
        WHERE contract_id = :contract_id
          AND company_id = :company_id
          AND client_id = :client_id
        LIMIT 1
    """), {
        'contract_id': upstream_contract_id,
        'company_id': primary_company_id,
        'client_id': secondary_client_id
    }).mappings().fetchone()
    if not upstream_contract:
        return None

    db.session.execute(text("""
        UPDATE contracts
        SET order_date = :order_date,
            delivery_deadline = :delivery_deadline,
            payment_method = :payment_method,
            shipping_address = :shipping_address,
            notes = :notes
        WHERE contract_id = :contract_id
    """), {
        'contract_id': upstream_contract_id,
        'order_date': downstream_contract.get('order_date'),
        'delivery_deadline': downstream_contract.get('delivery_deadline'),
        'payment_method': downstream_contract.get('payment_method'),
        'shipping_address': downstream_contract.get('shipping_address'),
        'notes': downstream_contract.get('notes')
    })

    downstream_items = db.session.execute(text("""
        SELECT product_id, quantity, agreed_cfu, unit_price, packaging_spec
        FROM items
        WHERE contract_id = :contract_id
        ORDER BY item_id
    """), {'contract_id': downstream_contract_id}).mappings().fetchall()

    upstream_items = db.session.execute(text("""
        SELECT item_id, product_id, unit_price
        FROM items
        WHERE contract_id = :contract_id
        ORDER BY item_id
    """), {'contract_id': upstream_contract_id}).mappings().fetchall()

    update_item_sql = text("""
        UPDATE items
        SET product_id = :product_id,
            quantity = :quantity,
            agreed_cfu = :agreed_cfu,
            unit_price = :unit_price,
            packaging_spec = :packaging_spec
        WHERE item_id = :item_id
    """)
    insert_item_sql = text("""
        INSERT INTO items (
            contract_id, product_id, quantity, agreed_cfu, unit_price, packaging_spec
        )
        VALUES (
            :contract_id, :product_id, :quantity, :agreed_cfu, :unit_price, :packaging_spec
        )
    """)

    total_amount = Decimal(0)
    for index, downstream_item in enumerate(downstream_items):
        quantity = Decimal(downstream_item['quantity'])
        upstream_item = upstream_items[index] if index < len(upstream_items) else None
        unit_price = calculate_primary_secondary_unit_price(downstream_item['unit_price'])
        total_amount += quantity * unit_price

        params = {
            'product_id': downstream_item['product_id'],
            'quantity': quantity,
            'agreed_cfu': downstream_item['agreed_cfu'],
            'unit_price': unit_price,
            'packaging_spec': downstream_item['packaging_spec'] or '1 kg/bag'
        }
        if upstream_item:
            params['item_id'] = upstream_item['item_id']
            db.session.execute(update_item_sql, params)
        else:
            params['contract_id'] = upstream_contract_id
            db.session.execute(insert_item_sql, params)

    for item in upstream_items[len(downstream_items):]:
        shipment_count = db.session.execute(text("""
            SELECT COUNT(*)
            FROM shipments
            WHERE item_id = :item_id
        """), {'item_id': item['item_id']}).scalar()
        if shipment_count:
            raise ValueError(f"Cannot sync upstream supply contract: order item ID {item['item_id']} already has shipment records and cannot be auto-deleted.")
        db.session.execute(text("""
            DELETE FROM items
            WHERE item_id = :item_id
        """), {'item_id': item['item_id']})

    db.session.execute(text("""
        UPDATE contracts
        SET total_amount = :total_amount
        WHERE contract_id = :contract_id
    """), {'total_amount': total_amount, 'contract_id': upstream_contract_id})
    return upstream_contract_id


def ensure_primary_secondary_supply_contract(downstream_contract_id):
    if not is_secondary_downstream_contract(downstream_contract_id):
        return None

    primary_company_id = get_company_id_by_name(PRIMARY_LEDGER_NAME)
    secondary_client_id = ensure_client_exists(SECONDARY_LEDGER_NAME)
    downstream_contract = db.session.execute(text("""
        SELECT *
        FROM contracts
        WHERE contract_id = :contract_id
        LIMIT 1
    """), {'contract_id': downstream_contract_id}).mappings().fetchone()
    if not downstream_contract:
        return None

    if downstream_contract.get('source_contract_id'):
        return sync_primary_secondary_supply_contract(
            downstream_contract_id,
            downstream_contract.get('source_contract_id')
        )

    result = db.session.execute(text("""
        INSERT INTO contracts (
            company_id, source_contract_id, contract_no, client_id, contact_id,
            order_date, delivery_deadline, payment_method, total_amount,
            shipping_address, notes
        )
        VALUES (
            :company_id, NULL, :contract_no, :client_id, NULL,
            :order_date, :delivery_deadline, :payment_method, 0,
            :shipping_address, :notes
        )
    """), {
        'company_id': primary_company_id,
        'contract_no': generate_internal_transfer_contract_no(),
        'client_id': secondary_client_id,
        'order_date': downstream_contract.get('order_date'),
        'delivery_deadline': downstream_contract.get('delivery_deadline'),
        'payment_method': downstream_contract.get('payment_method'),
        'shipping_address': downstream_contract.get('shipping_address'),
        'notes': downstream_contract.get('notes'),
    })
    upstream_contract_id = result.lastrowid

    db.session.execute(text("""
        UPDATE contracts
        SET source_contract_id = :source_contract_id
        WHERE contract_id = :contract_id
    """), {
        'source_contract_id': upstream_contract_id,
        'contract_id': downstream_contract_id
    })
    return sync_primary_secondary_supply_contract(downstream_contract_id, upstream_contract_id)


def sync_primary_secondary_internal_shipment(downstream_shipment_id):
    downstream = db.session.execute(text("""
        SELECT
            s.shipment_id,
            s.shipping_date,
            s.quantity_shipped,
            i.item_id,
            i.contract_id,
            i.product_id,
            c.company_id,
            c.source_contract_id,
            cl.company_name AS client_name
        FROM shipments s
        JOIN items i ON s.item_id = i.item_id
        JOIN contracts c ON i.contract_id = c.contract_id
        JOIN clients cl ON c.client_id = cl.client_id
        WHERE s.shipment_id = :shipment_id
        LIMIT 1
    """), {'shipment_id': downstream_shipment_id}).mappings().fetchone()
    if not downstream:
        return None

    secondary_company_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    if (
        downstream['company_id'] != secondary_company_id
        or RESALE_CLIENT_KEYWORD not in (downstream['client_name'] or '')
    ):
        return None

    upstream_contract_id = ensure_primary_secondary_supply_contract(downstream['contract_id'])
    if not upstream_contract_id:
        return None

    downstream_item_ids = db.session.execute(text("""
        SELECT item_id
        FROM items
        WHERE contract_id = :contract_id
        ORDER BY item_id
    """), {'contract_id': downstream['contract_id']}).scalars().all()

    try:
        downstream_item_index = downstream_item_ids.index(downstream['item_id'])
    except ValueError:
        return None

    upstream_items = db.session.execute(text("""
        SELECT item_id, product_id
        FROM items
        WHERE contract_id = :contract_id
        ORDER BY item_id
    """), {'contract_id': upstream_contract_id}).mappings().all()
    if downstream_item_index >= len(upstream_items):
        return None

    upstream_item = upstream_items[downstream_item_index]
    if upstream_item['product_id'] != downstream['product_id']:
        matching_upstream_items = [
            item for item in upstream_items
            if item['product_id'] == downstream['product_id']
        ]
        if len(matching_upstream_items) != 1:
            raise ValueError('Could not automatically match the corresponding upstream supply order item.')
        upstream_item = matching_upstream_items[0]

    internal_tracking_suffix = f"%-{downstream_shipment_id}"
    existing_internal_shipment_id = db.session.execute(text("""
        SELECT shipment_id
        FROM shipments
        WHERE item_id = :item_id
          AND logistics_company = :logistics_company
          AND tracking_no LIKE :tracking_suffix
        LIMIT 1
    """), {
        'item_id': upstream_item['item_id'],
        'logistics_company': INTERNAL_TRANSFER_LOGISTICS_COMPANY,
        'tracking_suffix': internal_tracking_suffix
    }).scalar()

    if existing_internal_shipment_id:
        db.session.execute(text("""
            UPDATE shipments
            SET shipping_date = :shipping_date,
                quantity_shipped = :quantity_shipped,
                logistics_company = :logistics_company,
                coa = NULL
            WHERE shipment_id = :shipment_id
        """), {
            'shipment_id': existing_internal_shipment_id,
            'shipping_date': downstream['shipping_date'],
            'quantity_shipped': downstream['quantity_shipped'],
            'logistics_company': INTERNAL_TRANSFER_LOGISTICS_COMPANY
        })
        return upstream_item['item_id']

    db.session.execute(text("""
        INSERT INTO shipments (
            item_id, shipping_date, quantity_shipped, tracking_no,
            logistics_company, coa
        )
        VALUES (
            :item_id, :shipping_date, :quantity_shipped, :tracking_no,
            :logistics_company, NULL
        )
    """), {
        'item_id': upstream_item['item_id'],
        'shipping_date': downstream['shipping_date'],
        'quantity_shipped': downstream['quantity_shipped'],
        'tracking_no': generate_internal_transfer_tracking_no(downstream_shipment_id),
        'logistics_company': INTERNAL_TRANSFER_LOGISTICS_COMPANY
    })
    return upstream_item['item_id']


def delete_synced_internal_shipment(downstream_shipment_id):
    downstream = db.session.execute(text("""
        SELECT c.company_id, cl.company_name AS client_name
        FROM shipments s
        JOIN items i ON s.item_id = i.item_id
        JOIN contracts c ON i.contract_id = c.contract_id
        JOIN clients cl ON c.client_id = cl.client_id
        WHERE s.shipment_id = :shipment_id
        LIMIT 1
    """), {'shipment_id': downstream_shipment_id}).mappings().fetchone()
    if not downstream:
        return

    secondary_company_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    if (
        downstream['company_id'] != secondary_company_id
        or RESALE_CLIENT_KEYWORD not in (downstream['client_name'] or '')
    ):
        return

    db.session.execute(text("""
        DELETE FROM shipments
        WHERE logistics_company = :logistics_company
          AND tracking_no LIKE :tracking_suffix
    """), {
        'logistics_company': INTERNAL_TRANSFER_LOGISTICS_COMPANY,
        'tracking_suffix': f"%-{downstream_shipment_id}"
    })


def table_column_exists(table_name, column_name):
    sql = text("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
    """)
    return db.session.execute(sql, {
        'table_name': table_name,
        'column_name': column_name
    }).scalar() > 0


def ensure_column(table_name, column_name, definition):
    if table_column_exists(table_name, column_name):
        return
    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))


def drop_column_if_exists(table_name, column_name):
    if not table_column_exists(table_name, column_name):
        return
    db.session.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))


def get_company_id_by_name(company_name):
    return db.session.execute(text("""
        SELECT company_id
        FROM companies
        WHERE company_name = :company_name
        LIMIT 1
    """), {'company_name': company_name}).scalar()


def get_default_company_id():
    company_id = get_company_id_by_name(PRIMARY_LEDGER_NAME)
    if company_id:
        return company_id
    return db.session.execute(text("""
        SELECT company_id
        FROM companies
        WHERE is_active = 1
        ORDER BY company_id
        LIMIT 1
    """)).scalar()


def get_companies():
    return db.session.execute(text("""
        SELECT company_id, company_name, short_name
        FROM companies
        WHERE is_active = 1
        ORDER BY company_id
    """)).mappings().all()


def get_selected_company_id(default_to_primary=False):
    company_id = request.args.get('company_id', type=int)
    if company_id:
        return company_id
    if default_to_primary:
        return get_default_company_id()
    return None


def ensure_components_schema():
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS components (
            component_id     INT AUTO_INCREMENT PRIMARY KEY,
            component_name   VARCHAR(100) NOT NULL,
            component_type   VARCHAR(20) NOT NULL DEFAULT 'Other',
            default_unit     VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',
            notes            TEXT NULL,
            is_active        TINYINT(1) NOT NULL DEFAULT 1,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_component_name (component_name)
        )
    """))
    ensure_column('components', 'default_unit', "VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g'")
    if table_column_exists('components', 'potency_unit'):
        db.session.execute(text("""
            UPDATE components
            SET default_unit = potency_unit
            WHERE (default_unit IS NULL OR default_unit = '' OR default_unit = 'billion CFU/g')
              AND potency_unit IS NOT NULL
              AND potency_unit != ''
        """))
    ensure_column('batches', 'component_id', 'INT NULL')
    ensure_column('formula_items', 'component_id', 'INT NULL')

    if table_column_exists('products', 'material_category'):
        formula_product_filter = ""
        if table_column_exists('formula_items', 'product_id'):
            formula_product_filter = """
               OR EXISTS (
                    SELECT 1 FROM formula_items fi
                    WHERE fi.product_id = p.product_id
               )
            """
        db.session.execute(text("""
            INSERT IGNORE INTO components (
                component_name, component_type, default_unit, notes
            )
            SELECT
                p.chinese_name,
                COALESCE(NULLIF(p.material_category, ''), 'Other'),
                COALESCE(p.potency_unit, 'billion CFU/g'),
                p.description
            FROM products p
            WHERE p.material_category IS NOT NULL
               OR EXISTS (
                    SELECT 1 FROM batches b
                    WHERE b.batch_type = 'received'
                      AND b.product_id = p.product_id
               )
        """ + formula_product_filter))

    if table_column_exists('formula_items', 'product_id'):
        db.session.execute(text("""
            UPDATE formula_items fi
            JOIN products p ON p.product_id = fi.product_id
            JOIN components c ON c.component_name = p.chinese_name
            SET fi.component_id = c.component_id
            WHERE fi.component_id IS NULL
        """))

    db.session.execute(text("""
        UPDATE batches b
        JOIN products p ON p.product_id = b.product_id
        JOIN components c ON c.component_name = p.chinese_name
        SET b.component_id = c.component_id
        WHERE b.batch_type = 'received'
          AND b.component_id IS NULL
    """))

    try:
        db.session.execute(text("ALTER TABLE batches MODIFY product_id INT NULL"))
    except Exception:
        pass

    if table_column_exists('formula_items', 'product_id'):
        db.session.execute(text("DELETE FROM formula_items WHERE component_id IS NULL"))
        try:
            db.session.execute(text("ALTER TABLE formula_items DROP KEY uq_formula_product"))
        except Exception:
            pass
        drop_column_if_exists('formula_items', 'product_id')
    drop_column_if_exists('formula_items', 'material_id')
    try:
        db.session.execute(text(
            "ALTER TABLE formula_items ADD UNIQUE KEY uq_formula_component (formula_id, component_id)"
        ))
    except Exception:
        pass

    for column_name in (
        'latin_genus',
        'latin_species',
        'standard_potency',
        'potency_unit',
        'legacy_product_id',
    ):
        if column_name == 'legacy_product_id':
            try:
                db.session.execute(text("ALTER TABLE components DROP KEY uq_component_legacy_product"))
            except Exception:
                pass
        drop_column_if_exists('components', column_name)

    for column_name in (
        'latin_genus',
        'latin_species',
        'standard_cfu',
        'product_type',
        'standard_potency',
        'potency_unit',
        'material_category',
    ):
        drop_column_if_exists('products', column_name)


def ensure_inventory_schema():
    drop_column_if_exists('contracts', 'legacy_sample_id')
    drop_column_if_exists('contracts', 'order_type')
    # New unified batches table (replaces inventory_batches + raw_material_batches)
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id           INT AUTO_INCREMENT PRIMARY KEY,
            product_id         INT NULL,
            component_id       INT NULL,
            batch_no           VARCHAR(50) NOT NULL,
            batch_type         ENUM('produced','received') NOT NULL DEFAULT 'received',
            production_date    DATE NULL,
            received_date      DATE NULL,
            expiry_date        DATE NULL,
            quantity_total     DECIMAL(12,3) NOT NULL DEFAULT 0,
            quantity_available DECIMAL(12,3) NOT NULL DEFAULT 0,
            unit               VARCHAR(20) NOT NULL DEFAULT 'kg',
            actual_potency     DECIMAL(18,4) NULL,
            potency_unit       VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',
            supplier           VARCHAR(100) NULL,
            supplier_batch_no  VARCHAR(50) NULL,
            status             ENUM('available','depleted','rejected') NOT NULL DEFAULT 'available',
            notes              TEXT NULL,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_batch_no (batch_no)
        )
    """))
    ensure_column('batches', 'unit', "VARCHAR(20) NOT NULL DEFAULT 'kg'")
    ensure_column('batches', 'supplier', 'VARCHAR(100) NULL')
    if table_column_exists('products', 'supplier'):
        db.session.execute(text("""
            UPDATE batches b
            JOIN products p ON b.product_id = p.product_id
            SET b.supplier = p.supplier
            WHERE b.batch_type = 'received'
              AND b.supplier IS NULL
              AND p.supplier IS NOT NULL
        """))
        drop_column_if_exists('products', 'supplier')
    ensure_column('shipments', 'batch_id', 'INT NULL')
    # Keep legacy tables with IF NOT EXISTS for migration safety (dropped after migrate_to_unified_batches)
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS inventory_batches (
            batch_id     INT AUTO_INCREMENT PRIMARY KEY,
            product_id   INT NOT NULL,
            batch_no     VARCHAR(50) NOT NULL,
            production_date DATE NULL,
            expiry_date  DATE NULL,
            quantity_produced  DECIMAL(12,3) NOT NULL DEFAULT 0,
            quantity_available DECIMAL(12,3) NOT NULL DEFAULT 0,
            actual_potency     DECIMAL(18,4) NULL,
            potency_unit       VARCHAR(20) NOT NULL DEFAULT 'billion CFU/g',
            status  ENUM('available','depleted','rejected') NOT NULL DEFAULT 'available',
            notes   TEXT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_batch_no (batch_no)
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS raw_materials (
            material_id   INT AUTO_INCREMENT PRIMARY KEY,
            material_name VARCHAR(100) NOT NULL,
            material_type ENUM('Strain','Enzyme','Excipient','Functional','Other') NOT NULL DEFAULT 'Other',
            supplier      VARCHAR(100) NULL,
            standard_potency DECIMAL(18,4) NULL,
            potency_unit  VARCHAR(20) NOT NULL DEFAULT 'billion CFU/g',
            unit          VARCHAR(20) NOT NULL DEFAULT 'kg',
            notes         TEXT NULL,
            is_active     TINYINT(1) NOT NULL DEFAULT 1,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_material_name (material_name)
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS raw_material_batches (
            rm_batch_id        INT AUTO_INCREMENT PRIMARY KEY,
            material_id        INT NOT NULL,
            batch_no           VARCHAR(50) NOT NULL,
            received_date      DATE NULL,
            expiry_date        DATE NULL,
            quantity_total     DECIMAL(12,3) NOT NULL DEFAULT 0,
            quantity_available DECIMAL(12,3) NOT NULL DEFAULT 0,
            actual_potency     DECIMAL(18,4) NULL,
            potency_unit       VARCHAR(20) NOT NULL DEFAULT 'billion CFU/g',
            supplier_batch_no  VARCHAR(50) NULL,
            status  ENUM('available','depleted','rejected') NOT NULL DEFAULT 'available',
            notes   TEXT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_rm_batch_no (batch_no)
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS product_formulas (
            formula_id  INT AUTO_INCREMENT PRIMARY KEY,
            product_id  INT NOT NULL UNIQUE,
            notes       TEXT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """))
    # formula_items stores target component content for each sales product formula.
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS formula_items (
            fi_id          INT AUTO_INCREMENT PRIMARY KEY,
            formula_id     INT NOT NULL,
            component_id   INT NOT NULL,
            target_amount  DECIMAL(18,6) NOT NULL DEFAULT 0,
            unit           VARCHAR(30) NOT NULL DEFAULT 'billion CFU/g',
            notes          VARCHAR(200) NULL,
            UNIQUE KEY uq_formula_component (formula_id, component_id)
        )
    """))
    # Migrate: rename qty_per_kg -> target_amount if old column still exists
    has_old_col = db.session.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'formula_items'
          AND column_name = 'qty_per_kg'
    """)).scalar()
    if has_old_col:
        ensure_column('formula_items', 'target_amount', 'DECIMAL(18,6) NOT NULL DEFAULT 0')
        db.session.execute(text(
            "UPDATE formula_items SET target_amount = qty_per_kg WHERE target_amount = 0"
        ))
        db.session.execute(text("ALTER TABLE formula_items DROP COLUMN qty_per_kg"))
    ensure_components_schema()
    db.session.commit()


def migrate_to_unified_batches():
    """One-time migration: merge inventory_batches + raw_material_batches into batches,
    and raw_materials into products (with material_category set).
    Safe to call multiple times — no-op if inventory_batches is already gone."""
    has_inv = db.session.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema=DATABASE() AND table_name='inventory_batches'
    """)).scalar()
    if not has_inv:
        return  # already migrated or fresh install

    # 1. Migrate inventory_batches → batches (batch_type='produced', preserve batch_id for shipments FK)
    db.session.execute(text("""
        INSERT INTO batches (batch_id, product_id, batch_no, batch_type, production_date,
                             expiry_date, quantity_total, quantity_available,
                             actual_potency, potency_unit, status, notes, created_at)
        SELECT batch_id, product_id, batch_no, 'produced', production_date,
               expiry_date, quantity_produced, quantity_available,
               actual_potency, potency_unit, status, notes, created_at
        FROM inventory_batches
    """))

    # 2. Migrate raw_materials → products, capture id mapping
    raw_mats = db.session.execute(text("SELECT * FROM raw_materials")).mappings().all()
    mat_id_to_product_id = {}
    mat_id_to_supplier = {}
    cat_map = {'Strain': 'Strain', 'Enzyme': 'Enzyme', 'Excipient': 'Excipient', 'Functional': 'Functional', 'Other': 'Other'}
    for m in raw_mats:
        mat_id_to_supplier[m['material_id']] = m['supplier']
        existing = db.session.execute(text(
            "SELECT product_id FROM products WHERE chinese_name=:name"
        ), {'name': m['material_name']}).scalar()
        if existing:
            mat_id_to_product_id[m['material_id']] = existing
            db.session.execute(text("""
                UPDATE products SET material_category=:cat
                WHERE product_id=:pid
            """), {'cat': cat_map.get(m['material_type'], 'Other'), 'pid': existing})
        else:
            res = db.session.execute(text("""
                INSERT INTO products (chinese_name, product_type, standard_potency, potency_unit,
                                      material_category)
                VALUES (:name, :ptype, :potency, :punit, :cat)
            """), {
                'name': m['material_name'],
                'ptype': m['material_type'],
                'potency': m['standard_potency'],
                'punit': m['potency_unit'],
                'cat': cat_map.get(m['material_type'], 'Other'),
            })
            mat_id_to_product_id[m['material_id']] = res.lastrowid

    # 3. Migrate raw_material_batches → batches (batch_type='received')
    rm_batches = db.session.execute(text("SELECT * FROM raw_material_batches")).mappings().all()
    for b in rm_batches:
        new_pid = mat_id_to_product_id.get(b['material_id'])
        if not new_pid:
            continue
        db.session.execute(text("""
            INSERT INTO batches (product_id, batch_no, batch_type, received_date, expiry_date,
                                 quantity_total, quantity_available, actual_potency, potency_unit,
                                 supplier, supplier_batch_no, status, notes, created_at)
            VALUES (:pid, :bno, 'received', :recv, :exp,
                    :qty_t, :qty_a, :potency, :punit,
                    :supplier, :sbno, :status, :notes, :created)
        """), {
            'pid': new_pid, 'bno': b['batch_no'], 'recv': b['received_date'],
            'exp': b['expiry_date'], 'qty_t': b['quantity_total'],
            'qty_a': b['quantity_available'], 'potency': b['actual_potency'],
            'punit': b['potency_unit'], 'supplier': mat_id_to_supplier.get(b['material_id']),
            'sbno': b['supplier_batch_no'],
            'status': b['status'], 'notes': b['notes'], 'created': b['created_at'],
        })

    # 4. Migrate formula_items: material_id → product_id
    has_mat_col = db.session.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema=DATABASE() AND table_name='formula_items' AND column_name='material_id'
    """)).scalar()
    if has_mat_col and mat_id_to_product_id:
        ensure_column('formula_items', 'product_id', 'INT NOT NULL DEFAULT 0')
        for old_mid, new_pid in mat_id_to_product_id.items():
            db.session.execute(text("""
                UPDATE formula_items SET product_id=:pid WHERE material_id=:mid
            """), {'pid': new_pid, 'mid': old_mid})
        try:
            db.session.execute(text("ALTER TABLE formula_items DROP KEY uq_formula_material"))
        except Exception:
            pass
        try:
            db.session.execute(text(
                "ALTER TABLE formula_items ADD UNIQUE KEY uq_formula_product (formula_id, product_id)"
            ))
        except Exception:
            pass
        db.session.execute(text("ALTER TABLE formula_items DROP COLUMN material_id"))

    # 5. Drop legacy tables
    db.session.execute(text("DROP TABLE IF EXISTS raw_material_batches"))
    db.session.execute(text("DROP TABLE IF EXISTS raw_materials"))
    db.session.execute(text("DROP TABLE IF EXISTS inventory_batches"))
    db.session.commit()


def ensure_procurement_schema():
    """Procurement module schema: supplier master data + purchase orders/line items + supplier invoices/payments,
    and migrates batches.supplier free text into a suppliers foreign key (supplier_id)."""
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS suppliers (
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
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_id          INT AUTO_INCREMENT PRIMARY KEY,
            company_id     INT NULL,
            supplier_id    INT NOT NULL,
            po_no          VARCHAR(50) NOT NULL,
            order_date     DATE NOT NULL,
            expected_date  DATE NULL,
            payment_method VARCHAR(50) NULL,
            total_amount   DECIMAL(12,2) NOT NULL DEFAULT 0.00,
            notes          TEXT NULL,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_po_no (po_no),
            FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            poi_id             INT AUTO_INCREMENT PRIMARY KEY,
            po_id              INT NOT NULL,
            component_id       INT NOT NULL,
            quantity           DECIMAL(12,3) NOT NULL,
            unit_price         DECIMAL(10,2) NOT NULL,
            unit               VARCHAR(20) NOT NULL DEFAULT 'kg',
            spec_potency       DECIMAL(18,4) NULL,
            spec_potency_unit  VARCHAR(30) NULL,
            notes              VARCHAR(200) NULL,
            FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
            FOREIGN KEY (component_id) REFERENCES components(component_id)
        )
    """))
    ensure_column('purchase_order_items', 'spec_potency', 'DECIMAL(18,4) NULL')
    ensure_column('purchase_order_items', 'spec_potency_unit', 'VARCHAR(30) NULL')
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            purchase_invoice_id INT AUTO_INCREMENT PRIMARY KEY,
            po_id          INT NOT NULL,
            company_id     INT NULL,
            invoice_no     VARCHAR(100) NULL,
            invoice_amount DECIMAL(12,2) NOT NULL,
            invoice_date   DATE NULL,
            remark         TEXT NULL,
            FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE
        )
    """))
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_payments (
            payment_id     INT AUTO_INCREMENT PRIMARY KEY,
            po_id          INT NOT NULL,
            company_id     INT NULL,
            payment_amount DECIMAL(12,2) NOT NULL,
            payment_date   DATE NULL,
            remark         TEXT NULL,
            FOREIGN KEY (po_id) REFERENCES purchase_orders(po_id) ON DELETE CASCADE
        )
    """))

    ensure_column('batches', 'supplier_id', 'INT NULL')
    ensure_column('batches', 'poi_id', 'INT NULL')
    ensure_column('batches', 'unit_cost', 'DECIMAL(10,2) NULL')

    if table_column_exists('batches', 'supplier'):
        # Backfill supplier master data: dedupe the existing free-text supplier values into records, then backfill supplier_id by matching name
        db.session.execute(text("""
            INSERT IGNORE INTO suppliers (supplier_name)
            SELECT DISTINCT TRIM(supplier) FROM batches
            WHERE supplier IS NOT NULL AND TRIM(supplier) != ''
        """))
        db.session.execute(text("""
            UPDATE batches b
            JOIN suppliers s ON TRIM(b.supplier) = s.supplier_name
            SET b.supplier_id = s.supplier_id
            WHERE b.supplier_id IS NULL AND b.supplier IS NOT NULL
        """))
        drop_column_if_exists('batches', 'supplier')

    db.session.commit()


def ensure_production_schema():
    """Production module schema: records which raw-material batches were consumed by a given production intake (production_consumptions).
    Neither batch_id field has a foreign key constraint -- they point to different
    batch_type subsets of the same polymorphic batches table, which a foreign key can't express,
    so consistency is maintained by application-layer query conditions, consistent with the
    existing approach used by batches.poi_id / shipments.batch_id."""
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS production_consumptions (
            pc_id              INT AUTO_INCREMENT PRIMARY KEY,
            produced_batch_id  INT NOT NULL,
            raw_batch_id       INT NOT NULL,
            quantity_consumed  DECIMAL(12,3) NOT NULL,
            notes              VARCHAR(200) NULL,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    db.session.commit()


def _fix_latin1_utf8(s):
    """Fix strings stored as UTF-8 bytes in a Latin-1 column."""
    if not s:
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def ensure_samples_schema():
    """Create or migrate samples to a flat single-table schema (one row per product per batch)."""
    has_sample_items = db.session.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = 'sample_items'
    """)).scalar()

    if has_sample_items:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS samples_flat (
                sample_id         INT AUTO_INCREMENT PRIMARY KEY,
                company_id        INT NOT NULL,
                sample_no         VARCHAR(50) NOT NULL,
                client_id         INT NOT NULL,
                contact_id        INT NULL,
                product_id        INT NOT NULL DEFAULT 0,
                quantity          DECIMAL(10,3) NOT NULL DEFAULT 0,
                agreed_cfu        DECIMAL(10,2) NULL,
                packaging_spec    VARCHAR(255) DEFAULT '1 kg/bag',
                request_date      DATE NOT NULL,
                shipping_address  TEXT,
                shipping_date     DATE NULL,
                tracking_no       VARCHAR(100) NULL,
                logistics_company VARCHAR(50) NULL,
                coa               VARCHAR(255) NULL,
                notes             TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.session.execute(text("""
            INSERT INTO samples_flat
                (company_id, sample_no, client_id, contact_id, product_id, quantity,
                 agreed_cfu, packaging_spec, request_date, shipping_address,
                 shipping_date, tracking_no, logistics_company, coa, notes, created_at)
            SELECT s.company_id, s.sample_no, s.client_id, s.contact_id,
                   si.product_id, si.quantity, si.agreed_cfu,
                   COALESCE(si.packaging_spec, '1 kg/bag'),
                   s.request_date, s.shipping_address,
                   si.shipping_date, si.tracking_no, si.logistics_company, si.coa,
                   s.notes, s.created_at
            FROM samples s
            JOIN sample_items si ON si.sample_id = s.sample_id
        """))
        db.session.execute(text("DROP TABLE sample_items"))
        db.session.execute(text("DROP TABLE samples"))
        db.session.execute(text("RENAME TABLE samples_flat TO samples"))
    else:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS samples (
                sample_id         INT AUTO_INCREMENT PRIMARY KEY,
                company_id        INT NOT NULL,
                sample_no         VARCHAR(50) NOT NULL,
                client_id         INT NOT NULL,
                contact_id        INT NULL,
                product_id        INT NOT NULL DEFAULT 0,
                quantity          DECIMAL(10,3) NOT NULL DEFAULT 0,
                agreed_cfu        DECIMAL(10,2) NULL,
                packaging_spec    VARCHAR(255) DEFAULT '1 kg/bag',
                request_date      DATE NOT NULL,
                shipping_address  TEXT,
                shipping_date     DATE NULL,
                tracking_no       VARCHAR(100) NULL,
                logistics_company VARCHAR(50) NULL,
                coa               VARCHAR(255) NULL,
                notes             TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        ensure_column('samples', 'product_id', 'INT NOT NULL DEFAULT 0')
        ensure_column('samples', 'quantity', 'DECIMAL(10,3) NOT NULL DEFAULT 0')
        ensure_column('samples', 'agreed_cfu', 'DECIMAL(10,2) NULL')
        ensure_column('samples', 'packaging_spec', "VARCHAR(255) DEFAULT '1 kg/bag'")
        ensure_column('samples', 'shipping_date', 'DATE NULL')
        ensure_column('samples', 'tracking_no', 'VARCHAR(100) NULL')
        ensure_column('samples', 'logistics_company', 'VARCHAR(50) NULL')
        ensure_column('samples', 'coa', 'VARCHAR(255) NULL')
    db.session.commit()


def migrate_sample_contracts_to_samples():
    """One-time legacy migration for databases that still have contracts.order_type."""
    if not table_column_exists('contracts', 'order_type'):
        return
    count = db.session.execute(text(
        "SELECT COUNT(*) FROM contracts WHERE order_type = 'sample'"
    )).scalar()
    if not count:
        return

    default_company_id = get_default_company_id()
    secondary_id = get_company_id_by_name(SECONDARY_LEDGER_NAME)
    sample_contracts = db.session.execute(text("""
        SELECT c.contract_id, c.company_id, c.client_id, c.contact_id,
               c.order_date, c.shipping_address, c.notes
        FROM contracts c
        WHERE c.order_type = 'sample'
        ORDER BY c.order_date, c.contract_id
    """)).mappings().all()

    prefix_year_seq = {}

    def next_no(date_val, cid):
        year = date_val.year if hasattr(date_val, 'year') else beijing_today().year
        pfx = f'GQ-{year}-' if cid == secondary_id else f'SP-{year}-'
        key = (pfx, year)
        if key not in prefix_year_seq:
            existing = db.session.execute(text(
                "SELECT DISTINCT sample_no FROM samples WHERE sample_no LIKE :p"
            ), {'p': f'{pfx}%'}).scalars().all()
            mx = 0
            pat = re.compile(rf'^{re.escape(pfx)}(\d+)$')
            for s in existing:
                m = pat.match(s or '')
                if m:
                    mx = max(mx, int(m.group(1)))
            prefix_year_seq[key] = mx
        prefix_year_seq[key] += 1
        return f'{pfx}{prefix_year_seq[key]:03d}'

    for c in sample_contracts:
        cid = c['company_id'] or default_company_id
        sample_no = next_no(c['order_date'], cid)
        items = db.session.execute(text("""
            SELECT i.product_id, i.quantity, i.agreed_cfu, i.packaging_spec,
                   sh.shipping_date, sh.tracking_no, sh.logistics_company, sh.coa
            FROM items i
            LEFT JOIN shipments sh ON sh.item_id = i.item_id
            WHERE i.contract_id = :cid
        """), {'cid': c['contract_id']}).mappings().all()
        for it in items:
            db.session.execute(text("""
                INSERT INTO samples
                    (company_id, sample_no, client_id, contact_id, product_id,
                     quantity, agreed_cfu, packaging_spec, request_date,
                     shipping_address, shipping_date, tracking_no, logistics_company,
                     coa, notes)
                VALUES (:cid, :sno, :clid, :coid, :pid,
                        :qty, :cfu, :spec, :rdate,
                        :addr, :sdate, :tno, :lc, :coa, :notes)
            """), {
                'cid': cid, 'sno': sample_no,
                'clid': c['client_id'], 'coid': c['contact_id'],
                'pid': it['product_id'], 'qty': it['quantity'],
                'cfu': it['agreed_cfu'], 'spec': it['packaging_spec'] or '1 kg/bag',
                'rdate': c['order_date'], 'addr': c['shipping_address'],
                'sdate': it['shipping_date'], 'tno': it['tracking_no'],
                'lc': it['logistics_company'], 'coa': it['coa'],
                'notes': c['notes'],
            })
        db.session.execute(text("""
            DELETE sh FROM shipments sh
            JOIN items i ON sh.item_id = i.item_id
            WHERE i.contract_id = :cid
        """), {'cid': c['contract_id']})
        db.session.execute(text("DELETE FROM items WHERE contract_id = :cid"),
                           {'cid': c['contract_id']})
        db.session.execute(text("DELETE FROM contracts WHERE contract_id = :cid"),
                           {'cid': c['contract_id']})
    db.session.commit()


def ensure_multi_company_schema():
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS companies (
            company_id INT AUTO_INCREMENT PRIMARY KEY,
            company_name VARCHAR(100) NOT NULL UNIQUE,
            short_name VARCHAR(50),
            tax_id VARCHAR(50),
            company_address TEXT,
            bank_info TEXT,
            phone VARCHAR(50),
            is_active TINYINT(1) DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    for company in LEDGER_COMPANIES:
        db.session.execute(text("""
            INSERT INTO companies (company_name, short_name, is_active)
            VALUES (:company_name, :short_name, 1)
            ON DUPLICATE KEY UPDATE
                short_name = VALUES(short_name),
                is_active = 1
        """), company)

    ensure_column('contracts', 'company_id', 'INT NULL')
    ensure_column('contracts', 'source_contract_id', 'INT NULL')
    ensure_column('invoices', 'company_id', 'INT NULL')
    ensure_column('receipts', 'company_id', 'INT NULL')
        # Legacy sample contracts are migrated before order_type is dropped.

    default_company_id = get_default_company_id()
    if default_company_id:
        db.session.execute(text("""
            UPDATE contracts
            SET company_id = :company_id
            WHERE company_id IS NULL
        """), {'company_id': default_company_id})
        # samples table removed — no backfill needed

    db.session.execute(text("""
        UPDATE invoices i
        JOIN contracts c ON i.contract_id = c.contract_id
        SET i.company_id = c.company_id
        WHERE i.company_id IS NULL
    """))
    db.session.execute(text("""
        UPDATE receipts r
        JOIN contracts c ON r.contract_id = c.contract_id
        SET r.company_id = c.company_id
        WHERE r.company_id IS NULL
    """))

    db.session.execute(text("""
        INSERT INTO clients (company_name)
        SELECT :company_name
        WHERE NOT EXISTS (
            SELECT 1 FROM clients WHERE company_name = :company_name
        )
    """), {'company_name': SECONDARY_LEDGER_NAME})

    db.session.commit()


with app.app_context():
    try:
        ensure_multi_company_schema()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"Multi-company schema initialization skipped: {exc}")
    try:
        ensure_inventory_schema()
    except Exception as exc:
        db.session.rollback()
        print(f"Inventory schema initialization skipped: {exc}")
    try:
        ensure_samples_schema()
    except Exception as exc:
        db.session.rollback()
        print(f"Samples schema init skipped: {exc}")
    try:
        migrate_sample_contracts_to_samples()
    except Exception as exc:
        db.session.rollback()
        print(f"Sample contract migration skipped: {exc}")
    try:
        migrate_to_unified_batches()
    except Exception as exc:
        db.session.rollback()
        print(f"Unified batch migration skipped: {exc}")
    try:
        ensure_procurement_schema()
    except Exception as exc:
        db.session.rollback()
        print(f"Procurement schema initialization skipped: {exc}")
    try:
        ensure_production_schema()
    except Exception as exc:
        db.session.rollback()
        print(f"Production schema initialization skipped: {exc}")
# --- Routes ---

@app.route('/')
def dashboard():
    """Home dashboard: lists all contracts/orders"""

    current_year = datetime.datetime.now().year
    years = list(range(2025, current_year + 6))

    # Get year and month from query parameters
    year = request.args.get('year', default=current_year, type=int)
    month = request.args.get('month', default=None, type=int)
    selected_company_id = get_selected_company_id(default_to_primary=True)

    # Base query
    sql_query = """
        SELECT
            c.contract_id,
            c.company_id,
            cp.short_name AS ledger_short_name,
            cp.company_name AS ledger_company_name,
            c.source_contract_id,
            c.contract_no,
            cl.company_name,
            co.contact_name,
            c.order_date,
            c.total_amount,
            c.delivery_deadline,
            c.shipping_address,
            c.notes,
            IFNULL(p_sum.paid_amount, 0) as paid_amount,
            IFNULL(inv_sum.invoiced_amount, 0) as invoiced_amount,
            IFNULL(s_sum.total_shipped, 0) as total_shipped,
            IFNULL(i_sum.total_quantity, 0) as total_quantity
        FROM contracts c
        LEFT JOIN companies cp ON c.company_id = cp.company_id
        LEFT JOIN clients cl ON c.client_id = cl.client_id
        LEFT JOIN contacts co ON c.contact_id = co.contact_id
        LEFT JOIN (
            SELECT contract_id, SUM(payment_amount) as paid_amount
            FROM receipts
            WHERE payment_date IS NOT NULL
            GROUP BY contract_id
        ) p_sum ON c.contract_id = p_sum.contract_id
        LEFT JOIN (
            SELECT contract_id, SUM(invoice_amount) as invoiced_amount
            FROM invoices
            WHERE invoice_date IS NOT NULL
            GROUP BY contract_id
        ) inv_sum ON c.contract_id = inv_sum.contract_id
        LEFT JOIN (
            SELECT i.contract_id, SUM(s.quantity_shipped) AS total_shipped
            FROM shipments s
            JOIN items i ON s.item_id = i.item_id
            GROUP BY i.contract_id
        ) s_sum ON c.contract_id = s_sum.contract_id
        LEFT JOIN (
            SELECT contract_id, SUM(quantity) as total_quantity
            FROM items
            GROUP BY contract_id
        ) i_sum ON c.contract_id = i_sum.contract_id
        WHERE 1=1
    """

    params = {}

    # Add filter conditions based on the parameters
    if year:
        sql_query += " AND YEAR(c.order_date) = :year"
        params['year'] = year
    if month:
        sql_query += " AND MONTH(c.order_date) = :month"
        params['month'] = month
    if selected_company_id:
        sql_query += " AND c.company_id = :company_id"
        params['company_id'] = selected_company_id

    sql_query += " GROUP BY c.contract_id ORDER BY c.contract_id DESC"

    try:
        companies = get_companies()
        result = db.session.execute(text(sql_query), params).mappings().fetchall()
    except Exception as e:
        return f"Database connection error: {str(e)} <br> Please check that the .env file and ca.pem are correct."

    return render_template(
        'dashboard.html',
        orders=result,
        selected_year=year,
        selected_month=month,
        selected_company_id=selected_company_id,
        companies=companies,
        years=years
    )

@app.route('/statistics')
def statistics():
    """Statistics page: supports filtering sales amounts by client name and product"""

    current_year = datetime.date.today().year
    selected_year = request.args.get('year', default=current_year, type=int)
    client_id = request.args.get('client_id', type=int)
    client_name = (request.args.get('client_name') or '').strip()
    product_id = request.args.get('product_id', type=int)
    selected_company_id = get_selected_company_id(default_to_primary=True)
    chart_mode = request.args.get('chart_mode', 'company')
    if chart_mode not in ('company', 'product'):
        chart_mode = 'company'
    po_chart_mode = request.args.get('po_chart_mode', 'supplier')
    if po_chart_mode not in ('supplier', 'material'):
        po_chart_mode = 'supplier'

    where_clauses = []
    params = {
        'selected_year': selected_year
    }

    where_clauses.append("YEAR(c.order_date) = :selected_year")

    if client_id:
        where_clauses.append("cl.client_id = :client_id")
        params['client_id'] = client_id

    if client_name:
        where_clauses.append("cl.company_name LIKE :client_name")
        params['client_name'] = f"%{client_name}%"

    if product_id:
        where_clauses.append("i.product_id = :product_id")
        params['product_id'] = product_id

    if selected_company_id:
        where_clauses.append("c.company_id = :company_id")
        params['company_id'] = selected_company_id

    where_sql = " AND ".join(where_clauses)

    try:
        year_rows = db.session.execute(text("""
            SELECT DISTINCT YEAR(order_date) AS order_year
            FROM contracts
            WHERE order_date IS NOT NULL
            ORDER BY order_year DESC
        """)).mappings().fetchall()

        years = []
        for row in year_rows:
            if row['order_year'] is not None:
                years.append(int(row['order_year']))
        if current_year not in years:
            years.insert(0, current_year)
        years = sorted(set(years), reverse=True)

        companies = get_companies()
        products = db.session.execute(text("""
            SELECT product_id, chinese_name
            FROM products
            ORDER BY chinese_name
        """)).mappings().fetchall()

        summary = db.session.execute(text(f"""
            SELECT
                COALESCE(SUM(i.quantity * COALESCE(i.unit_price, 0)), 0) AS total_sales_amount,
                COALESCE(SUM(i.quantity), 0) AS total_quantity,
                COUNT(DISTINCT c.contract_id) AS contract_count,
                COUNT(DISTINCT c.client_id) AS client_count,
                COUNT(DISTINCT i.product_id) AS product_count
            FROM items i
            JOIN contracts c ON i.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            WHERE {where_sql}
        """), params).mappings().first()

        client_stats = db.session.execute(text(f"""
            SELECT
                cl.client_id,
                cl.company_name,
                COUNT(DISTINCT c.contract_id) AS contract_count,
                COALESCE(SUM(i.quantity), 0) AS total_quantity,
                COALESCE(SUM(i.quantity * COALESCE(i.unit_price, 0)), 0) AS sales_amount
            FROM items i
            JOIN contracts c ON i.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            WHERE {where_sql}
            GROUP BY cl.client_id, cl.company_name
            ORDER BY sales_amount DESC, cl.company_name
        """), params).mappings().fetchall()

        product_stats = db.session.execute(text(f"""
            SELECT
                p.product_id,
                p.chinese_name,
                COUNT(DISTINCT c.contract_id) AS contract_count,
                COALESCE(SUM(i.quantity), 0) AS total_quantity,
                COALESCE(AVG(i.unit_price), 0) AS avg_unit_price,
                COALESCE(SUM(i.quantity * COALESCE(i.unit_price, 0)), 0) AS sales_amount
            FROM items i
            JOIN contracts c ON i.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            WHERE {where_sql}
            GROUP BY p.product_id, p.chinese_name
            ORDER BY sales_amount DESC, p.chinese_name
        """), params).mappings().fetchall()

        contract_details = db.session.execute(text(f"""
            SELECT
                c.contract_id,
                cp.short_name AS ledger_short_name,
                c.contract_no,
                c.order_date,
                cl.company_name,
                p.chinese_name,
                i.quantity,
                COALESCE(i.unit_price, 0) AS unit_price,
                i.quantity * COALESCE(i.unit_price, 0) AS line_amount
            FROM items i
            JOIN contracts c ON i.contract_id = c.contract_id
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            WHERE {where_sql}
            ORDER BY c.order_date DESC, c.contract_id DESC, i.item_id DESC
        """), params).mappings().fetchall()

        monthly_sales_rows = db.session.execute(text(f"""
            SELECT
                MONTH(c.order_date) AS sale_month,
                COALESCE(SUM(i.quantity * COALESCE(i.unit_price, 0)), 0) AS sales_amount
            FROM items i
            JOIN contracts c ON i.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            WHERE {where_sql}
            GROUP BY MONTH(c.order_date)
            ORDER BY sale_month
        """), params).mappings().fetchall()

        # --- Procurement Statistics ---
        po_where_clauses = ["YEAR(po.order_date) = :selected_year"]
        po_params = {'selected_year': selected_year}
        if selected_company_id:
            po_where_clauses.append("po.company_id = :company_id")
            po_params['company_id'] = selected_company_id
        po_where_sql = " AND ".join(po_where_clauses)

        procurement_summary = db.session.execute(text(f"""
            SELECT
                COALESCE(SUM(poi.quantity * poi.unit_price), 0) AS total_purchase_amount,
                COALESCE(SUM(poi.quantity), 0) AS total_purchase_quantity,
                COUNT(DISTINCT po.po_id) AS po_count,
                COUNT(DISTINCT po.supplier_id) AS supplier_count,
                COUNT(DISTINCT poi.component_id) AS material_count
            FROM purchase_order_items poi
            JOIN purchase_orders po ON poi.po_id = po.po_id
            WHERE {po_where_sql}
        """), po_params).mappings().first()

        supplier_stats = db.session.execute(text(f"""
            SELECT
                s.supplier_id,
                s.supplier_name,
                COUNT(DISTINCT po.po_id) AS po_count,
                COALESCE(SUM(poi.quantity), 0) AS total_quantity,
                COALESCE(SUM(poi.quantity * poi.unit_price), 0) AS purchase_amount
            FROM purchase_order_items poi
            JOIN purchase_orders po ON poi.po_id = po.po_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            WHERE {po_where_sql}
            GROUP BY s.supplier_id, s.supplier_name
            ORDER BY purchase_amount DESC, s.supplier_name
        """), po_params).mappings().fetchall()

        material_stats = db.session.execute(text(f"""
            SELECT
                c.component_id,
                c.component_name,
                COUNT(DISTINCT po.po_id) AS po_count,
                COALESCE(SUM(poi.quantity), 0) AS total_quantity,
                COALESCE(AVG(poi.unit_price), 0) AS avg_unit_price,
                COALESCE(SUM(poi.quantity * poi.unit_price), 0) AS purchase_amount
            FROM purchase_order_items poi
            JOIN purchase_orders po ON poi.po_id = po.po_id
            JOIN components c ON poi.component_id = c.component_id
            WHERE {po_where_sql}
            GROUP BY c.component_id, c.component_name
            ORDER BY purchase_amount DESC, c.component_name
        """), po_params).mappings().fetchall()

        po_details = db.session.execute(text(f"""
            SELECT
                po.po_id,
                po.po_no,
                cp.short_name AS ledger_short_name,
                s.supplier_name,
                po.order_date,
                c.component_name,
                poi.quantity,
                poi.unit_price,
                poi.quantity * poi.unit_price AS line_amount
            FROM purchase_order_items poi
            JOIN purchase_orders po ON poi.po_id = po.po_id
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            JOIN components c ON poi.component_id = c.component_id
            WHERE {po_where_sql}
            ORDER BY po.order_date DESC, po.po_id DESC, poi.poi_id DESC
        """), po_params).mappings().fetchall()

        monthly_purchase_rows = db.session.execute(text(f"""
            SELECT
                MONTH(po.order_date) AS purchase_month,
                COALESCE(SUM(poi.quantity * poi.unit_price), 0) AS purchase_amount
            FROM purchase_order_items poi
            JOIN purchase_orders po ON poi.po_id = po.po_id
            WHERE {po_where_sql}
            GROUP BY MONTH(po.order_date)
            ORDER BY purchase_month
        """), po_params).mappings().fetchall()

        # --- Raw Material Inventory Valuation — current snapshot, unaffected by year/company filters ---
        inventory_valuation = db.session.execute(text("""
            SELECT
                c.component_id,
                c.component_name,
                SUM(b.quantity_available) AS quantity_available,
                MAX(b.unit) AS unit,
                CASE WHEN SUM(b.quantity_available) > 0
                     THEN SUM(b.quantity_available * COALESCE(b.unit_cost, 0)) / SUM(b.quantity_available)
                     ELSE 0 END AS avg_unit_cost,
                SUM(b.quantity_available * COALESCE(b.unit_cost, 0)) AS inventory_value
            FROM batches b
            JOIN components c ON b.component_id = c.component_id
            WHERE b.batch_type = 'received' AND b.status = 'available' AND b.quantity_available > 0
            GROUP BY c.component_id, c.component_name
            ORDER BY inventory_value DESC, c.component_name
        """)).mappings().fetchall()
    except Exception as e:
        return f"Database connection error: {str(e)} <br> Please check that the .env file and ca.pem are correct."

    total_sales_amount = summary['total_sales_amount'] or Decimal('0')
    contract_count = summary['contract_count'] or 0
    avg_contract_amount = (
        total_sales_amount / contract_count if contract_count else Decimal('0')
    )

    client_chart_data = [
        {
            'id': row['client_id'],
            'label': row['company_name'],
            'value': float(row['sales_amount'] or 0)
        }
        for row in client_stats
        if row['sales_amount']
    ]

    product_chart_data = [
        {
            'id': row['product_id'],
            'label': row['chinese_name'],
            'value': float(row['sales_amount'] or 0)
        }
        for row in product_stats
        if row['sales_amount']
    ]

    monthly_sales_map = {
        int(row['sale_month']): float(row['sales_amount'] or 0)
        for row in monthly_sales_rows
        if row['sale_month'] is not None
    }
    monthly_sales_data = [
        {
            'month': month,
            'label': f'Month {month}',
            'value': monthly_sales_map.get(month, 0)
        }
        for month in range(1, 13)
    ]

    total_purchase_amount = procurement_summary['total_purchase_amount'] or Decimal('0')
    po_count = procurement_summary['po_count'] or 0
    avg_po_amount = (
        total_purchase_amount / po_count if po_count else Decimal('0')
    )

    supplier_chart_data = [
        {
            'id': row['supplier_id'],
            'label': row['supplier_name'],
            'value': float(row['purchase_amount'] or 0)
        }
        for row in supplier_stats
        if row['purchase_amount']
    ]

    material_chart_data = [
        {
            'id': row['component_id'],
            'label': row['component_name'],
            'value': float(row['purchase_amount'] or 0)
        }
        for row in material_stats
        if row['purchase_amount']
    ]

    monthly_purchase_map = {
        int(row['purchase_month']): float(row['purchase_amount'] or 0)
        for row in monthly_purchase_rows
        if row['purchase_month'] is not None
    }
    monthly_purchase_data = [
        {
            'month': month,
            'label': f'Month {month}',
            'value': monthly_purchase_map.get(month, 0)
        }
        for month in range(1, 13)
    ]

    inventory_valuation_total = sum(float(row['inventory_value'] or 0) for row in inventory_valuation)

    filters = {
        'year': selected_year,
        'client_id': client_id,
        'client_name': client_name,
        'product_id': product_id,
        'company_id': selected_company_id,
        'chart_mode': chart_mode,
        'po_chart_mode': po_chart_mode
    }

    return render_template(
        'statistics.html',
        years=years,
        products=products,
        companies=companies,
        filters=filters,
        summary=summary,
        avg_contract_amount=avg_contract_amount,
        client_stats=client_stats,
        product_stats=product_stats,
        contract_details=contract_details,
        client_chart_data=client_chart_data,
        product_chart_data=product_chart_data,
        monthly_sales_data=monthly_sales_data,
        procurement_summary=procurement_summary,
        avg_po_amount=avg_po_amount,
        supplier_stats=supplier_stats,
        material_stats=material_stats,
        po_details=po_details,
        supplier_chart_data=supplier_chart_data,
        material_chart_data=material_chart_data,
        monthly_purchase_data=monthly_purchase_data,
        inventory_valuation=inventory_valuation,
        inventory_valuation_total=inventory_valuation_total
    )


@app.route('/ai-query', methods=['GET', 'POST'])
def ai_query():
    question = ''
    result = None
    error = None

    if request.method == 'POST':
        question = (request.form.get('question') or '').strip()
        if not question:
            error = 'Please enter a business question to run.'
        else:
            try:
                sql_prompt = f"""
{CRM_SCHEMA_PROMPT}

User question:
{question}
"""
                ai_plan = call_gemini_json(sql_prompt)
                safe_sql = validate_ai_sql(ai_plan.get('sql'))

                rows = db.session.execute(text(safe_sql)).mappings().fetchall()
                json_rows = rows_to_json(rows)
                columns = list(json_rows[0].keys()) if json_rows else []

                chart_type = ai_plan.get('chart_type') or 'none'
                if chart_type not in ('bar', 'line', 'pie', 'none'):
                    chart_type = 'none'

                chart_data = build_chart_data(
                    json_rows,
                    ai_plan.get('label_column'),
                    ai_plan.get('value_column')
                )
                if not chart_data:
                    chart_type = 'none'

                analysis_prompt = f"""
You are a business analyst for NorthPeak Biotech ERP.
Answer in concise English.
The user asked: {question}
SQL executed: {safe_sql}
Rows JSON, max 200 rows:
{json.dumps(json_rows[:AI_QUERY_MAX_ROWS], ensure_ascii=False)}

Return JSON only:
{{
  "summary": "2-4 sentence English business interpretation",
  "insights": ["short insight 1", "short insight 2", "short insight 3"]
}}
"""
                analysis = call_gemini_json(analysis_prompt)

                result = {
                    'title': ai_plan.get('title') or 'Smart Query Result',
                    'sql': safe_sql,
                    'columns': columns,
                    'rows': json_rows,
                    'row_count': len(json_rows),
                    'chart_type': chart_type,
                    'chart_data': chart_data,
                    'summary': analysis.get('summary') or '',
                    'insights': analysis.get('insights') or [],
                }
            except Exception as exc:
                error = str(exc)

    examples = [
        "This year's sales, amount received, and amount outstanding per client, sorted by outstanding amount descending",
        "Monthly sales trend for this year",
        'Orders that have shipped but are not yet fully paid',
        "Each product's sales quantity and sales amount this year",
        'Samples sent vs. not yet sent, grouped by client',
    ]
    return render_template(
        'ai_query.html',
        question=question,
        result=result,
        error=error,
        examples=examples
    )


@app.route('/new-order', methods=['GET', 'POST'])
def new_order():
    """Page and handling logic for creating a new order."""
    if request.method == 'POST':
        try:
            # --- 1. Insert into the contracts master table ---
            contract_sql = text("""
                INSERT INTO contracts (
                    company_id, source_contract_id, client_id, contact_id, contract_no,
                    order_date, delivery_deadline, shipping_address, payment_method, notes, total_amount
                )
                VALUES (
                    :company_id, :source_contract_id, :client_id, :contact_id, :contract_no,
                    :order_date, :delivery_deadline, :shipping_address, :payment_method, :notes, 0
                )
            """)

            # Handle the optional contact_id
            contact_id = request.form.get('contact_id')
            selected_company_id = request.form.get('company_id') or get_default_company_id()

            result = db.session.execute(contract_sql, {
                'company_id': selected_company_id,
                'source_contract_id': request.form.get('source_contract_id') or None,
                'client_id': request.form['client_id'],
                'contact_id': contact_id if contact_id else None,
                'contract_no': request.form.get('contract_no'),
                'order_date': request.form['order_date'],
                'delivery_deadline': request.form.get('delivery_deadline') or None,
                'shipping_address': request.form.get('shipping_address'),
                'payment_method': request.form.get('payment_method'),
                'notes': request.form.get('notes')
            })
            
            # Get the newly inserted contract ID
            contract_id = result.lastrowid
            total_amount = Decimal(0)

            # --- 2. Insert into the order line-items table ---
            product_ids = request.form.getlist('product_id')
            quantities = request.form.getlist('quantity')
            unit_prices = request.form.getlist('unit_price')
            agreed_cfus = request.form.getlist('agreed_cfu')
            packaging_specs = request.form.getlist('packaging_spec')

            item_sql = text("""
                INSERT INTO items (contract_id, product_id, quantity, unit_price, agreed_cfu, packaging_spec)
                VALUES (:contract_id, :product_id, :quantity, :unit_price, :agreed_cfu, :packaging_spec)
            """)

            for i in range(len(product_ids)):
                quantity = Decimal(quantities[i])
                unit_price = Decimal(unit_prices[i])
                total_amount += quantity * unit_price
                
                db.session.execute(item_sql, {
                    'contract_id': contract_id,
                    'product_id': product_ids[i],
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'agreed_cfu': agreed_cfus[i] or None,
                    'packaging_spec': packaging_specs[i] or '1 kg/bag'
                })
            
            # --- 3. Update the contract total amount ---
            update_contract_sql = text("UPDATE contracts SET total_amount = :total_amount WHERE contract_id = :contract_id")
            db.session.execute(update_contract_sql, {'total_amount': total_amount, 'contract_id': contract_id})

            ensure_primary_secondary_supply_contract(contract_id)
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            return f"Failed to add order: {str(e)}"

        return redirect(url_for('dashboard', company_id=selected_company_id))

    else: # GET request
        try:
            companies = get_companies()
            clients = db.session.execute(text("SELECT client_id, company_name FROM clients ORDER BY company_name")).fetchall()
            products = db.session.execute(text("""
                SELECT product_id, chinese_name, NULL AS standard_cfu
                FROM products
                ORDER BY chinese_name
            """)).fetchall()
            source_contracts = db.session.execute(text("""
                SELECT
                    c.contract_id,
                    c.contract_no,
                    c.order_date,
                    cp.short_name AS ledger_short_name,
                    cl.company_name,
                    c.total_amount
                FROM contracts c
                LEFT JOIN companies cp ON c.company_id = cp.company_id
                JOIN clients cl ON c.client_id = cl.client_id
                ORDER BY c.order_date DESC, c.contract_id DESC
                LIMIT 500
            """)).mappings().fetchall()
        except Exception as e:
             return f"Database connection error: {str(e)} <br> Please check that the .env file and ca.pem are correct."

        return render_template(
            'new_order.html',
            clients=clients,
            products=products,
            companies=companies,
            source_contracts=source_contracts,
            default_company_id=get_default_company_id()
        )


@app.route('/edit-order/<int:contract_id>', methods=['GET', 'POST'])
def edit_order(contract_id):
    """Page and handling logic for editing an order."""
    if request.method == 'POST':
        try:
            # --- 1. Update the contracts master table ---
            contract_sql = text("""
                UPDATE contracts 
                SET company_id = :company_id,
                    source_contract_id = :source_contract_id,
                    client_id = :client_id,
                    contact_id = :contact_id, 
                    contract_no = :contract_no, 
                    order_date = :order_date, 
                    delivery_deadline = :delivery_deadline, 
                    shipping_address = :shipping_address, 
                    payment_method = :payment_method,
                    notes = :notes
                WHERE contract_id = :contract_id
            """)

            contact_id_form = request.form.get('contact_id')
            selected_company_id = request.form.get('company_id') or get_default_company_id()

            db.session.execute(contract_sql, {
                'company_id': selected_company_id,
                'source_contract_id': request.form.get('source_contract_id') or None,
                'client_id': request.form['client_id'],
                'contact_id': contact_id_form if contact_id_form else None,
                'contract_no': request.form.get('contract_no'),
                'order_date': request.form['order_date'],
                'delivery_deadline': request.form.get('delivery_deadline') or None,
                'shipping_address': request.form.get('shipping_address'),
                'payment_method': request.form.get('payment_method'),
                'notes': request.form.get('notes'),
                'contract_id': contract_id
            })

            # --- 2. Process order line items ---
            total_amount = Decimal(0)

            # Get all item data from the form
            item_ids = request.form.getlist('item_id')
            product_ids = request.form.getlist('product_id')
            quantities = request.form.getlist('quantity')
            unit_prices = request.form.getlist('unit_price')
            agreed_cfus = request.form.getlist('agreed_cfu')
            packaging_specs = request.form.getlist('packaging_spec')

            # Get the item_ids that already exist in the database
            existing_items_sql = text("SELECT item_id FROM items WHERE contract_id = :contract_id")
            existing_item_ids_result = db.session.execute(existing_items_sql, {'contract_id': contract_id}).fetchall()
            existing_item_ids = {item[0] for item in existing_item_ids_result}
            
            submitted_item_ids = {int(item_id) for item_id in item_ids if item_id}

            # --- 3. Delete order items that were removed ---
            ids_to_delete = existing_item_ids - submitted_item_ids
            if ids_to_delete:
                for item_id_to_delete in ids_to_delete:
                    # Check whether it has shipment records
                    shipment_check_sql = text("SELECT COUNT(*) FROM shipments WHERE item_id = :item_id")
                    shipment_count = db.session.execute(shipment_check_sql, {'item_id': item_id_to_delete}).scalar()
                    if shipment_count > 0:
                        db.session.rollback()
                        return f"Failed to update order: cannot delete order item ID {item_id_to_delete} because it already has associated shipment records."

                # Safely build the IN clause
                if ids_to_delete:
                    delete_sql = text("DELETE FROM items WHERE item_id IN :ids")
                    db.session.execute(delete_sql, {'ids': tuple(ids_to_delete)})

            # --- 4. Update or insert order line items ---
            update_item_sql = text("""
                UPDATE items 
                SET product_id = :product_id, quantity = :quantity, unit_price = :unit_price, 
                    agreed_cfu = :agreed_cfu, packaging_spec = :packaging_spec
                WHERE item_id = :item_id
            """)
            insert_item_sql = text("""
                INSERT INTO items (contract_id, product_id, quantity, unit_price, agreed_cfu, packaging_spec)
                VALUES (:contract_id, :product_id, :quantity, :unit_price, :agreed_cfu, :packaging_spec)
            """)

            for i in range(len(product_ids)):
                # Skip product rows with incomplete data
                if not product_ids[i] or not quantities[i] or not unit_prices[i]:
                    continue

                quantity = Decimal(quantities[i])
                unit_price = Decimal(unit_prices[i])
                total_amount += quantity * unit_price
                item_id = item_ids[i]

                params = {
                    'product_id': product_ids[i],
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'agreed_cfu': agreed_cfus[i] or None,
                    'packaging_spec': packaging_specs[i] or '1 kg/bag'
                }

                if item_id and int(item_id) in existing_item_ids:
                    # Update the existing item
                    params['item_id'] = int(item_id)
                    db.session.execute(update_item_sql, params)
                else:
                    # Insert a new item
                    params['contract_id'] = contract_id
                    db.session.execute(insert_item_sql, params)

            # --- 5. Update the contract total amount ---
            update_contract_sql = text("UPDATE contracts SET total_amount = :total_amount WHERE contract_id = :contract_id")
            db.session.execute(update_contract_sql, {'total_amount': total_amount, 'contract_id': contract_id})

            ensure_primary_secondary_supply_contract(contract_id)
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            # Avoid showing the raw SQLAlchemy error; show something more useful instead
            error_message = str(e)
            if "foreign key constraint fails" in error_message:
                return f"Failed to update order: the operation violated a database constraint, possibly an attempt to delete an order item that has already been shipped or paid."
            return f"Failed to update order: {error_message}"

        return redirect(url_for('dashboard', company_id=selected_company_id))

    else: # GET request
        try:
            contract_sql = text("SELECT * FROM contracts WHERE contract_id = :contract_id")
            contract = db.session.execute(contract_sql, {'contract_id': contract_id}).mappings().fetchone()
            if not contract:
                return "Order not found!", 404

            # Get the products associated with the contract
            items_sql = text("SELECT * FROM items WHERE contract_id = :contract_id ORDER BY item_id")
            items = db.session.execute(items_sql, {'contract_id': contract_id}).mappings().fetchall()
            companies = get_companies()
            source_contracts = db.session.execute(text("""
                SELECT
                    c.contract_id,
                    c.contract_no,
                    c.order_date,
                    cp.short_name AS ledger_short_name,
                    cl.company_name,
                    c.total_amount
                FROM contracts c
                LEFT JOIN companies cp ON c.company_id = cp.company_id
                JOIN clients cl ON c.client_id = cl.client_id
                WHERE c.contract_id <> :contract_id
                ORDER BY c.order_date DESC, c.contract_id DESC
                LIMIT 500
            """), {'contract_id': contract_id}).mappings().fetchall()

            # Get all clients and products for the dropdowns
            clients = db.session.execute(text("SELECT client_id, company_name FROM clients ORDER BY company_name")).fetchall()
            products = db.session.execute(text("""
                SELECT product_id, chinese_name, NULL AS standard_cfu
                FROM products
                ORDER BY chinese_name
            """)).fetchall()
            
            # Get contacts for the current client
            contacts_sql = text("SELECT contact_id, contact_name FROM contacts WHERE client_id = :client_id ORDER BY contact_name")
            contacts = db.session.execute(contacts_sql, {'client_id': contract.client_id}).fetchall()

        except Exception as e:
             return f"Database query failed: {str(e)}"

        return render_template(
            'edit_order.html',
            contract=contract,
            items=items,
            clients=clients,
            products=products,
            contacts=contacts,
            companies=companies,
            source_contracts=source_contracts
        )


@app.route('/delete-order/<int:contract_id>', methods=['POST'])
def delete_order(contract_id):
    """Delete an order and all its associated items"""
    selected_company_id = None
    try:
        selected_company_id = db.session.execute(text("""
            SELECT company_id
            FROM contracts
            WHERE contract_id = :contract_id
        """), {'contract_id': contract_id}).scalar()

        sql_delete_invoices = text("DELETE FROM invoices WHERE contract_id = :contract_id")
        db.session.execute(sql_delete_invoices, {'contract_id': contract_id})

        sql_delete_receipts = text("DELETE FROM receipts WHERE contract_id = :contract_id")
        db.session.execute(sql_delete_receipts, {'contract_id': contract_id})

        # Delete the associated product items
        sql_delete_items = text("DELETE FROM items WHERE contract_id = :contract_id")
        db.session.execute(sql_delete_items, {'contract_id': contract_id})

        # Delete the contract itself
        sql_delete_contract = text("DELETE FROM contracts WHERE contract_id = :contract_id")
        db.session.execute(sql_delete_contract, {'contract_id': contract_id})

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete order: {e}"

    if selected_company_id:
        return redirect(url_for('dashboard', company_id=selected_company_id))
    return redirect(url_for('dashboard'))

@app.route('/delete-sample/<path:sample_no>', methods=['POST'])
def delete_sample(sample_no):
    try:
        db.session.execute(text("DELETE FROM samples WHERE sample_no = :sno"), {'sno': sample_no})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete sample record: {e}"
    return redirect(url_for('samples'))


# --- API Endpoints (for frontend AJAX calls) ---

@app.route('/api/contracts/<int:contract_id>/items')
def get_contract_details_api(contract_id):
    """API: get detailed information for a contract (products, shipments, payments)"""
    try:
        # 0. Query the contract notes
        contract_sql = text("SELECT notes FROM contracts WHERE contract_id = :cid")
        contract_notes = db.session.execute(contract_sql, {'cid': contract_id}).scalar()

        # 1. Query the order's product line items
        items_sql = text("""
            SELECT 
                p.chinese_name,
                i.quantity,
                i.unit_price,
                i.agreed_cfu,
                i.packaging_spec
            FROM items i
            JOIN products p ON i.product_id = p.product_id
            WHERE i.contract_id = :cid
        """)
        items_rows = db.session.execute(items_sql, {'cid': contract_id}).mappings().all()
        # Convert the database results to a list of dicts
        items_data = [dict(row) for row in items_rows]

        # 2. Query invoice records
        invoices_sql = text("""
            SELECT
                invoice_date,
                invoice_amount,
                invoice_no,
                remark
            FROM invoices
            WHERE contract_id = :cid
            ORDER BY invoice_date DESC, invoice_id DESC
        """)
        invoice_rows = db.session.execute(invoices_sql, {'cid': contract_id}).mappings().all()
        invoices_data = []
        for row in invoice_rows:
            d = dict(row)
            if d['invoice_date']:
                d['invoice_date'] = str(d['invoice_date'])
            if d['invoice_amount'] is not None:
                d['invoice_amount'] = str(d['invoice_amount'])
            invoices_data.append(d)

        # 3. Query receipt records
        receipts_sql = text("""
            SELECT
                payment_date,
                payment_amount,
                remark
            FROM receipts
            WHERE contract_id = :cid
            ORDER BY payment_date DESC, receipt_id DESC
        """)
        receipt_rows = db.session.execute(receipts_sql, {'cid': contract_id}).mappings().all()
        receipts_data = []
        for row in receipt_rows:
            d = dict(row)
            if d['payment_date']:
                d['payment_date'] = str(d['payment_date'])
            if d['payment_amount'] is not None:
                d['payment_amount'] = str(d['payment_amount'])
            receipts_data.append(d)

        # 4. Query shipment records
        # Note: here we query tracking_no; if the frontend JS uses tracking_number, it needs to map to this
        shipments_sql = text("""
            SELECT 
                s.shipping_date,
                s.quantity_shipped,
                s.tracking_no,
                s.logistics_company,
                s.coa,
                p.chinese_name,
                i.item_id
            FROM shipments s
            JOIN items i ON s.item_id = i.item_id
            JOIN products p ON i.product_id = p.product_id
            WHERE i.contract_id = :cid
            ORDER BY s.shipping_date DESC
        """)
        shipments_rows = db.session.execute(shipments_sql, {'cid': contract_id}).mappings().all()
        shipments_data = []
        for row in shipments_rows:
            d = dict(row)
            if d['shipping_date']: d['shipping_date'] = str(d['shipping_date'])

            # [Important compat note] the frontend JS uses tracking_number and notes
            # so that the JS works without changes, we map them manually here:
            d['tracking_number'] = d.get('tracking_no')
            d['notes'] = d.get('logistics_company') # temporarily show the logistics company in the notes field

            shipments_data.append(d)

        # Package and return as JSON
        return jsonify({
            'notes': contract_notes,
            'items': items_data,
            'invoices': invoices_data,
            'receipts': receipts_data,
            'shipments': shipments_data
        })

    except Exception as e:
        print(f"API Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/clients/<int:client_id>/contacts')
def get_contacts(client_id):
    """Get the contact list for a client ID (for AJAX)"""
    try:
        sql = text("SELECT contact_id, contact_name FROM contacts WHERE client_id = :client_id ORDER BY contact_name")
        contacts = db.session.execute(sql, {'client_id': client_id}).fetchall()

        # Convert the results to a list of dicts for JSON serialization
        contact_list = [{'contact_id': c.contact_id, 'contact_name': c.contact_name} for c in contacts]
        
        return jsonify(contact_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/clients/<int:client_id>/shipping_addresses')
def get_shipping_addresses(client_id):
    """Get the shipping address list for a client ID (for AJAX)"""
    try:
        sql = text("SELECT address_id, address, is_default FROM shipping_addresses WHERE client_id = :client_id ORDER BY is_default DESC, address")
        addresses = db.session.execute(sql, {'client_id': client_id}).mappings().fetchall()
        return jsonify([dict(row) for row in addresses])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sample-no')
def api_sample_no():
    try:
        order_date = request.args.get('order_date')
        company_id = request.args.get('company_id', type=int)
        date_obj = None
        if order_date:
            try:
                date_obj = datetime.date.fromisoformat(order_date)
            except ValueError:
                date_obj = None
        return jsonify({'sample_no': generate_sample_no(date_obj, company_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/clients')
def clients():
    """Client management page"""
    try:
        # 1. Get all clients
        clients_sql = text("SELECT * FROM clients ORDER BY company_name")
        clients_result = db.session.execute(clients_sql).mappings().all()

        # 2. Get all contacts
        contacts_sql = text("SELECT * FROM contacts")
        contacts_result = db.session.execute(contacts_sql).mappings().all()

        # 3. Group contacts by client_id
        contacts_by_client = {}
        for contact in contacts_result:
            if contact['client_id'] not in contacts_by_client:
                contacts_by_client[contact['client_id']] = []
            contacts_by_client[contact['client_id']].append(contact)

        # 4. Get all shipping addresses
        addresses_sql = text("SELECT * FROM shipping_addresses")
        addresses_result = db.session.execute(addresses_sql).mappings().all()

        # 5. Group shipping addresses by client_id
        addresses_by_client = {}
        for address in addresses_result:
            if address['client_id'] not in addresses_by_client:
                addresses_by_client[address['client_id']] = []
            addresses_by_client[address['client_id']].append(address)

        # 6. Attach the contact list and address list to each client
        clients_with_details = []
        for client in clients_result:
            client_dict = dict(client)
            client_dict['contacts'] = contacts_by_client.get(client['client_id'], [])
            client_dict['shipping_addresses'] = addresses_by_client.get(client['client_id'], [])
            clients_with_details.append(client_dict)

    except Exception as e:
        return f"Database query error: {e}"
        
    return render_template('clients.html', clients=clients_with_details)


@app.route('/add-client', methods=['POST'])
def add_client():
    """Add a new client"""
    try:
        sql = text("""
            INSERT INTO clients (company_name, company_address, tax_id) 
            VALUES (:company_name, :company_address, :tax_id)
        """)
        db.session.execute(sql, {
            'company_name': request.form['company_name'],
            'company_address': request.form.get('company_address'),
            'tax_id': request.form.get('tax_id')
        })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to add client: {e}"
    
    return redirect(url_for('clients'))


@app.route('/add-contact', methods=['POST'])
def add_contact():
    """Add a new contact for a given client"""
    try:
        sql = text("""
            INSERT INTO contacts (client_id, contact_name, title, cellphone, other_info) 
            VALUES (:client_id, :contact_name, :title, :cellphone, :other_info)
        """)
        db.session.execute(sql, {
            'client_id': request.form['client_id'],
            'contact_name': request.form['contact_name'],
            'title': request.form.get('title'),
            'cellphone': request.form.get('cellphone'),
            'other_info': request.form.get('other_info'),
        })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to add contact: {e}"

    return redirect(url_for('clients'))


@app.route('/edit-client/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    """Edit client information"""
    if request.method == 'POST':
        try:
            sql = text("""
                UPDATE clients 
                SET company_name = :company_name, 
                    company_address = :company_address, 
                    tax_id = :tax_id
                WHERE client_id = :client_id
            """)
            db.session.execute(sql, {
                'company_name': request.form['company_name'],
                'company_address': request.form.get('company_address'),
                'tax_id': request.form.get('tax_id'),
                'client_id': client_id
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update client: {e}"
        return redirect(url_for('clients'))
    else: # GET
        try:
            sql = text("SELECT * FROM clients WHERE client_id = :client_id")
            client = db.session.execute(sql, {'client_id': client_id}).mappings().fetchone()
            if not client:
                return "Client not found!", 404
        except Exception as e:
            return f"Database query error: {e}"
        
        return render_template('edit_client.html', client=client)

@app.route('/edit-contact/<int:contact_id>', methods=['GET', 'POST'])
def edit_contact(contact_id):
    """Edit contact information"""
    if request.method == 'POST':
        try:
            sql = text("""
                UPDATE contacts 
                SET contact_name = :contact_name, 
                    title = :title, 
                    cellphone = :cellphone, 
                    other_info = :other_info
                WHERE contact_id = :contact_id
            """)
            db.session.execute(sql, {
                'contact_name': request.form['contact_name'],
                'title': request.form.get('title'),
                'cellphone': request.form.get('cellphone'),
                'other_info': request.form.get('other_info'),
                'contact_id': contact_id
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update contact: {e}"
        return redirect(url_for('clients'))
    else: # GET
        try:
            sql = text("SELECT * FROM contacts WHERE contact_id = :contact_id")
            contact = db.session.execute(sql, {'contact_id': contact_id}).mappings().fetchone()
            if not contact:
                return "Contact not found!", 404
        except Exception as e:
            return f"Database query error: {e}"
        
        return render_template('edit_contact.html', contact=contact)


@app.route('/delete-client/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    """Delete a client and all its contacts"""
    try:
        # Check whether any contracts are linked to this client
        sql_check = text("SELECT COUNT(*) FROM contracts WHERE client_id = :client_id")
        count = db.session.execute(sql_check, {'client_id': client_id}).scalar()
        if count > 0:
            return f"Cannot delete: this client is linked to {count} order(s).", 400

        # Delete all of the client's contacts
        sql_delete_contacts = text("DELETE FROM contacts WHERE client_id = :client_id")
        db.session.execute(sql_delete_contacts, {'client_id': client_id})

        # Delete the client
        sql_delete_client = text("DELETE FROM clients WHERE client_id = :client_id")
        db.session.execute(sql_delete_client, {'client_id': client_id})

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete client: {e}"

    return redirect(url_for('clients'))


@app.route('/delete-contact/<int:contact_id>', methods=['POST'])
def delete_contact(contact_id):
    """Delete a contact"""
    try:
        # Check whether any contracts are linked to this contact
        sql_check = text("SELECT COUNT(*) FROM contracts WHERE contact_id = :contact_id")
        count = db.session.execute(sql_check, {'contact_id': contact_id}).scalar()
        if count > 0:
            return f"Cannot delete: this contact is linked to {count} order(s).", 400

        sql = text("DELETE FROM contacts WHERE contact_id = :contact_id")
        db.session.execute(sql, {'contact_id': contact_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete contact: {e}"

    return redirect(url_for('clients'))


@app.route('/add_shipping_address', methods=['POST'])
def add_shipping_address():
    """Add a new shipping address"""
    try:
        client_id = request.form['client_id']
        address = request.form['address']
        is_default = 'is_default' in request.form

        # If setting as default, unset the default flag on the client's other addresses
        if is_default:
            sql_unset = text("UPDATE shipping_addresses SET is_default = 0 WHERE client_id = :client_id")
            db.session.execute(sql_unset, {'client_id': client_id})

        sql = text("""
            INSERT INTO shipping_addresses (client_id, address, is_default) 
            VALUES (:client_id, :address, :is_default)
        """)
        db.session.execute(sql, {
            'client_id': client_id,
            'address': address,
            'is_default': 1 if is_default else 0
        })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to add address: {e}"
    return redirect(url_for('clients'))


@app.route('/delete_shipping_address/<int:address_id>', methods=['POST'])
def delete_shipping_address(address_id):
    """Delete a shipping address"""
    try:
        sql = text("DELETE FROM shipping_addresses WHERE address_id = :address_id")
        db.session.execute(sql, {'address_id': address_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete address: {e}"
    return redirect(url_for('clients'))


@app.route('/set_default_shipping_address/<int:client_id>/<int:address_id>', methods=['POST'])
def set_default_shipping_address(client_id, address_id):
    """Set the default shipping address"""
    try:
        # Unset the default flag on all of this client's addresses
        sql_unset = text("UPDATE shipping_addresses SET is_default = 0 WHERE client_id = :client_id")
        db.session.execute(sql_unset, {'client_id': client_id})

        # Set the new default address
        sql_set = text("UPDATE shipping_addresses SET is_default = 1 WHERE address_id = :address_id")
        db.session.execute(sql_set, {'address_id': address_id})

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to set default address: {e}"
    return redirect(url_for('clients'))


@app.route('/edit_shipping_address/<int:address_id>', methods=['GET', 'POST'])
def edit_shipping_address(address_id):
    """Edit a shipping address"""
    if request.method == 'POST':
        try:
            address = request.form['address']
            is_default = 'is_default' in request.form
            client_id = request.form['client_id']

            # If setting as default, unset the default flag on the client's other addresses
            if is_default:
                sql_unset = text("UPDATE shipping_addresses SET is_default = 0 WHERE client_id = :client_id")
                db.session.execute(sql_unset, {'client_id': client_id})

            sql = text("""
                UPDATE shipping_addresses 
                SET address = :address, is_default = :is_default
                WHERE address_id = :address_id
            """)
            db.session.execute(sql, {
                'address': address,
                'is_default': 1 if is_default else 0,
                'address_id': address_id
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update address: {e}"
        return redirect(url_for('clients'))
    else: # GET
        try:
            sql = text("SELECT * FROM shipping_addresses WHERE address_id = :address_id")
            address = db.session.execute(sql, {'address_id': address_id}).mappings().fetchone()
            if not address:
                return "Address not found!", 404
        except Exception as e:
            return f"Database query error: {e}"
        
        return render_template('edit_shipping_address.html', address=address)




@app.route('/products')
def products():
    """Product management page"""
    try:
        sql = text("SELECT * FROM products ORDER BY chinese_name")
        products_result = db.session.execute(sql).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
        
    return render_template('products.html', products=products_result, potency_units=POTENCY_UNITS)


@app.route('/add-product', methods=['POST'])
def add_product():
    """Add a new product"""
    try:
        product_data = parse_product_form()
        sql = text("""
            INSERT INTO products (chinese_name, process_type, description)
            VALUES (:chinese_name, :process_type, :description)
        """)
        db.session.execute(sql, product_data)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to add product: {e}"

    return redirect(url_for('products'))


@app.route('/edit-product/<int:product_id>', methods=['GET', 'POST'])
def edit_product(product_id):
    """Edit product information"""
    if request.method == 'POST':
        try:
            product_data = parse_product_form()
            sql = text("""
                UPDATE products
                SET chinese_name = :chinese_name,
                    process_type = :process_type,
                    description = :description
                WHERE product_id = :product_id
            """)
            product_data['product_id'] = product_id
            db.session.execute(sql, product_data)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update product: {e}"
        return redirect(url_for('products'))
    else: # GET
        try:
            sql = text("SELECT * FROM products WHERE product_id = :product_id")
            product = db.session.execute(sql, {'product_id': product_id}).mappings().fetchone()
            if not product:
                return "Product not found!", 404
        except Exception as e:
            return f"Database query error: {e}"
        
        return render_template('edit_product.html', product=product, potency_units=POTENCY_UNITS)


@app.route('/delete-product/<int:product_id>', methods=['POST'])
def delete_product(product_id):
    """Delete a product"""
    try:
        # Before deleting, check if the product is used in any contracts.
        # This is a safety check. You can decide if you want to enforce this.
        sql_check = text("SELECT COUNT(*) FROM items WHERE product_id = :product_id")
        count = db.session.execute(sql_check, {'product_id': product_id}).scalar()
        
        if count > 0:
            # You might want to return a more user-friendly error message
            return f"Cannot delete: this product is used by {count} order(s).", 400

        sql = text("DELETE FROM products WHERE product_id = :product_id")
        db.session.execute(sql, {'product_id': product_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete product: {e}"
    
    return redirect(url_for('products'))


@app.route('/shipments')
def shipments():
    """Shipment management page"""
    try:
        sql = text("""
            SELECT 
                s.shipment_id,
                s.shipping_date,
                s.quantity_shipped,
                s.tracking_no,
                s.logistics_company,
                c.contract_id,
                c.contract_no,
                cp.short_name AS ledger_short_name,
                cl.company_name,
                p.chinese_name,
                i.item_id
            FROM shipments s
            JOIN items i ON s.item_id = i.item_id
            JOIN contracts c ON i.contract_id = c.contract_id
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            JOIN products p ON i.product_id = p.product_id
            ORDER BY s.shipping_date DESC
        """)
        shipments_result = db.session.execute(sql).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"

    return render_template('shipments.html', shipments=shipments_result)

@app.route('/new-shipment', methods=['GET', 'POST'])
def new_shipment():
    """Create a new shipment record"""
    if request.method == 'POST':
        try:
            batch_id = request.form.get('batch_id') or None
            batch_id = int(batch_id) if batch_id else None
            quantity_shipped = Decimal(request.form['quantity_shipped'])
            if batch_id:
                batch = db.session.execute(text("""
                    SELECT batch_id, quantity_available
                    FROM batches
                    WHERE batch_id = :bid AND status = 'available'
                """), {'bid': batch_id}).mappings().fetchone()
                if not batch:
                    return "Shipment failed: the selected batch does not exist or has been disabled.", 400
                if Decimal(str(batch['quantity_available'])) < quantity_shipped:
                    return f"Shipment failed: batch has {batch['quantity_available']} kg available, not enough to ship {quantity_shipped} kg.", 400
            result = db.session.execute(text("""
                INSERT INTO shipments (
                    item_id, batch_id, shipping_date, quantity_shipped,
                    tracking_no, logistics_company, coa
                ) VALUES (
                    :item_id, :batch_id, :shipping_date, :quantity_shipped,
                    :tracking_no, :logistics_company, :coa
                )
            """), {
                'item_id': request.form['item_id'],
                'batch_id': batch_id,
                'shipping_date': request.form['shipping_date'],
                'quantity_shipped': quantity_shipped,
                'tracking_no': request.form.get('tracking_no'),
                'logistics_company': request.form.get('logistics_company'),
                'coa': request.form.get('coa'),
            })
            if batch_id:
                db.session.execute(text("""
                    UPDATE batches
                    SET quantity_available = quantity_available - :qty,
                        status = CASE WHEN quantity_available - :qty <= 0 THEN 'depleted' ELSE status END
                    WHERE batch_id = :bid
                """), {'qty': quantity_shipped, 'bid': batch_id})
            sync_primary_secondary_internal_shipment(result.lastrowid)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add shipment record: {e}"
        return redirect(url_for('shipments'))
    else: # GET
        try:
            # Get all order items that can still be shipped (remaining quantity > 0)
            items_sql = text("""
                SELECT
                    i.item_id,
                    i.product_id,
                    c.contract_no,
                    cp.short_name AS ledger_short_name,
                    cl.company_name,
                    p.chinese_name,
                    i.quantity,
                    (i.quantity - IFNULL(shipped.total_shipped, 0)) AS remaining_quantity
                FROM items i
                JOIN contracts c ON i.contract_id = c.contract_id
                LEFT JOIN companies cp ON c.company_id = cp.company_id
                JOIN clients cl ON c.client_id = cl.client_id
                JOIN products p ON i.product_id = p.product_id
                LEFT JOIN (
                    SELECT item_id, SUM(quantity_shipped) AS total_shipped
                    FROM shipments
                    GROUP BY item_id
                ) AS shipped ON i.item_id = shipped.item_id
                HAVING remaining_quantity > 0
                ORDER BY c.order_date DESC, i.item_id
            """)
            shippable_items = db.session.execute(items_sql).mappings().all()
        except Exception as e:
            return f"Database query error: {e}"
        return render_template('new_shipment.html', items=shippable_items, today=datetime.date.today().strftime('%Y-%m-%d'))

@app.route('/edit-shipment/<int:shipment_id>', methods=['GET', 'POST'])
def edit_shipment(shipment_id):
    """Edit a shipment record"""
    if request.method == 'POST':
        try:
            old = db.session.execute(text("""
                SELECT quantity_shipped, batch_id FROM shipments WHERE shipment_id = :sid
            """), {'sid': shipment_id}).mappings().fetchone()
            if not old:
                return "Shipment record not found!", 404
            new_batch_id = request.form.get('batch_id')
            new_batch_id = int(new_batch_id) if new_batch_id else old['batch_id']
            new_qty = Decimal(request.form['quantity_shipped'])
            old_qty = Decimal(str(old['quantity_shipped']))
            if old['batch_id']:
                db.session.execute(text("""
                    UPDATE batches
                    SET quantity_available = quantity_available + :qty, status = 'available'
                    WHERE batch_id = :bid
                """), {'qty': old_qty, 'bid': old['batch_id']})
            if new_batch_id:
                batch = db.session.execute(text("""
                    SELECT quantity_available FROM batches
                    WHERE batch_id = :bid AND status = 'available'
                """), {'bid': new_batch_id}).mappings().fetchone()
                if not batch:
                    db.session.rollback()
                    return "Update failed: the selected batch does not exist or has been disabled.", 400
                if Decimal(str(batch['quantity_available'])) < new_qty:
                    db.session.rollback()
                    return f"Update failed: insufficient batch stock available ({batch['quantity_available']} kg).", 400
                db.session.execute(text("""
                    UPDATE batches
                    SET quantity_available = quantity_available - :qty,
                        status = CASE WHEN quantity_available - :qty <= 0 THEN 'depleted' ELSE status END
                    WHERE batch_id = :bid
                """), {'qty': new_qty, 'bid': new_batch_id})
            db.session.execute(text("""
                UPDATE shipments
                SET shipping_date = :shipping_date,
                    quantity_shipped = :quantity_shipped,
                    batch_id = :batch_id,
                    tracking_no = :tracking_no,
                    logistics_company = :logistics_company,
                    coa = :coa
                WHERE shipment_id = :shipment_id
            """), {
                'shipping_date': request.form['shipping_date'],
                'quantity_shipped': new_qty,
                'batch_id': new_batch_id,
                'tracking_no': request.form.get('tracking_no'),
                'logistics_company': request.form.get('logistics_company'),
                'coa': request.form.get('coa'),
                'shipment_id': shipment_id,
            })
            sync_primary_secondary_internal_shipment(shipment_id)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update shipment record: {e}"
        return redirect(url_for('shipments'))
    else: # GET
        try:
            sql = text("""
                SELECT
                    s.*,
                    c.contract_no,
                    cl.company_name,
                    p.chinese_name,
                    p.product_id,
                    i.quantity AS item_quantity,
                    (i.quantity - IFNULL(shipped.total_shipped, 0) + s.quantity_shipped) AS max_shippable,
                    b.batch_no, b.quantity_available AS batch_qty_available
                FROM shipments s
                JOIN items i ON s.item_id = i.item_id
                JOIN contracts c ON i.contract_id = c.contract_id
                JOIN clients cl ON c.client_id = cl.client_id
                JOIN products p ON i.product_id = p.product_id
                LEFT JOIN batches b ON s.batch_id = b.batch_id
                LEFT JOIN (
                    SELECT item_id, SUM(quantity_shipped) AS total_shipped
                    FROM shipments
                    GROUP BY item_id
                ) AS shipped ON i.item_id = shipped.item_id
                WHERE s.shipment_id = :shipment_id
            """)
            shipment = db.session.execute(sql, {'shipment_id': shipment_id}).mappings().fetchone()
            if not shipment:
                return "Shipment record not found!", 404
        except Exception as e:
            return f"Database query error: {e}"
        return render_template('edit_shipment.html', shipment=shipment)

@app.route('/delete-shipment/<int:shipment_id>', methods=['POST'])
def delete_shipment(shipment_id):
    """Delete a shipment record"""
    try:
        shipment = db.session.execute(text("""
            SELECT quantity_shipped, batch_id FROM shipments WHERE shipment_id = :sid
        """), {'sid': shipment_id}).mappings().fetchone()
        if shipment and shipment['batch_id']:
            db.session.execute(text("""
                UPDATE batches
                SET quantity_available = quantity_available + :qty, status = 'available'
                WHERE batch_id = :bid
            """), {'qty': shipment['quantity_shipped'], 'bid': shipment['batch_id']})
        delete_synced_internal_shipment(shipment_id)
        db.session.execute(text(
            "DELETE FROM shipments WHERE shipment_id = :shipment_id"
        ), {'shipment_id': shipment_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete shipment record: {e}"
    return redirect(url_for('shipments'))


@app.route('/samples')
def samples():
    try:
        rows = db.session.execute(text("""
            SELECT
                s.sample_no,
                MIN(s.request_date)    AS request_date,
                MIN(s.shipping_address) AS shipping_address,
                MIN(s.notes)           AS notes,
                MIN(cp.short_name)     AS ledger_short_name,
                MIN(cl.company_name)   AS company_name,
                MIN(co.contact_name)   AS contact_name,
                GROUP_CONCAT(p.chinese_name ORDER BY s.sample_id SEPARATOR ', ') AS product_names,
                SUM(s.quantity)        AS total_quantity,
                COUNT(s.sample_id)     AS item_count,
                MAX(s.shipping_date)   AS shipping_date,
                GROUP_CONCAT(DISTINCT NULLIF(s.tracking_no, '') SEPARATOR ', ') AS tracking_nos,
                COUNT(CASE WHEN s.shipping_date IS NOT NULL THEN 1 END) AS shipped_count
            FROM samples s
            LEFT JOIN companies cp ON s.company_id = cp.company_id
            JOIN clients cl ON s.client_id = cl.client_id
            LEFT JOIN contacts co ON s.contact_id = co.contact_id
            LEFT JOIN products p ON s.product_id = p.product_id
            GROUP BY s.company_id, s.sample_no
            ORDER BY MIN(s.request_date) DESC, s.sample_no DESC
        """)).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('samples.html', samples=rows)


@app.route('/new-sample', methods=['GET', 'POST'])
def new_sample():
    if request.method == 'POST':
        try:
            order_date = request.form['order_date']
            try:
                od_obj = datetime.date.fromisoformat(order_date)
            except ValueError:
                od_obj = None
            company_id = int(request.form.get('company_id') or get_default_company_id())
            manual_sample_no = (request.form.get('sample_no') or '').strip()
            sample_no = manual_sample_no
            if request.form.get('sample_no_mode') == 'auto' or not sample_no:
                sample_no = generate_sample_no(od_obj, company_id)
            client_id  = request.form['client_id']
            contact_id = request.form.get('contact_id') or None
            addr  = request.form.get('shipping_address') or ''
            notes = request.form.get('notes') or None
            product_ids     = request.form.getlist('product_id')
            quantities      = request.form.getlist('quantity')
            agreed_cfus     = request.form.getlist('agreed_cfu')
            packaging_specs = request.form.getlist('packaging_spec')
            for i in range(len(product_ids)):
                if not product_ids[i] or not quantities[i]:
                    continue
                db.session.execute(text("""
                    INSERT INTO samples
                        (company_id, sample_no, client_id, contact_id, product_id,
                         quantity, agreed_cfu, packaging_spec, request_date,
                         shipping_address, notes)
                    VALUES (:cid, :sno, :clid, :coid, :pid,
                            :qty, :cfu, :spec, :rdate, :addr, :notes)
                """), {
                    'cid':  company_id, 'sno': sample_no,
                    'clid': client_id,  'coid': contact_id,
                    'pid':  product_ids[i],
                    'qty':  Decimal(quantities[i]),
                    'cfu':  agreed_cfus[i] if agreed_cfus[i] else None,
                    'spec': packaging_specs[i] or '1 kg/bag',
                    'rdate': order_date, 'addr': addr, 'notes': notes,
                })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add sample record: {e}"
        return redirect(url_for('samples'))

    try:
        companies = get_companies()
        clients = db.session.execute(text(
            "SELECT client_id, company_name FROM clients ORDER BY company_name"
        )).mappings().all()
        products = db.session.execute(text("""
            SELECT product_id, chinese_name, NULL AS standard_potency,
                   'billion CFU/g' AS potency_unit, 'Product' AS product_type
            FROM products ORDER BY chinese_name
        """)).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    today = beijing_today().strftime('%Y-%m-%d')
    next_sample_no = generate_sample_no()
    return render_template(
        'new_sample.html',
        clients=clients, products=products, companies=companies,
        default_company_id=get_default_company_id(),
        next_sample_no=next_sample_no, today=today,
    )


@app.route('/edit-sample/<path:sample_no>', methods=['GET', 'POST'])
def edit_sample(sample_no):
    rows = db.session.execute(text(
        "SELECT * FROM samples WHERE sample_no = :sno ORDER BY sample_id"
    ), {'sno': sample_no}).mappings().all()
    if not rows:
        return "Sample record not found", 404
    header = rows[0]
    if request.method == 'POST':
        try:
            company_id = int(request.form.get('company_id') or get_default_company_id())
            new_sno  = request.form.get('sample_no') or sample_no
            client_id  = request.form['client_id']
            contact_id = request.form.get('contact_id') or None
            rdate = request.form['order_date']
            addr  = request.form.get('shipping_address') or ''
            notes = request.form.get('notes') or None
            db.session.execute(text("DELETE FROM samples WHERE sample_no = :sno"),
                               {'sno': sample_no})
            product_ids     = request.form.getlist('product_id')
            quantities      = request.form.getlist('quantity')
            agreed_cfus     = request.form.getlist('agreed_cfu')
            packaging_specs = request.form.getlist('packaging_spec')
            shipping_dates  = request.form.getlist('shipping_date')
            tracking_nos    = request.form.getlist('tracking_no')
            logistics_cos   = request.form.getlist('logistics_company')
            for i in range(len(product_ids)):
                if not product_ids[i] or not quantities[i]:
                    continue
                db.session.execute(text("""
                    INSERT INTO samples
                        (company_id, sample_no, client_id, contact_id, product_id,
                         quantity, agreed_cfu, packaging_spec, request_date,
                         shipping_address, shipping_date, tracking_no, logistics_company, notes)
                    VALUES (:cid, :sno, :clid, :coid, :pid,
                            :qty, :cfu, :spec, :rdate,
                            :addr, :sdate, :tno, :lc, :notes)
                """), {
                    'cid': company_id, 'sno': new_sno,
                    'clid': client_id, 'coid': contact_id,
                    'pid':  product_ids[i],
                    'qty':  Decimal(quantities[i]),
                    'cfu':  agreed_cfus[i] if agreed_cfus[i] else None,
                    'spec': packaging_specs[i] or '1 kg/bag',
                    'rdate': rdate, 'addr': addr,
                    'sdate': shipping_dates[i] if shipping_dates[i] else None,
                    'tno':  tracking_nos[i] if tracking_nos[i] else None,
                    'lc':   logistics_cos[i] if logistics_cos[i] else None,
                    'notes': notes,
                })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update sample record: {e}"
        return redirect(url_for('samples'))

    companies = get_companies()
    clients = db.session.execute(text(
        "SELECT client_id, company_name FROM clients ORDER BY company_name"
    )).mappings().all()
    contacts = db.session.execute(text(
        "SELECT contact_id, contact_name FROM contacts WHERE client_id=:clid ORDER BY contact_name"
    ), {'clid': header['client_id']}).mappings().all()
    products = [dict(row) for row in db.session.execute(text(
        "SELECT product_id, chinese_name, NULL AS standard_potency, 'billion CFU/g' AS potency_unit FROM products ORDER BY chinese_name"
    )).mappings().all()]
    return render_template('edit_sample.html', sample=header, items=rows,
                           companies=companies, clients=clients,
                           contacts=contacts, products=products)


@app.route('/ship-sample/<path:sample_no>', methods=['GET', 'POST'])
def ship_sample(sample_no):
    rows = db.session.execute(text("""
        SELECT
            s.*,
            p.chinese_name,
            cp.short_name AS ledger_short_name,
            cl.company_name,
            co.contact_name
        FROM samples s
        LEFT JOIN products p ON s.product_id = p.product_id
        LEFT JOIN companies cp ON s.company_id = cp.company_id
        JOIN clients cl ON s.client_id = cl.client_id
        LEFT JOIN contacts co ON s.contact_id = co.contact_id
        WHERE s.sample_no = :sample_no
        ORDER BY s.sample_id
    """), {'sample_no': sample_no}).mappings().all()
    if not rows:
        return "Sample record not found", 404

    if request.method == 'POST':
        try:
            sample_ids = request.form.getlist('sample_id')
            shipping_dates = request.form.getlist('shipping_date')
            tracking_nos = request.form.getlist('tracking_no')
            logistics_cos = request.form.getlist('logistics_company')
            coas = request.form.getlist('coa')

            for i, sample_id in enumerate(sample_ids):
                db.session.execute(text("""
                    UPDATE samples
                    SET shipping_date = :shipping_date,
                        tracking_no = :tracking_no,
                        logistics_company = :logistics_company,
                        coa = :coa
                    WHERE sample_id = :sample_id
                      AND sample_no = :sample_no
                """), {
                    'shipping_date': shipping_dates[i] if i < len(shipping_dates) and shipping_dates[i] else None,
                    'tracking_no': tracking_nos[i].strip() if i < len(tracking_nos) and tracking_nos[i].strip() else None,
                    'logistics_company': logistics_cos[i].strip() if i < len(logistics_cos) and logistics_cos[i].strip() else None,
                    'coa': coas[i].strip() if i < len(coas) and coas[i].strip() else None,
                    'sample_id': sample_id,
                    'sample_no': sample_no,
                })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update sample shipping information: {e}"
        return redirect(url_for('samples'))

    return render_template('ship_sample.html', sample=rows[0], items=rows)



@app.route('/invoices')
def invoices():
    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND c.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    try:
        companies = get_companies()
        invoices_sql = text("""
            SELECT
                i.invoice_id,
                i.invoice_date,
                i.invoice_amount,
                i.invoice_no,
                i.remark,
                c.contract_id,
                c.contract_no,
                cp.short_name AS ledger_short_name,
                cl.company_name
            FROM invoices i
            JOIN contracts c ON i.contract_id = c.contract_id
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            WHERE 1=1
              """ + company_filter_sql + """
            ORDER BY i.invoice_date DESC, i.invoice_id DESC
        """)
        invoice_rows = db.session.execute(invoices_sql, params).mappings().all()

        pending_sql = text("""
            SELECT
                c.contract_id,
                c.contract_no,
                c.order_date,
                c.delivery_deadline,
                c.total_amount,
                cp.short_name AS ledger_short_name,
                cl.company_name,
                IFNULL(inv_sum.invoiced_amount, 0) AS invoiced_amount,
                c.total_amount - IFNULL(inv_sum.invoiced_amount, 0) AS outstanding_amount
            FROM contracts c
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            LEFT JOIN (
                SELECT contract_id, SUM(invoice_amount) AS invoiced_amount
                FROM invoices
                WHERE invoice_date IS NOT NULL
                GROUP BY contract_id
            ) inv_sum ON c.contract_id = inv_sum.contract_id
            WHERE c.total_amount > 0
              """ + company_filter_sql + """
              AND IFNULL(inv_sum.invoiced_amount, 0) < c.total_amount
            ORDER BY outstanding_amount DESC, c.order_date DESC, c.contract_id DESC
        """)
        pending_contracts = db.session.execute(pending_sql, params).mappings().all()

        pending_summary_sql = text("""
            SELECT
                COUNT(*) AS pending_contract_count,
                COALESCE(SUM(pending.outstanding_amount), 0) AS outstanding_total_amount
            FROM (
                SELECT
                    c.contract_id,
                    c.total_amount - IFNULL(inv_sum.invoiced_amount, 0) AS outstanding_amount
                FROM contracts c
                LEFT JOIN (
                    SELECT contract_id, SUM(invoice_amount) AS invoiced_amount
                    FROM invoices
                    WHERE invoice_date IS NOT NULL
                    GROUP BY contract_id
                ) inv_sum ON c.contract_id = inv_sum.contract_id
                WHERE c.total_amount > 0
                  """ + company_filter_sql + """
                  AND IFNULL(inv_sum.invoiced_amount, 0) < c.total_amount
            ) pending
        """)
        pending_summary = db.session.execute(pending_summary_sql, params).mappings().first()
    except Exception as e:
        return f"Database query error: {e}"

    return render_template(
        'invoices.html',
        invoices=invoice_rows,
        pending_contracts=pending_contracts,
        pending_summary=pending_summary,
        companies=companies,
        selected_company_id=selected_company_id
    )


@app.route('/new-invoice', methods=['GET', 'POST'])
def new_invoice():
    if request.method == 'POST':
        contract_company_id = None
        try:
            contract_company_id = db.session.execute(text("""
                SELECT company_id FROM contracts WHERE contract_id = :contract_id
            """), {'contract_id': request.form['contract_id']}).scalar()
            sql = text("""
                INSERT INTO invoices (contract_id, company_id, invoice_no, invoice_amount, invoice_date, remark)
                VALUES (:contract_id, :company_id, :invoice_no, :invoice_amount, :invoice_date, :remark)
            """)
            db.session.execute(sql, {
                'contract_id': request.form['contract_id'],
                'company_id': contract_company_id,
                'invoice_no': request.form.get('invoice_no') or None,
                'invoice_amount': request.form['invoice_amount'],
                'invoice_date': request.form.get('invoice_date') or None,
                'remark': request.form.get('remark')
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add invoice record: {e}"
        return redirect(url_for('invoices', company_id=contract_company_id))

    try:
        selected_company_id = get_selected_company_id(default_to_primary=True)
        company_filter_sql = "AND c.company_id = :company_id" if selected_company_id else ""
        params = {'company_id': selected_company_id} if selected_company_id else {}
        contracts_sql = text("""
            SELECT
                c.contract_id,
                c.contract_no,
                cp.short_name AS ledger_short_name,
                cl.company_name,
                c.total_amount,
                c.total_amount - IFNULL(inv_sum.invoiced_amount, 0) AS outstanding_amount
            FROM contracts c
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            LEFT JOIN (
                SELECT contract_id, SUM(invoice_amount) AS invoiced_amount
                FROM invoices
                WHERE invoice_date IS NOT NULL
                GROUP BY contract_id
            ) inv_sum ON c.contract_id = inv_sum.contract_id
            WHERE c.total_amount > 0
              """ + company_filter_sql + """
              AND c.total_amount - IFNULL(inv_sum.invoiced_amount, 0) > 0
            ORDER BY c.order_date DESC
        """)
        contracts = db.session.execute(contracts_sql, params).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"

    today = datetime.date.today().strftime('%Y-%m-%d')
    return render_template(
        'new_invoice.html',
        contracts=contracts,
        today=today,
        selected_company_id=selected_company_id
    )


@app.route('/edit-invoice/<int:invoice_id>', methods=['GET', 'POST'])
def edit_invoice(invoice_id):
    if request.method == 'POST':
        invoice_company_id = None
        try:
            invoice_company_id = db.session.execute(text("""
                SELECT c.company_id
                FROM invoices i
                JOIN contracts c ON i.contract_id = c.contract_id
                WHERE i.invoice_id = :invoice_id
            """), {'invoice_id': invoice_id}).scalar()
            sql = text("""
                UPDATE invoices
                SET invoice_no = :invoice_no,
                    invoice_amount = :invoice_amount,
                    invoice_date = :invoice_date,
                    remark = :remark
                WHERE invoice_id = :invoice_id
            """)
            db.session.execute(sql, {
                'invoice_no': request.form.get('invoice_no') or None,
                'invoice_amount': request.form['invoice_amount'],
                'invoice_date': request.form.get('invoice_date') or None,
                'remark': request.form.get('remark'),
                'invoice_id': invoice_id
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update invoice record: {e}"
        return redirect(url_for('invoices', company_id=invoice_company_id))

    try:
        sql = text("""
            SELECT
                i.*,
                c.contract_no,
                c.company_id,
                cl.company_name
            FROM invoices i
            JOIN contracts c ON i.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            WHERE i.invoice_id = :invoice_id
        """)
        invoice = db.session.execute(sql, {'invoice_id': invoice_id}).mappings().fetchone()
        if not invoice:
            return "Invoice record not found!", 404
    except Exception as e:
        return f"Database query error: {e}"

    today = datetime.date.today().strftime('%Y-%m-%d')
    return render_template('edit_invoice.html', invoice=invoice, today=today)


@app.route('/delete-invoice/<int:invoice_id>', methods=['POST'])
def delete_invoice(invoice_id):
    invoice_company_id = None
    try:
        invoice_company_id = db.session.execute(text("""
            SELECT c.company_id
            FROM invoices i
            JOIN contracts c ON i.contract_id = c.contract_id
            WHERE i.invoice_id = :invoice_id
        """), {'invoice_id': invoice_id}).scalar()
        sql = text("DELETE FROM invoices WHERE invoice_id = :invoice_id")
        db.session.execute(sql, {'invoice_id': invoice_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete invoice record: {e}"
    if invoice_company_id:
        return redirect(url_for('invoices', company_id=invoice_company_id))
    return redirect(url_for('invoices'))


@app.route('/receipts')
def receipts():
    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND c.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    try:
        companies = get_companies()
        receipts_sql = text("""
            SELECT
                r.receipt_id,
                r.payment_date,
                r.payment_amount,
                r.remark,
                c.contract_id,
                c.contract_no,
                cp.short_name AS ledger_short_name,
                cl.company_name
            FROM receipts r
            JOIN contracts c ON r.contract_id = c.contract_id
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            WHERE 1=1
              """ + company_filter_sql + """
            ORDER BY r.payment_date DESC, r.receipt_id DESC
        """)
        receipt_rows = db.session.execute(receipts_sql, params).mappings().all()

        unpaid_sql = text("""
            SELECT
                c.contract_id,
                c.contract_no,
                c.order_date,
                c.delivery_deadline,
                c.total_amount,
                cp.short_name AS ledger_short_name,
                cl.company_name,
                IFNULL(p_sum.paid_amount, 0) AS paid_amount,
                c.total_amount - IFNULL(p_sum.paid_amount, 0) AS outstanding_amount
            FROM contracts c
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            LEFT JOIN (
                SELECT contract_id, SUM(payment_amount) AS paid_amount
                FROM receipts
                WHERE payment_date IS NOT NULL
                GROUP BY contract_id
            ) p_sum ON c.contract_id = p_sum.contract_id
            WHERE c.total_amount > 0
              """ + company_filter_sql + """
              AND IFNULL(p_sum.paid_amount, 0) < c.total_amount
            ORDER BY outstanding_amount DESC, c.order_date DESC, c.contract_id DESC
        """)
        unpaid_contracts = db.session.execute(unpaid_sql, params).mappings().all()

        unpaid_summary_sql = text("""
            SELECT
                COUNT(*) AS unpaid_contract_count,
                COALESCE(SUM(unpaid.outstanding_amount), 0) AS outstanding_total_amount
            FROM (
                SELECT
                    c.contract_id,
                    c.total_amount - IFNULL(p_sum.paid_amount, 0) AS outstanding_amount
                FROM contracts c
                LEFT JOIN (
                    SELECT contract_id, SUM(payment_amount) AS paid_amount
                    FROM receipts
                    WHERE payment_date IS NOT NULL
                    GROUP BY contract_id
                ) p_sum ON c.contract_id = p_sum.contract_id
                WHERE c.total_amount > 0
                  """ + company_filter_sql + """
                  AND IFNULL(p_sum.paid_amount, 0) < c.total_amount
            ) unpaid
        """)
        unpaid_summary = db.session.execute(unpaid_summary_sql, params).mappings().first()
    except Exception as e:
        return f"Database query error: {e}"

    return render_template(
        'receipts.html',
        receipts=receipt_rows,
        unpaid_contracts=unpaid_contracts,
        unpaid_summary=unpaid_summary,
        companies=companies,
        selected_company_id=selected_company_id
    )


@app.route('/new-receipt', methods=['GET', 'POST'])
def new_receipt():
    if request.method == 'POST':
        contract_company_id = None
        try:
            contract_company_id = db.session.execute(text("""
                SELECT company_id FROM contracts WHERE contract_id = :contract_id
            """), {'contract_id': request.form['contract_id']}).scalar()
            sql = text("""
                INSERT INTO receipts (contract_id, company_id, payment_amount, payment_date, remark)
                VALUES (:contract_id, :company_id, :payment_amount, :payment_date, :remark)
            """)
            db.session.execute(sql, {
                'contract_id': request.form['contract_id'],
                'company_id': contract_company_id,
                'payment_amount': request.form['payment_amount'],
                'payment_date': request.form.get('payment_date') or None,
                'remark': request.form.get('remark')
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add receipt record: {e}"
        return redirect(url_for('receipts', company_id=contract_company_id))

    try:
        selected_company_id = get_selected_company_id(default_to_primary=True)
        company_filter_sql = "AND c.company_id = :company_id" if selected_company_id else ""
        params = {'company_id': selected_company_id} if selected_company_id else {}
        contracts_sql = text("""
            SELECT
                c.contract_id,
                c.contract_no,
                cp.short_name AS ledger_short_name,
                cl.company_name,
                c.total_amount,
                c.total_amount - IFNULL(p_sum.paid_amount, 0) AS outstanding_amount
            FROM contracts c
            LEFT JOIN companies cp ON c.company_id = cp.company_id
            JOIN clients cl ON c.client_id = cl.client_id
            LEFT JOIN (
                SELECT contract_id, SUM(payment_amount) AS paid_amount
                FROM receipts
                WHERE payment_date IS NOT NULL
                GROUP BY contract_id
            ) p_sum ON c.contract_id = p_sum.contract_id
            WHERE c.total_amount > 0
              """ + company_filter_sql + """
              AND c.total_amount - IFNULL(p_sum.paid_amount, 0) > 0
            ORDER BY c.order_date DESC
        """)
        contracts = db.session.execute(contracts_sql, params).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"

    today = datetime.date.today().strftime('%Y-%m-%d')
    return render_template(
        'new_receipt.html',
        contracts=contracts,
        today=today,
        selected_company_id=selected_company_id
    )


@app.route('/edit-receipt/<int:receipt_id>', methods=['GET', 'POST'])
def edit_receipt(receipt_id):
    if request.method == 'POST':
        receipt_company_id = None
        try:
            receipt_company_id = db.session.execute(text("""
                SELECT c.company_id
                FROM receipts r
                JOIN contracts c ON r.contract_id = c.contract_id
                WHERE r.receipt_id = :receipt_id
            """), {'receipt_id': receipt_id}).scalar()
            sql = text("""
                UPDATE receipts
                SET payment_amount = :payment_amount,
                    payment_date = :payment_date,
                    remark = :remark
                WHERE receipt_id = :receipt_id
            """)
            db.session.execute(sql, {
                'payment_amount': request.form['payment_amount'],
                'payment_date': request.form.get('payment_date') or None,
                'remark': request.form.get('remark'),
                'receipt_id': receipt_id
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update receipt record: {e}"
        return redirect(url_for('receipts', company_id=receipt_company_id))

    try:
        sql = text("""
            SELECT
                r.*,
                c.contract_no,
                c.company_id,
                cl.company_name
            FROM receipts r
            JOIN contracts c ON r.contract_id = c.contract_id
            JOIN clients cl ON c.client_id = cl.client_id
            WHERE r.receipt_id = :receipt_id
        """)
        receipt = db.session.execute(sql, {'receipt_id': receipt_id}).mappings().fetchone()
        if not receipt:
            return "Receipt record not found!", 404
    except Exception as e:
        return f"Database query error: {e}"

    today = datetime.date.today().strftime('%Y-%m-%d')
    return render_template('edit_receipt.html', receipt=receipt, today=today)


@app.route('/delete-receipt/<int:receipt_id>', methods=['POST'])
def delete_receipt(receipt_id):
    receipt_company_id = None
    try:
        receipt_company_id = db.session.execute(text("""
            SELECT c.company_id
            FROM receipts r
            JOIN contracts c ON r.contract_id = c.contract_id
            WHERE r.receipt_id = :receipt_id
        """), {'receipt_id': receipt_id}).scalar()
        sql = text("DELETE FROM receipts WHERE receipt_id = :receipt_id")
        db.session.execute(sql, {'receipt_id': receipt_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete receipt record: {e}"
    if receipt_company_id:
        return redirect(url_for('receipts', company_id=receipt_company_id))
    return redirect(url_for('receipts'))



# --- Inventory routes ---

@app.route('/inventory')
def inventory():
    try:
        product_rows = db.session.execute(text("""
            SELECT p.product_id, p.chinese_name,
                   COUNT(b.batch_id) AS batch_count,
                   SUM(b.quantity_total) AS total_produced,
                   SUM(CASE WHEN b.status='available' THEN b.quantity_available ELSE 0 END) AS total_available
            FROM products p
            JOIN batches b ON b.product_id = p.product_id AND b.batch_type='produced'
            GROUP BY p.product_id
            ORDER BY p.chinese_name
        """)).mappings().all()
        batches_raw = db.session.execute(text("""
            SELECT b.batch_id, b.product_id, b.batch_no, b.production_date, b.expiry_date,
                   b.quantity_total, b.quantity_available, b.actual_potency,
                   b.potency_unit, b.status, b.notes
            FROM batches b
            WHERE b.batch_type='produced'
            ORDER BY b.production_date DESC, b.batch_id DESC
        """)).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    batches_by_product = {}
    for b in batches_raw:
        batches_by_product.setdefault(b['product_id'], []).append(dict(b))
    return render_template('inventory.html', products=product_rows,
                           batches_by_product=batches_by_product)


def _validate_consumption_rows(raw_batch_ids, consumed_qtys, consumption_notes):
    """Validate raw-material consumption rows: check each row's raw-material batch exists, is
    available, and has sufficient stock. Returns (rows, error) — rows is [(raw_batch_id, qty, note), ...],
    error is a message string on validation failure or None. Read-only, no writes; the caller can
    short-circuit on error before touching the database."""
    rows = []
    for i in range(len(raw_batch_ids)):
        if not raw_batch_ids[i] or not consumed_qtys[i]:
            continue
        rbid = int(raw_batch_ids[i])
        qty = Decimal(consumed_qtys[i])
        raw_batch = db.session.execute(text("""
            SELECT batch_no, quantity_available FROM batches
            WHERE batch_id = :rbid AND batch_type = 'received' AND status = 'available'
        """), {'rbid': rbid}).mappings().fetchone()
        if not raw_batch:
            return None, f"Production failed: raw material batch ID {rbid} does not exist or is inactive."
        if Decimal(str(raw_batch['quantity_available'])) < qty:
            return None, f"Production failed: raw material batch {raw_batch['batch_no']} has {raw_batch['quantity_available']} kg available, not enough to consume {qty} kg."
        note = consumption_notes[i].strip() if i < len(consumption_notes) and consumption_notes[i] else None
        rows.append((rbid, qty, note))
    return rows, None


def _apply_consumption_rows(produced_batch_id, rows):
    """Deduct stock and insert production_consumptions rows for already-validated consumption rows, mirroring new_shipment()'s deduction pattern."""
    for rbid, qty, note in rows:
        db.session.execute(text("""
            UPDATE batches
            SET quantity_available = quantity_available - :qty,
                status = CASE WHEN quantity_available - :qty <= 0 THEN 'depleted' ELSE status END
            WHERE batch_id = :rbid
        """), {'qty': qty, 'rbid': rbid})
        db.session.execute(text("""
            INSERT INTO production_consumptions (produced_batch_id, raw_batch_id, quantity_consumed, notes)
            VALUES (:pbid, :rbid, :qty, :note)
        """), {'pbid': produced_batch_id, 'rbid': rbid, 'qty': qty, 'note': note})


@app.route('/new-batch', methods=['GET', 'POST'])
def new_batch():
    if request.method == 'POST':
        try:
            quantity_total = Decimal(request.form['quantity_total'])
            rows, error = _validate_consumption_rows(
                request.form.getlist('raw_batch_id'),
                request.form.getlist('quantity_consumed'),
                request.form.getlist('consumption_notes'),
            )
            if error:
                return error, 400

            result = db.session.execute(text("""
                INSERT INTO batches (
                    product_id, batch_no, batch_type, production_date, expiry_date,
                    quantity_total, quantity_available,
                    actual_potency, potency_unit, status, notes
                ) VALUES (
                    :product_id, :batch_no, 'produced', :production_date, :expiry_date,
                    :quantity_total, :quantity_total,
                    :actual_potency, :potency_unit, 'available', :notes
                )
            """), {
                'product_id': request.form['product_id'],
                'batch_no': request.form['batch_no'],
                'production_date': request.form.get('production_date') or None,
                'expiry_date': request.form.get('expiry_date') or None,
                'quantity_total': quantity_total,
                'actual_potency': request.form.get('actual_potency') or None,
                'potency_unit': request.form.get('potency_unit') or 'billion CFU/g',
                'notes': request.form.get('notes') or None,
            })
            produced_batch_id = result.lastrowid
            _apply_consumption_rows(produced_batch_id, rows)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add batch: {e}"
        return redirect(url_for('inventory'))
    try:
        products = db.session.execute(text("""
            SELECT product_id, chinese_name,
                   'billion CFU/g' AS potency_unit, 'Product' AS product_type
            FROM products ORDER BY chinese_name
        """)).mappings().all()
        raw_batches = db.session.execute(text("""
            SELECT b.batch_id, b.batch_no, b.component_id, c.component_name, c.component_type,
                   b.quantity_available, b.unit, b.actual_potency, b.potency_unit
            FROM batches b
            JOIN components c ON b.component_id = c.component_id
            WHERE b.batch_type = 'received' AND b.status = 'available' AND b.quantity_available > 0
            ORDER BY c.component_type, c.component_name, b.batch_no
        """)).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('new_batch.html', products=products,
                           potency_units=POTENCY_UNITS,
                           raw_batches=raw_batches,
                           today=beijing_today().strftime('%Y-%m-%d'),
                           suggested_batch_no=generate_batch_no('produced'))


@app.route('/edit-batch/<int:batch_id>', methods=['GET', 'POST'])
def edit_batch(batch_id):
    if request.method == 'POST':
        try:
            old = db.session.execute(text("""
                SELECT quantity_total, quantity_available
                FROM batches WHERE batch_id = :bid
            """), {'bid': batch_id}).mappings().fetchone()
            if not old:
                return "Batch not found!", 404

            # Restore and delete all old raw-material consumption rows first, then re-validate and
            # re-apply the newly submitted rows. production_consumptions has no downstream table
            # referencing it, so unlike items/shipments this doesn't need a per-row diff.
            old_consumptions = db.session.execute(text("""
                SELECT raw_batch_id, quantity_consumed FROM production_consumptions
                WHERE produced_batch_id = :bid
            """), {'bid': batch_id}).mappings().fetchall()
            for row in old_consumptions:
                db.session.execute(text("""
                    UPDATE batches
                    SET quantity_available = quantity_available + :qty, status = 'available'
                    WHERE batch_id = :rbid
                """), {'qty': row['quantity_consumed'], 'rbid': row['raw_batch_id']})
            db.session.execute(text(
                "DELETE FROM production_consumptions WHERE produced_batch_id = :bid"
            ), {'bid': batch_id})

            rows, error = _validate_consumption_rows(
                request.form.getlist('raw_batch_id'),
                request.form.getlist('quantity_consumed'),
                request.form.getlist('consumption_notes'),
            )
            if error:
                # The restore already wrote to the current transaction, so we must roll back explicitly rather than just returning.
                db.session.rollback()
                return error, 400
            _apply_consumption_rows(batch_id, rows)

            new_total = Decimal(request.form['quantity_total'])
            qty_diff = new_total - Decimal(str(old['quantity_total']))
            new_available = max(Decimal('0'), Decimal(str(old['quantity_available'])) + qty_diff)
            db.session.execute(text("""
                UPDATE batches
                SET product_id = :product_id,
                    batch_no = :batch_no,
                    production_date = :production_date,
                    expiry_date = :expiry_date,
                    quantity_total = :quantity_total,
                    quantity_available = :quantity_available,
                    actual_potency = :actual_potency,
                    potency_unit = :potency_unit,
                    status = :status,
                    notes = :notes
                WHERE batch_id = :batch_id
            """), {
                'product_id': request.form['product_id'],
                'batch_no': request.form['batch_no'],
                'production_date': request.form.get('production_date') or None,
                'expiry_date': request.form.get('expiry_date') or None,
                'quantity_total': new_total,
                'quantity_available': new_available,
                'actual_potency': request.form.get('actual_potency') or None,
                'potency_unit': request.form.get('potency_unit') or 'billion CFU/g',
                'status': request.form.get('status') or 'available',
                'notes': request.form.get('notes') or None,
                'batch_id': batch_id,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update batch: {e}"
        return redirect(url_for('inventory'))
    try:
        batch = db.session.execute(text("""
            SELECT b.*, p.chinese_name
            FROM batches b
            JOIN products p ON b.product_id = p.product_id
            WHERE b.batch_id = :bid
        """), {'bid': batch_id}).mappings().fetchone()
        if not batch:
            return "Batch not found!", 404
        products = db.session.execute(text(
            "SELECT product_id, chinese_name, 'billion CFU/g' AS potency_unit FROM products ORDER BY chinese_name"
        )).mappings().all()
        consumptions = db.session.execute(text("""
            SELECT pc.pc_id, pc.raw_batch_id, pc.quantity_consumed, pc.notes,
                   b.batch_no AS raw_batch_no, b.unit AS raw_batch_unit, c.component_name
            FROM production_consumptions pc
            JOIN batches b ON pc.raw_batch_id = b.batch_id
            JOIN components c ON b.component_id = c.component_id
            WHERE pc.produced_batch_id = :bid
        """), {'bid': batch_id}).mappings().all()
        # Selectable raw-material batches: currently available ones + any already linked to this
        # record (even if now depleted/inactive), otherwise the pre-selected batch would
        # disappear from the dropdown on the edit page.
        raw_batches = db.session.execute(text("""
            SELECT b.batch_id, b.batch_no, b.component_id, c.component_name, c.component_type,
                   b.quantity_available, b.unit, b.actual_potency, b.potency_unit
            FROM batches b
            JOIN components c ON b.component_id = c.component_id
            WHERE b.batch_type = 'received'
              AND (
                  (b.status = 'available' AND b.quantity_available > 0)
                  OR b.batch_id IN (SELECT raw_batch_id FROM production_consumptions WHERE produced_batch_id = :bid)
              )
            ORDER BY c.component_type, c.component_name, b.batch_no
        """), {'bid': batch_id}).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('edit_batch.html', batch=batch, products=products,
                           potency_units=POTENCY_UNITS,
                           consumptions=consumptions, raw_batches=raw_batches)


@app.route('/delete-batch/<int:batch_id>', methods=['POST'])
def delete_batch(batch_id):
    try:
        count = db.session.execute(text(
            "SELECT COUNT(*) FROM shipments WHERE batch_id = :bid"
        ), {'bid': batch_id}).scalar()
        if count:
            return f"Cannot delete: this batch already has {count} linked shipment record(s).", 400

        consumptions = db.session.execute(text("""
            SELECT raw_batch_id, quantity_consumed FROM production_consumptions
            WHERE produced_batch_id = :bid
        """), {'bid': batch_id}).mappings().fetchall()
        for row in consumptions:
            db.session.execute(text("""
                UPDATE batches
                SET quantity_available = quantity_available + :qty, status = 'available'
                WHERE batch_id = :rbid
            """), {'qty': row['quantity_consumed'], 'rbid': row['raw_batch_id']})
        db.session.execute(text(
            "DELETE FROM production_consumptions WHERE produced_batch_id = :bid"
        ), {'bid': batch_id})

        db.session.execute(text(
            "DELETE FROM batches WHERE batch_id = :bid"
        ), {'bid': batch_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete batch: {e}"
    return redirect(url_for('inventory'))


@app.route('/api/products/<int:product_id>/batches')
def get_product_batches(product_id):
    try:
        batches = db.session.execute(text("""
            SELECT batch_id, batch_no, batch_type, production_date, received_date, expiry_date,
                   quantity_available, actual_potency, potency_unit, status
            FROM batches
            WHERE product_id = :product_id
              AND status = 'available'
              AND quantity_available > 0
            ORDER BY COALESCE(production_date, received_date) ASC, batch_id ASC
        """), {'product_id': product_id}).mappings().all()
        result = []
        import decimal as _dec
        for b in batches:
            d = dict(b)
            for k, v in list(d.items()):
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif isinstance(v, _dec.Decimal):
                    d[k] = float(v)
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ───────────────────────────────────────────────
# Raw Materials
# ───────────────────────────────────────────────

@app.route('/raw-materials')
def raw_materials():
    materials = db.session.execute(text("""
        SELECT c.component_id AS material_id, c.component_name AS material_name,
               c.component_type AS material_type,
               c.default_unit,
               COUNT(b.batch_id) AS batch_count,
               SUM(CASE WHEN b.status='available' THEN b.quantity_available ELSE 0 END) AS total_stock,
               MAX(b.unit) AS unit
        FROM components c
        LEFT JOIN batches b ON b.component_id = c.component_id AND b.batch_type='received'
        WHERE c.is_active = 1
        GROUP BY c.component_id
        ORDER BY c.component_type, c.component_name
    """)).mappings().all()
    batches_raw = db.session.execute(text("""
        SELECT b.batch_id, b.component_id AS product_id, b.batch_no, b.received_date, b.expiry_date,
               b.quantity_total, b.quantity_available, b.unit, b.actual_potency, b.potency_unit,
               s.supplier_name AS supplier, b.supplier_batch_no, b.unit_cost, b.status, b.notes
        FROM batches b
        LEFT JOIN suppliers s ON b.supplier_id = s.supplier_id
        WHERE b.batch_type='received'
        ORDER BY b.received_date DESC, b.batch_id DESC
    """)).mappings().all()
    batches_by_product = {}
    for b in batches_raw:
        batches_by_product.setdefault(b['product_id'], []).append(dict(b))
    return render_template('raw_materials.html', materials=materials,
                           batches_by_product=batches_by_product)


@app.route('/new-raw-material', methods=['GET', 'POST'])
def new_raw_material():
    if request.method == 'POST':
        try:
            db.session.execute(text("""
                INSERT INTO components
                    (component_name, component_type, default_unit, notes)
                VALUES
                    (:name, :ctype, :default_unit, :notes)
            """), {
                'name':     request.form['material_name'].strip(),
                'ctype':    request.form.get('material_type', 'Other'),
                'default_unit': request.form.get('default_unit') or 'billion CFU/g',
                'notes':    request.form.get('notes', '').strip() or None,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add raw material: {e}"
        return redirect(url_for('raw_materials'))
    return render_template('new_raw_material.html')


@app.route('/edit-raw-material/<int:material_id>', methods=['GET', 'POST'])
def edit_raw_material(material_id):
    mat = db.session.execute(text("""
        SELECT component_id AS material_id, component_name AS material_name,
               component_type AS material_type,
               default_unit
        FROM components WHERE component_id = :id AND is_active = 1
    """), {'id': material_id}).mappings().first()
    if not mat:
        return "Raw material not found", 404
    if request.method == 'POST':
        try:
            db.session.execute(text("""
                UPDATE components SET
                    component_name=:name, component_type=:ctype,
                    default_unit=:default_unit
                WHERE component_id=:id
            """), {
                'id':       material_id,
                'name':     request.form['material_name'].strip(),
                'ctype':    request.form.get('material_type', 'Other'),
                'default_unit': request.form.get('default_unit') or 'billion CFU/g',
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update raw material: {e}"
        return redirect(url_for('raw_materials'))
    return render_template('edit_raw_material.html', mat=mat)


@app.route('/delete-raw-material/<int:material_id>', methods=['POST'])
def delete_raw_material(material_id):
    try:
        db.session.execute(text(
            "UPDATE components SET is_active=0 WHERE component_id=:id"
        ), {'id': material_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete raw material: {e}"
    return redirect(url_for('raw_materials'))


# ───────────────────────────────────────────────
# Procurement module: suppliers / purchase orders / supplier invoices / purchase payments
# ───────────────────────────────────────────────

@app.route('/suppliers')
def suppliers():
    suppliers_list = db.session.execute(text("""
        SELECT * FROM suppliers WHERE is_active = 1 ORDER BY supplier_name
    """)).mappings().all()
    return render_template('suppliers.html', suppliers=suppliers_list)


@app.route('/add-supplier', methods=['POST'])
def add_supplier():
    try:
        db.session.execute(text("""
            INSERT INTO suppliers
                (supplier_name, supplier_address, tax_id, contact_name, contact_phone, bank_info, notes)
            VALUES
                (:name, :address, :tax_id, :contact_name, :contact_phone, :bank_info, :notes)
        """), {
            'name':          request.form['supplier_name'].strip(),
            'address':       request.form.get('supplier_address', '').strip() or None,
            'tax_id':        request.form.get('tax_id', '').strip() or None,
            'contact_name':  request.form.get('contact_name', '').strip() or None,
            'contact_phone': request.form.get('contact_phone', '').strip() or None,
            'bank_info':     request.form.get('bank_info', '').strip() or None,
            'notes':         request.form.get('notes', '').strip() or None,
        })
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to add supplier: {e}"
    return redirect(url_for('suppliers'))


@app.route('/edit-supplier/<int:supplier_id>', methods=['GET', 'POST'])
def edit_supplier(supplier_id):
    supplier = db.session.execute(text(
        "SELECT * FROM suppliers WHERE supplier_id = :id"
    ), {'id': supplier_id}).mappings().first()
    if not supplier:
        return "Supplier not found", 404
    if request.method == 'POST':
        try:
            db.session.execute(text("""
                UPDATE suppliers SET
                    supplier_name=:name, supplier_address=:address, tax_id=:tax_id,
                    contact_name=:contact_name, contact_phone=:contact_phone,
                    bank_info=:bank_info, notes=:notes
                WHERE supplier_id=:id
            """), {
                'id':            supplier_id,
                'name':          request.form['supplier_name'].strip(),
                'address':       request.form.get('supplier_address', '').strip() or None,
                'tax_id':        request.form.get('tax_id', '').strip() or None,
                'contact_name':  request.form.get('contact_name', '').strip() or None,
                'contact_phone': request.form.get('contact_phone', '').strip() or None,
                'bank_info':     request.form.get('bank_info', '').strip() or None,
                'notes':         request.form.get('notes', '').strip() or None,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update supplier: {e}"
        return redirect(url_for('suppliers'))
    return render_template('edit_supplier.html', supplier=supplier)


@app.route('/delete-supplier/<int:supplier_id>', methods=['POST'])
def delete_supplier(supplier_id):
    try:
        db.session.execute(text(
            "UPDATE suppliers SET is_active=0 WHERE supplier_id=:id"
        ), {'id': supplier_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete supplier: {e}"
    return redirect(url_for('suppliers'))


@app.route('/purchase-orders')
def purchase_orders():
    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND po.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    try:
        companies = get_companies()
        pos = db.session.execute(text("""
            SELECT
                po.po_id, po.po_no, po.order_date, po.expected_date, po.total_amount,
                po.payment_method, po.notes,
                cp.short_name AS ledger_short_name,
                s.supplier_name,
                IFNULL(ord_sum.ordered_qty, 0) AS ordered_qty,
                IFNULL(recv_sum.received_qty, 0) AS received_qty,
                IFNULL(inv_sum.invoiced_amount, 0) AS invoiced_amount,
                IFNULL(pay_sum.paid_amount, 0) AS paid_amount,
                mat_sum.material_summary
            FROM purchase_orders po
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            LEFT JOIN (
                SELECT po_id, SUM(quantity) AS ordered_qty
                FROM purchase_order_items GROUP BY po_id
            ) ord_sum ON po.po_id = ord_sum.po_id
            LEFT JOIN (
                SELECT poi.po_id,
                       GROUP_CONCAT(CONCAT(c.component_name, ' ', poi.quantity, poi.unit,
                                           IF(poi.spec_potency IS NOT NULL,
                                              CONCAT(' (', poi.spec_potency, IFNULL(poi.spec_potency_unit, ''), ')'),
                                              ''))
                                    ORDER BY poi.poi_id SEPARATOR ', ') AS material_summary
                FROM purchase_order_items poi
                JOIN components c ON poi.component_id = c.component_id
                GROUP BY poi.po_id
            ) mat_sum ON po.po_id = mat_sum.po_id
            LEFT JOIN (
                SELECT poi.po_id, SUM(b.quantity_total) AS received_qty
                FROM batches b JOIN purchase_order_items poi ON b.poi_id = poi.poi_id
                GROUP BY poi.po_id
            ) recv_sum ON po.po_id = recv_sum.po_id
            LEFT JOIN (
                SELECT po_id, SUM(invoice_amount) AS invoiced_amount
                FROM purchase_invoices WHERE invoice_date IS NOT NULL GROUP BY po_id
            ) inv_sum ON po.po_id = inv_sum.po_id
            LEFT JOIN (
                SELECT po_id, SUM(payment_amount) AS paid_amount
                FROM purchase_payments WHERE payment_date IS NOT NULL GROUP BY po_id
            ) pay_sum ON po.po_id = pay_sum.po_id
            WHERE 1=1 """ + company_filter_sql + """
            ORDER BY po.order_date DESC, po.po_id DESC
        """), params).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('purchase_orders.html', purchase_orders=pos,
                           companies=companies, selected_company_id=selected_company_id)


@app.route('/new-purchase-order', methods=['GET', 'POST'])
def new_purchase_order():
    if request.method == 'POST':
        selected_company_id = request.form.get('company_id') or get_default_company_id()
        try:
            po_sql = text("""
                INSERT INTO purchase_orders
                    (company_id, supplier_id, po_no, order_date, expected_date, payment_method, notes, total_amount)
                VALUES
                    (:company_id, :supplier_id, :po_no, :order_date, :expected_date, :payment_method, :notes, 0)
            """)
            result = db.session.execute(po_sql, {
                'company_id':     selected_company_id,
                'supplier_id':    request.form['supplier_id'],
                'po_no':          request.form['po_no'].strip(),
                'order_date':     request.form['order_date'],
                'expected_date':  request.form.get('expected_date') or None,
                'payment_method': request.form.get('payment_method'),
                'notes':          request.form.get('notes'),
            })
            po_id = result.lastrowid
            total_amount = Decimal(0)

            component_ids = request.form.getlist('component_id')
            quantities    = request.form.getlist('quantity')
            unit_prices   = request.form.getlist('unit_price')
            units         = request.form.getlist('unit')
            spec_potencies = request.form.getlist('spec_potency')
            spec_potency_units = request.form.getlist('spec_potency_unit')
            item_notes    = request.form.getlist('item_notes')

            item_sql = text("""
                INSERT INTO purchase_order_items
                    (po_id, component_id, quantity, unit_price, unit, spec_potency, spec_potency_unit, notes)
                VALUES
                    (:po_id, :component_id, :quantity, :unit_price, :unit, :spec_potency, :spec_potency_unit, :notes)
            """)
            for i in range(len(component_ids)):
                if not component_ids[i] or not quantities[i] or not unit_prices[i]:
                    continue
                quantity = Decimal(quantities[i])
                unit_price = Decimal(unit_prices[i])
                total_amount += quantity * unit_price
                db.session.execute(item_sql, {
                    'po_id': po_id,
                    'component_id': component_ids[i],
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'unit': units[i] or 'kg',
                    'spec_potency': Decimal(spec_potencies[i]) if i < len(spec_potencies) and spec_potencies[i] else None,
                    'spec_potency_unit': spec_potency_units[i].strip() if i < len(spec_potency_units) and spec_potency_units[i] else None,
                    'notes': item_notes[i].strip() if i < len(item_notes) and item_notes[i] else None,
                })

            db.session.execute(text(
                "UPDATE purchase_orders SET total_amount = :total_amount WHERE po_id = :po_id"
            ), {'total_amount': total_amount, 'po_id': po_id})
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add purchase order: {e}"
        return redirect(url_for('purchase_orders', company_id=selected_company_id))

    companies = get_companies()
    suppliers_list = db.session.execute(text(
        "SELECT supplier_id, supplier_name FROM suppliers WHERE is_active=1 ORDER BY supplier_name"
    )).mappings().all()
    components_list = db.session.execute(text("""
        SELECT component_id, component_name, component_type, default_unit
        FROM components WHERE is_active=1 ORDER BY component_type, component_name
    """)).mappings().all()
    return render_template(
        'new_purchase_order.html',
        companies=companies,
        suppliers=suppliers_list,
        components=components_list,
        default_company_id=get_default_company_id(),
        today=beijing_today().strftime('%Y-%m-%d'),
        suggested_po_no=generate_po_no(),
    )


@app.route('/edit-purchase-order/<int:po_id>', methods=['GET', 'POST'])
def edit_purchase_order(po_id):
    if request.method == 'POST':
        selected_company_id = request.form.get('company_id') or get_default_company_id()
        try:
            db.session.execute(text("""
                UPDATE purchase_orders SET
                    company_id=:company_id, supplier_id=:supplier_id, po_no=:po_no,
                    order_date=:order_date, expected_date=:expected_date,
                    payment_method=:payment_method, notes=:notes
                WHERE po_id=:po_id
            """), {
                'po_id':          po_id,
                'company_id':     selected_company_id,
                'supplier_id':    request.form['supplier_id'],
                'po_no':          request.form['po_no'].strip(),
                'order_date':     request.form['order_date'],
                'expected_date':  request.form.get('expected_date') or None,
                'payment_method': request.form.get('payment_method'),
                'notes':          request.form.get('notes'),
            })

            total_amount = Decimal(0)
            poi_ids       = request.form.getlist('poi_id')
            component_ids = request.form.getlist('component_id')
            quantities    = request.form.getlist('quantity')
            unit_prices   = request.form.getlist('unit_price')
            units         = request.form.getlist('unit')
            spec_potencies = request.form.getlist('spec_potency')
            spec_potency_units = request.form.getlist('spec_potency_unit')
            item_notes    = request.form.getlist('item_notes')

            existing_poi_ids = {row[0] for row in db.session.execute(text(
                "SELECT poi_id FROM purchase_order_items WHERE po_id = :po_id"
            ), {'po_id': po_id}).fetchall()}
            submitted_poi_ids = {int(pid) for pid in poi_ids if pid}

            ids_to_delete = existing_poi_ids - submitted_poi_ids
            if ids_to_delete:
                for poi_id_to_delete in ids_to_delete:
                    batch_count = db.session.execute(text(
                        "SELECT COUNT(*) FROM batches WHERE poi_id = :poi_id"
                    ), {'poi_id': poi_id_to_delete}).scalar()
                    if batch_count > 0:
                        db.session.rollback()
                        return f"Failed to update purchase order: cannot delete order item ID {poi_id_to_delete} because it already has a linked received batch."
                db.session.execute(text(
                    "DELETE FROM purchase_order_items WHERE poi_id IN :ids"
                ), {'ids': tuple(ids_to_delete)})

            update_item_sql = text("""
                UPDATE purchase_order_items
                SET component_id=:component_id, quantity=:quantity, unit_price=:unit_price,
                    unit=:unit, spec_potency=:spec_potency, spec_potency_unit=:spec_potency_unit, notes=:notes
                WHERE poi_id=:poi_id
            """)
            insert_item_sql = text("""
                INSERT INTO purchase_order_items
                    (po_id, component_id, quantity, unit_price, unit, spec_potency, spec_potency_unit, notes)
                VALUES
                    (:po_id, :component_id, :quantity, :unit_price, :unit, :spec_potency, :spec_potency_unit, :notes)
            """)
            for i in range(len(component_ids)):
                if not component_ids[i] or not quantities[i] or not unit_prices[i]:
                    continue
                quantity = Decimal(quantities[i])
                unit_price = Decimal(unit_prices[i])
                total_amount += quantity * unit_price
                params = {
                    'component_id': component_ids[i],
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'unit': units[i] or 'kg',
                    'spec_potency': Decimal(spec_potencies[i]) if i < len(spec_potencies) and spec_potencies[i] else None,
                    'spec_potency_unit': spec_potency_units[i].strip() if i < len(spec_potency_units) and spec_potency_units[i] else None,
                    'notes': item_notes[i].strip() if i < len(item_notes) and item_notes[i] else None,
                }
                poi_id_val = poi_ids[i] if i < len(poi_ids) else ''
                if poi_id_val and int(poi_id_val) in existing_poi_ids:
                    params['poi_id'] = int(poi_id_val)
                    db.session.execute(update_item_sql, params)
                else:
                    params['po_id'] = po_id
                    db.session.execute(insert_item_sql, params)

            db.session.execute(text(
                "UPDATE purchase_orders SET total_amount = :total_amount WHERE po_id = :po_id"
            ), {'total_amount': total_amount, 'po_id': po_id})
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update purchase order: {e}"
        return redirect(url_for('purchase_orders', company_id=selected_company_id))

    po = db.session.execute(text(
        "SELECT * FROM purchase_orders WHERE po_id = :po_id"
    ), {'po_id': po_id}).mappings().first()
    if not po:
        return "Purchase order not found", 404
    items = db.session.execute(text("""
        SELECT poi.*, c.component_name, c.component_type
        FROM purchase_order_items poi
        JOIN components c ON poi.component_id = c.component_id
        WHERE poi.po_id = :po_id ORDER BY poi.poi_id
    """), {'po_id': po_id}).mappings().all()
    companies = get_companies()
    suppliers_list = db.session.execute(text(
        "SELECT supplier_id, supplier_name FROM suppliers WHERE is_active=1 ORDER BY supplier_name"
    )).mappings().all()
    components_list = db.session.execute(text("""
        SELECT component_id, component_name, component_type, default_unit
        FROM components WHERE is_active=1 ORDER BY component_type, component_name
    """)).mappings().all()
    return render_template(
        'edit_purchase_order.html',
        po=po, items=items, companies=companies,
        suppliers=suppliers_list, components=components_list,
    )


@app.route('/delete-purchase-order/<int:po_id>', methods=['POST'])
def delete_purchase_order(po_id):
    try:
        batch_count = db.session.execute(text("""
            SELECT COUNT(*) FROM batches b
            JOIN purchase_order_items poi ON b.poi_id = poi.poi_id
            WHERE poi.po_id = :po_id
        """), {'po_id': po_id}).scalar()
        if batch_count > 0:
            return f"Cannot delete: this purchase order already has {batch_count} linked received batch(es).", 400
        inv_count = db.session.execute(text(
            "SELECT COUNT(*) FROM purchase_invoices WHERE po_id = :po_id"
        ), {'po_id': po_id}).scalar()
        pay_count = db.session.execute(text(
            "SELECT COUNT(*) FROM purchase_payments WHERE po_id = :po_id"
        ), {'po_id': po_id}).scalar()
        if inv_count > 0 or pay_count > 0:
            return f"Cannot delete: this purchase order already has invoice or payment records.", 400

        db.session.execute(text(
            "DELETE FROM purchase_orders WHERE po_id = :po_id"
        ), {'po_id': po_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete purchase order: {e}"
    return redirect(url_for('purchase_orders'))


def _jsonify_rows(rows):
    import decimal as _dec
    result = []
    for row in rows:
        d = dict(row)
        for k, v in list(d.items()):
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
            elif isinstance(v, _dec.Decimal):
                d[k] = float(v)
        result.append(d)
    return jsonify(result)


@app.route('/api/purchase-orders/<int:po_id>/items')
def get_po_items_api(po_id):
    items = db.session.execute(text("""
        SELECT
            poi.poi_id, poi.component_id, c.component_name, poi.quantity, poi.unit_price, poi.unit,
            IFNULL(recv.received_qty, 0) AS received_qty,
            poi.quantity - IFNULL(recv.received_qty, 0) AS remaining_qty
        FROM purchase_order_items poi
        JOIN components c ON poi.component_id = c.component_id
        LEFT JOIN (
            SELECT poi_id, SUM(quantity_total) AS received_qty
            FROM batches GROUP BY poi_id
        ) recv ON poi.poi_id = recv.poi_id
        WHERE poi.po_id = :po_id
    """), {'po_id': po_id}).mappings().all()
    return _jsonify_rows(items)


@app.route('/api/purchase-order-items/open')
def get_open_po_items_api():
    component_id = request.args.get('component_id', type=int)
    supplier_id = request.args.get('supplier_id', type=int)
    if not component_id or not supplier_id:
        return jsonify([])
    items = db.session.execute(text("""
        SELECT
            poi.poi_id, poi.quantity, poi.unit_price, poi.unit,
            po.po_no,
            IFNULL(recv.received_qty, 0) AS received_qty,
            poi.quantity - IFNULL(recv.received_qty, 0) AS remaining_qty
        FROM purchase_order_items poi
        JOIN purchase_orders po ON poi.po_id = po.po_id
        LEFT JOIN (
            SELECT poi_id, SUM(quantity_total) AS received_qty
            FROM batches GROUP BY poi_id
        ) recv ON poi.poi_id = recv.poi_id
        WHERE poi.component_id = :component_id AND po.supplier_id = :supplier_id
        HAVING remaining_qty > 0
        ORDER BY po.order_date ASC
    """), {'component_id': component_id, 'supplier_id': supplier_id}).mappings().all()
    return _jsonify_rows(items)


@app.route('/purchase-invoices')
def purchase_invoices():
    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND po.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    try:
        companies = get_companies()
        invoice_rows = db.session.execute(text("""
            SELECT
                pi.purchase_invoice_id, pi.invoice_date, pi.invoice_amount, pi.invoice_no, pi.remark,
                po.po_id, po.po_no, cp.short_name AS ledger_short_name, s.supplier_name
            FROM purchase_invoices pi
            JOIN purchase_orders po ON pi.po_id = po.po_id
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            WHERE 1=1 """ + company_filter_sql + """
            ORDER BY pi.invoice_date DESC, pi.purchase_invoice_id DESC
        """), params).mappings().all()

        pending_pos = db.session.execute(text("""
            SELECT
                po.po_id, po.po_no, po.order_date, po.total_amount,
                cp.short_name AS ledger_short_name, s.supplier_name,
                IFNULL(inv_sum.invoiced_amount, 0) AS invoiced_amount,
                po.total_amount - IFNULL(inv_sum.invoiced_amount, 0) AS outstanding_amount
            FROM purchase_orders po
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            LEFT JOIN (
                SELECT po_id, SUM(invoice_amount) AS invoiced_amount
                FROM purchase_invoices WHERE invoice_date IS NOT NULL GROUP BY po_id
            ) inv_sum ON po.po_id = inv_sum.po_id
            WHERE po.total_amount > 0 """ + company_filter_sql + """
              AND IFNULL(inv_sum.invoiced_amount, 0) < po.total_amount
            ORDER BY outstanding_amount DESC, po.order_date DESC, po.po_id DESC
        """), params).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('purchase_invoices.html', invoices=invoice_rows,
                           pending_pos=pending_pos, companies=companies,
                           selected_company_id=selected_company_id)


@app.route('/new-purchase-invoice', methods=['GET', 'POST'])
def new_purchase_invoice():
    if request.method == 'POST':
        po_company_id = None
        try:
            po_company_id = db.session.execute(text(
                "SELECT company_id FROM purchase_orders WHERE po_id = :po_id"
            ), {'po_id': request.form['po_id']}).scalar()
            db.session.execute(text("""
                INSERT INTO purchase_invoices (po_id, company_id, invoice_no, invoice_amount, invoice_date, remark)
                VALUES (:po_id, :company_id, :invoice_no, :invoice_amount, :invoice_date, :remark)
            """), {
                'po_id':          request.form['po_id'],
                'company_id':     po_company_id,
                'invoice_no':     request.form.get('invoice_no') or None,
                'invoice_amount': request.form['invoice_amount'],
                'invoice_date':   request.form.get('invoice_date') or None,
                'remark':         request.form.get('remark'),
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add supplier invoice: {e}"
        return redirect(url_for('purchase_invoices', company_id=po_company_id))

    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND po.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    pos = db.session.execute(text("""
        SELECT
            po.po_id, po.po_no, s.supplier_name, po.total_amount,
            po.total_amount - IFNULL(inv_sum.invoiced_amount, 0) AS outstanding_amount
        FROM purchase_orders po
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        LEFT JOIN (
            SELECT po_id, SUM(invoice_amount) AS invoiced_amount
            FROM purchase_invoices WHERE invoice_date IS NOT NULL GROUP BY po_id
        ) inv_sum ON po.po_id = inv_sum.po_id
        WHERE po.total_amount > 0 """ + company_filter_sql + """
          AND po.total_amount - IFNULL(inv_sum.invoiced_amount, 0) > 0
        ORDER BY po.order_date DESC
    """), params).mappings().all()
    today = beijing_today().strftime('%Y-%m-%d')
    return render_template('new_purchase_invoice.html', purchase_orders=pos, today=today,
                           selected_company_id=selected_company_id)


@app.route('/edit-purchase-invoice/<int:purchase_invoice_id>', methods=['GET', 'POST'])
def edit_purchase_invoice(purchase_invoice_id):
    if request.method == 'POST':
        pi_company_id = None
        try:
            pi_company_id = db.session.execute(text("""
                SELECT po.company_id FROM purchase_invoices pi
                JOIN purchase_orders po ON pi.po_id = po.po_id
                WHERE pi.purchase_invoice_id = :id
            """), {'id': purchase_invoice_id}).scalar()
            db.session.execute(text("""
                UPDATE purchase_invoices
                SET invoice_no=:invoice_no, invoice_amount=:invoice_amount,
                    invoice_date=:invoice_date, remark=:remark
                WHERE purchase_invoice_id=:id
            """), {
                'invoice_no':     request.form.get('invoice_no') or None,
                'invoice_amount': request.form['invoice_amount'],
                'invoice_date':   request.form.get('invoice_date') or None,
                'remark':         request.form.get('remark'),
                'id':             purchase_invoice_id,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update supplier invoice: {e}"
        return redirect(url_for('purchase_invoices', company_id=pi_company_id))

    invoice = db.session.execute(text("""
        SELECT pi.*, po.po_no, po.company_id, s.supplier_name
        FROM purchase_invoices pi
        JOIN purchase_orders po ON pi.po_id = po.po_id
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        WHERE pi.purchase_invoice_id = :id
    """), {'id': purchase_invoice_id}).mappings().fetchone()
    if not invoice:
        return "Supplier invoice record not found!", 404
    today = beijing_today().strftime('%Y-%m-%d')
    return render_template('edit_purchase_invoice.html', invoice=invoice, today=today)


@app.route('/delete-purchase-invoice/<int:purchase_invoice_id>', methods=['POST'])
def delete_purchase_invoice(purchase_invoice_id):
    pi_company_id = None
    try:
        pi_company_id = db.session.execute(text("""
            SELECT po.company_id FROM purchase_invoices pi
            JOIN purchase_orders po ON pi.po_id = po.po_id
            WHERE pi.purchase_invoice_id = :id
        """), {'id': purchase_invoice_id}).scalar()
        db.session.execute(text(
            "DELETE FROM purchase_invoices WHERE purchase_invoice_id = :id"
        ), {'id': purchase_invoice_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete supplier invoice: {e}"
    if pi_company_id:
        return redirect(url_for('purchase_invoices', company_id=pi_company_id))
    return redirect(url_for('purchase_invoices'))


@app.route('/purchase-payments')
def purchase_payments():
    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND po.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    try:
        companies = get_companies()
        payment_rows = db.session.execute(text("""
            SELECT
                pp.payment_id, pp.payment_date, pp.payment_amount, pp.remark,
                po.po_id, po.po_no, cp.short_name AS ledger_short_name, s.supplier_name
            FROM purchase_payments pp
            JOIN purchase_orders po ON pp.po_id = po.po_id
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            WHERE 1=1 """ + company_filter_sql + """
            ORDER BY pp.payment_date DESC, pp.payment_id DESC
        """), params).mappings().all()

        unpaid_pos = db.session.execute(text("""
            SELECT
                po.po_id, po.po_no, po.order_date, po.total_amount,
                cp.short_name AS ledger_short_name, s.supplier_name,
                IFNULL(p_sum.paid_amount, 0) AS paid_amount,
                po.total_amount - IFNULL(p_sum.paid_amount, 0) AS outstanding_amount
            FROM purchase_orders po
            LEFT JOIN companies cp ON po.company_id = cp.company_id
            JOIN suppliers s ON po.supplier_id = s.supplier_id
            LEFT JOIN (
                SELECT po_id, SUM(payment_amount) AS paid_amount
                FROM purchase_payments WHERE payment_date IS NOT NULL GROUP BY po_id
            ) p_sum ON po.po_id = p_sum.po_id
            WHERE po.total_amount > 0 """ + company_filter_sql + """
              AND IFNULL(p_sum.paid_amount, 0) < po.total_amount
            ORDER BY outstanding_amount DESC, po.order_date DESC, po.po_id DESC
        """), params).mappings().all()
    except Exception as e:
        return f"Database query error: {e}"
    return render_template('purchase_payments.html', payments=payment_rows,
                           unpaid_pos=unpaid_pos, companies=companies,
                           selected_company_id=selected_company_id)


@app.route('/new-purchase-payment', methods=['GET', 'POST'])
def new_purchase_payment():
    if request.method == 'POST':
        po_company_id = None
        try:
            po_company_id = db.session.execute(text(
                "SELECT company_id FROM purchase_orders WHERE po_id = :po_id"
            ), {'po_id': request.form['po_id']}).scalar()
            db.session.execute(text("""
                INSERT INTO purchase_payments (po_id, company_id, payment_amount, payment_date, remark)
                VALUES (:po_id, :company_id, :payment_amount, :payment_date, :remark)
            """), {
                'po_id':          request.form['po_id'],
                'company_id':     po_company_id,
                'payment_amount': request.form['payment_amount'],
                'payment_date':   request.form.get('payment_date') or None,
                'remark':         request.form.get('remark'),
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add purchase payment record: {e}"
        return redirect(url_for('purchase_payments', company_id=po_company_id))

    selected_company_id = get_selected_company_id(default_to_primary=True)
    company_filter_sql = "AND po.company_id = :company_id" if selected_company_id else ""
    params = {'company_id': selected_company_id} if selected_company_id else {}
    pos = db.session.execute(text("""
        SELECT
            po.po_id, po.po_no, s.supplier_name, po.total_amount,
            po.total_amount - IFNULL(p_sum.paid_amount, 0) AS outstanding_amount
        FROM purchase_orders po
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        LEFT JOIN (
            SELECT po_id, SUM(payment_amount) AS paid_amount
            FROM purchase_payments WHERE payment_date IS NOT NULL GROUP BY po_id
        ) p_sum ON po.po_id = p_sum.po_id
        WHERE po.total_amount > 0 """ + company_filter_sql + """
          AND po.total_amount - IFNULL(p_sum.paid_amount, 0) > 0
        ORDER BY po.order_date DESC
    """), params).mappings().all()
    today = beijing_today().strftime('%Y-%m-%d')
    return render_template('new_purchase_payment.html', purchase_orders=pos, today=today,
                           selected_company_id=selected_company_id)


@app.route('/edit-purchase-payment/<int:payment_id>', methods=['GET', 'POST'])
def edit_purchase_payment(payment_id):
    if request.method == 'POST':
        pp_company_id = None
        try:
            pp_company_id = db.session.execute(text("""
                SELECT po.company_id FROM purchase_payments pp
                JOIN purchase_orders po ON pp.po_id = po.po_id
                WHERE pp.payment_id = :id
            """), {'id': payment_id}).scalar()
            db.session.execute(text("""
                UPDATE purchase_payments
                SET payment_amount=:payment_amount, payment_date=:payment_date, remark=:remark
                WHERE payment_id=:id
            """), {
                'payment_amount': request.form['payment_amount'],
                'payment_date':   request.form.get('payment_date') or None,
                'remark':         request.form.get('remark'),
                'id':             payment_id,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update purchase payment record: {e}"
        return redirect(url_for('purchase_payments', company_id=pp_company_id))

    payment = db.session.execute(text("""
        SELECT pp.*, po.po_no, po.company_id, s.supplier_name
        FROM purchase_payments pp
        JOIN purchase_orders po ON pp.po_id = po.po_id
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        WHERE pp.payment_id = :id
    """), {'id': payment_id}).mappings().fetchone()
    if not payment:
        return "Purchase payment record not found!", 404
    today = beijing_today().strftime('%Y-%m-%d')
    return render_template('edit_purchase_payment.html', payment=payment, today=today)


@app.route('/delete-purchase-payment/<int:payment_id>', methods=['POST'])
def delete_purchase_payment(payment_id):
    pp_company_id = None
    try:
        pp_company_id = db.session.execute(text("""
            SELECT po.company_id FROM purchase_payments pp
            JOIN purchase_orders po ON pp.po_id = po.po_id
            WHERE pp.payment_id = :id
        """), {'id': payment_id}).scalar()
        db.session.execute(text(
            "DELETE FROM purchase_payments WHERE payment_id = :id"
        ), {'id': payment_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete purchase payment record: {e}"
    if pp_company_id:
        return redirect(url_for('purchase_payments', company_id=pp_company_id))
    return redirect(url_for('purchase_payments'))


# ───────────────────────────────────────────────
# Raw Material Batches
# ───────────────────────────────────────────────

@app.route('/new-rm-batch', methods=['GET', 'POST'])
def new_rm_batch():
    materials = db.session.execute(text("""
        SELECT component_id AS material_id, component_name AS material_name,
               component_type AS material_type, default_unit AS potency_unit
        FROM components WHERE is_active = 1
        ORDER BY component_type, component_name
    """)).mappings().all()
    if request.method == 'POST':
        try:
            qty = float(request.form['quantity_total'])
            db.session.execute(text("""
                INSERT INTO batches
                    (component_id, batch_no, batch_type, received_date, expiry_date,
                     quantity_total, quantity_available, unit, actual_potency, potency_unit,
                     supplier_id, supplier_batch_no, poi_id, unit_cost, notes)
                VALUES
                    (:cid, :bno, 'received', :recv, :exp, :qty, :qty, :unit, :potency, :p_unit,
                     :supplier_id, :sbno, :poi_id, :unit_cost, :notes)
            """), {
                'cid':         int(request.form['material_id']),
                'bno':         request.form['batch_no'].strip(),
                'recv':        request.form.get('received_date') or None,
                'exp':         request.form.get('expiry_date') or None,
                'qty':         qty,
                'unit':        request.form.get('unit') or 'kg',
                'potency':     request.form.get('actual_potency') or None,
                'p_unit':      request.form.get('potency_unit', 'billion CFU/g'),
                'supplier_id': request.form.get('supplier_id') or None,
                'sbno':        request.form.get('supplier_batch_no', '').strip() or None,
                'poi_id':      request.form.get('poi_id') or None,
                'unit_cost':   request.form.get('unit_cost') or None,
                'notes':       request.form.get('notes', '').strip() or None,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to add raw material batch: {e}"
        return redirect(url_for('raw_materials'))
    preselect = request.args.get('material_id', '')
    suppliers_list = db.session.execute(text(
        "SELECT supplier_id, supplier_name FROM suppliers WHERE is_active=1 ORDER BY supplier_name"
    )).mappings().all()
    return render_template('new_rm_batch.html', materials=materials,
                           suppliers=suppliers_list,
                           today=beijing_today().strftime('%Y-%m-%d'),
                           preselect=preselect,
                           suggested_batch_no=generate_batch_no('received'))


@app.route('/edit-rm-batch/<int:rm_batch_id>', methods=['GET', 'POST'])
def edit_rm_batch(rm_batch_id):
    batch = db.session.execute(text("""
        SELECT b.*, c.component_name AS material_name, c.component_type AS material_type
        FROM batches b
        JOIN components c ON b.component_id = c.component_id
        WHERE b.batch_id = :id AND b.batch_type = 'received'
    """), {'id': rm_batch_id}).mappings().first()
    if not batch:
        return "Batch not found", 404
    if request.method == 'POST':
        try:
            db.session.execute(text("""
                UPDATE batches SET
                    batch_no=:bno, received_date=:recv, expiry_date=:exp,
                    quantity_total=:qty_total, quantity_available=:qty_avail, unit=:unit,
                    actual_potency=:potency, potency_unit=:p_unit,
                    supplier_id=:supplier_id, supplier_batch_no=:sbno,
                    poi_id=:poi_id, unit_cost=:unit_cost,
                    status=:status, notes=:notes
                WHERE batch_id=:id
            """), {
                'id':          rm_batch_id,
                'bno':         request.form['batch_no'].strip(),
                'recv':        request.form.get('received_date') or None,
                'exp':         request.form.get('expiry_date') or None,
                'qty_total':   float(request.form['quantity_total']),
                'qty_avail':   float(request.form['quantity_available']),
                'unit':        request.form.get('unit') or 'kg',
                'potency':     request.form.get('actual_potency') or None,
                'p_unit':      request.form.get('potency_unit', 'billion CFU/g'),
                'supplier_id': request.form.get('supplier_id') or None,
                'sbno':        request.form.get('supplier_batch_no', '').strip() or None,
                'poi_id':      request.form.get('poi_id') or None,
                'unit_cost':   request.form.get('unit_cost') or None,
                'status':      request.form.get('status', 'available'),
                'notes':       request.form.get('notes', '').strip() or None,
            })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to update batch: {e}"
        return redirect(url_for('raw_materials'))
    suppliers_list = db.session.execute(text(
        "SELECT supplier_id, supplier_name FROM suppliers WHERE is_active=1 ORDER BY supplier_name"
    )).mappings().all()
    return render_template('edit_rm_batch.html', batch=batch, suppliers=suppliers_list)


@app.route('/delete-rm-batch/<int:rm_batch_id>', methods=['POST'])
def delete_rm_batch(rm_batch_id):
    try:
        count = db.session.execute(text(
            "SELECT COUNT(*) FROM production_consumptions WHERE raw_batch_id = :id"
        ), {'id': rm_batch_id}).scalar()
        if count:
            return f"Cannot delete: this raw material batch is linked to {count} production consumption record(s).", 400
        db.session.execute(text(
            "DELETE FROM batches WHERE batch_id=:id AND batch_type='received'"
        ), {'id': rm_batch_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return f"Failed to delete batch: {e}"
    return redirect(url_for('raw_materials'))


# ───────────────────────────────────────────────
# Product Formulas
# ───────────────────────────────────────────────

@app.route('/api/products/<int:product_id>/formula')
def get_product_formula_api(product_id):
    """Read-only API: returns formula line items for a product, for reference display on the
    "Production Receipt" page only; not used in any validation."""
    items = db.session.execute(text("""
        SELECT fi.target_amount, fi.unit, fi.notes,
               c.component_name AS material_name, c.component_type AS material_type
        FROM product_formulas pf
        JOIN formula_items fi ON fi.formula_id = pf.formula_id
        JOIN components c ON fi.component_id = c.component_id
        WHERE pf.product_id = :pid
        ORDER BY c.component_type, c.component_name
    """), {'pid': product_id}).mappings().all()
    return _jsonify_rows(items)


@app.route('/product-formula/<int:product_id>', methods=['GET', 'POST'])
def product_formula(product_id):
    product = db.session.execute(text(
        "SELECT * FROM products WHERE product_id=:id"
    ), {'id': product_id}).mappings().first()
    if not product:
        return "Product not found", 404

    if request.method == 'POST':
        try:
            formula = db.session.execute(text(
                "SELECT formula_id FROM product_formulas WHERE product_id=:pid"
            ), {'pid': product_id}).scalar()
            if not formula:
                db.session.execute(text(
                    "INSERT INTO product_formulas (product_id, notes) VALUES (:pid, :notes)"
                ), {'pid': product_id, 'notes': request.form.get('formula_notes', '') or None})
                formula = db.session.execute(text(
                    "SELECT LAST_INSERT_ID()"
                )).scalar()
            else:
                db.session.execute(text(
                    "UPDATE product_formulas SET notes=:notes WHERE formula_id=:fid"
                ), {'fid': formula, 'notes': request.form.get('formula_notes', '') or None})
                db.session.execute(text(
                    "DELETE FROM formula_items WHERE formula_id=:fid"
                ), {'fid': formula})

            component_ids  = request.form.getlist('material_id')
            target_amounts = request.form.getlist('target_amount')
            units          = request.form.getlist('fi_unit')
            fi_notes       = request.form.getlist('fi_notes')
            for component_id, amt, unit, note in zip(component_ids, target_amounts, units, fi_notes):
                if component_id and amt:
                    db.session.execute(text("""
                        INSERT INTO formula_items (formula_id, component_id, target_amount, unit, notes)
                        VALUES (:fid, :cid, :amt, :unit, :note)
                    """), {
                        'fid': formula, 'cid': int(component_id),
                        'amt': float(amt), 'unit': unit or 'billion CFU/g',
                        'note': note.strip() or None,
                    })
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return f"Failed to save formula: {e}"
        return redirect(url_for('product_formula', product_id=product_id))

    formula = db.session.execute(text(
        "SELECT * FROM product_formulas WHERE product_id=:pid"
    ), {'pid': product_id}).mappings().first()
    items = []
    if formula:
        items = db.session.execute(text("""
            SELECT fi.*,
                   fi.component_id AS material_id,
                   c.component_name AS material_name,
                   c.component_type AS material_type,
                   c.default_unit AS m_potency_unit
            FROM formula_items fi
            JOIN components c ON fi.component_id = c.component_id
            WHERE fi.formula_id=:fid
            ORDER BY c.component_type, c.component_name
        """), {'fid': formula['formula_id']}).mappings().all()
    materials = [dict(m) for m in db.session.execute(text("""
        SELECT component_id AS material_id,
               component_name AS material_name,
               component_type AS material_type,
               default_unit AS potency_unit
        FROM components
        WHERE is_active = 1
        ORDER BY component_type, component_name
    """)).mappings().all()]
    return render_template('product_formula.html',
                           product=product, formula=formula, items=items, materials=materials)


# ───────────────────────────────────────────────
# API: raw material batches for a material
# ───────────────────────────────────────────────

@app.route('/api/raw-materials')
def api_raw_materials():
    materials = db.session.execute(text("""
        SELECT component_id AS material_id, component_name AS material_name,
               component_type AS material_type, default_unit AS potency_unit
        FROM components WHERE is_active = 1
        ORDER BY component_type, component_name
    """)).mappings().all()
    return jsonify([dict(m) for m in materials])


if __name__ == '__main__':
    app.run(debug=True, port=5000)
