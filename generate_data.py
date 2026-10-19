import csv
import random
import datetime

random.seed(42)

# --- INVOICES (100) ---
invoices = []
companies = [
    "Tech Solutions Inc", "Global Corp LLC", "Startup Co", "Acme Corp",
    "Haball Technologies", "DataFlow Systems", "Cloud 9", "NextGen IT",
    "Alpha Industries", "Beta Services", "Gamma Logistics", "Delta Finance"
]

base_date = datetime.date(2026, 6, 1)

for i in range(1, 101):
    inv_num = f"INV-{1000 + i}"
    company = random.choice(companies)
    issue_date = base_date + datetime.timedelta(days=random.randint(0, 30))
    due_date = issue_date + datetime.timedelta(days=30)
    qty = random.randint(1, 10)
    unit_price = round(random.uniform(100.0, 5000.0), 2)
    sales_value = round(qty * unit_price, 2)
    
    invoices.append({
        "invoice_number": inv_num,
        "company_code": "NFL",
        "buyer_business_name": company,
        "buyer_iden_code": "",
        "issue_date": issue_date.strftime("%Y-%m-%d"),
        "due_date": due_date.strftime("%Y-%m-%d"),
        "product_code": f"PRD-{random.randint(10,99)}",
        "quantity": qty,
        "unit_price": unit_price,
        "sales_value": sales_value
    })

# Write Invoices
with open("sample_invoices.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "invoice_number", "company_code", "buyer_business_name", 
        "buyer_iden_code", "issue_date", "due_date", "product_code", 
        "quantity", "unit_price", "sales_value"
    ])
    writer.writeheader()
    writer.writerows(invoices)


# --- PAYMENTS / PSIDs (50) ---
payments = []
pmt_date = base_date + datetime.timedelta(days=35)

# Scenario 1: Exact matches (25 payments)
for i in range(25):
    inv = invoices[i]
    payments.append({
        "reference": inv["invoice_number"],
        "amount": inv["sales_value"],
        "payment_date": pmt_date.strftime("%Y-%m-%d"),
        "sender_name": inv["buyer_business_name"]
    })

# Scenario 2: Fuzzy matches - typos in ref/amount/sender (10 payments)
for i in range(25, 35):
    inv = invoices[i]
    
    # 50% chance of typo in reference
    ref = inv["invoice_number"]
    if random.choice([True, False]):
        ref = ref.replace("-", "") # e.g. INV1026
        
    # 50% chance of slight amount deviation (e.g. wire fee deducted)
    amt = inv["sales_value"]
    if random.choice([True, False]):
        amt = round(amt - random.uniform(1.0, 25.0), 2)
        
    # 50% chance of sender name variation
    sender = inv["buyer_business_name"]
    if random.choice([True, False]):
        sender = sender.split(" ")[0] # e.g. Tech Solutions Inc -> Tech
        
    payments.append({
        "reference": ref,
        "amount": amt,
        "payment_date": pmt_date.strftime("%Y-%m-%d"),
        "sender_name": sender
    })

# Scenario 3: Bulk matches - 1 payment covers 2 or 3 invoices (5 payments covering 12 invoices)
idx = 35
for _ in range(5):
    num_invs = random.randint(2, 3)
    bulk_invs = invoices[idx:idx+num_invs]
    idx += num_invs
    
    total_amt = round(sum(i["sales_value"] for i in bulk_invs), 2)
    # the sender name should ideally match the buyer, just take the first one
    buyer = bulk_invs[0]["buyer_business_name"]
    
    payments.append({
        "reference": f"BULK-{random.randint(1000,9999)}",
        "amount": total_amt,
        "payment_date": pmt_date.strftime("%Y-%m-%d"),
        "sender_name": buyer
    })

# Scenario 4: Discarded / Unmatched (10 payments)
for i in range(10):
    payments.append({
        "reference": f"UNKNOWN-{random.randint(10000,99999)}",
        "amount": round(random.uniform(10000.0, 50000.0), 2),
        "payment_date": pmt_date.strftime("%Y-%m-%d"),
        "sender_name": "Mystery Sender Corp"
    })

# Shuffle payments so they aren't completely ordered
random.shuffle(payments)

# Write Payments
with open("sample_payments.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "reference", "amount", "payment_date", "sender_name"
    ])
    writer.writeheader()
    writer.writerows(payments)

print("Generated 100 invoices in sample_invoices.csv")
print(f"Generated {len(payments)} payments in sample_payments.csv")
