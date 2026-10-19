from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from db_models import Base, InvoiceRow, InvoiceDetailRow, PaymentRow

def seed():
    engine = create_engine("sqlite:///reconciliation.db")
    Base.metadata.create_all(engine)
    
    with Session(engine) as session:
        # Clear existing data just in case
        session.query(InvoiceDetailRow).delete()
        session.query(InvoiceRow).delete()
        session.query(PaymentRow).delete()
        session.execute(text("DELETE FROM manual_reviews")) # If model is mapped or not
        
        today = date.today()
        
        # 1. Exact Match Scenario
        # Invoice 101, Amount $1,000, Buyer: Tech Solutions
        inv1 = InvoiceRow(
            id=1, invoice_number="INV-1001", company_code="NFL", status="Unpaid",
            buyer_business_name="Tech Solutions Inc", issue_date=today - timedelta(days=5),
            due_date=today + timedelta(days=25)
        )
        session.add(inv1)
        session.flush()
        session.add(InvoiceDetailRow(id=1, invoice_id=inv1.id, product_code="SVC-01", quantity=1, unit_price=Decimal("1000.00"), sales_value=Decimal("1000.00")))
        
        pmt1 = PaymentRow(
            id=1, reference="INV-1001", amount=Decimal("1000.00"), payment_date=today,
            sender_name="Tech Solutions Inc", allocation_status="UNALLOCATED"
        )
        session.add(pmt1)

        # 2. Fuzzy Match (Should Auto-Match)
        # Invoice 102, Amount $2,500
        inv2 = InvoiceRow(
            id=2, invoice_number="INV-1002", company_code="NFL", status="Unpaid",
            buyer_business_name="Global Corp LLC", issue_date=today - timedelta(days=10),
            due_date=today + timedelta(days=20)
        )
        session.add(inv2)
        session.flush()
        session.add(InvoiceDetailRow(id=2, invoice_id=inv2.id, product_code="PRD-99", quantity=2, unit_price=Decimal("1250.00"), sales_value=Decimal("2500.00")))
        
        pmt2 = PaymentRow(
            id=2, reference="INV1002", amount=Decimal("2500.00"), payment_date=today,
            sender_name="Global Corp", allocation_status="UNALLOCATED"
        )
        session.add(pmt2)

        # 3. Manual Review (Score ~0.6-0.8)
        # Right amount, completely wrong reference, slightly wrong name
        inv3 = InvoiceRow(
            id=3, invoice_number="INV-1003", company_code="NFL", status="Unpaid",
            buyer_business_name="Startup Co", issue_date=today - timedelta(days=2),
            due_date=today + timedelta(days=28)
        )
        session.add(inv3)
        session.flush()
        session.add(InvoiceDetailRow(id=3, invoice_id=inv3.id, product_code="PRD-10", quantity=1, unit_price=Decimal("4500.00"), sales_value=Decimal("4500.00")))
        
        pmt3 = PaymentRow(
            id=3, reference="WIRE-993812", amount=Decimal("4500.00"), payment_date=today,
            sender_name="Startup Company", allocation_status="UNALLOCATED"
        )
        session.add(pmt3)

        # 4. Discard (Wildly off)
        pmt4 = PaymentRow(
            id=4, reference="UNKNOWN-999", amount=Decimal("99999.00"), payment_date=today,
            sender_name="Mystery Payer", allocation_status="UNALLOCATED"
        )
        session.add(pmt4)

        # 5. Bulk Match
        # Invoices 104 and 105 total $3,000 for 'Acme Corp'
        inv4 = InvoiceRow(
            id=4, invoice_number="INV-1004", company_code="NFL", status="Unpaid",
            buyer_business_name="Acme Corp", buyer_iden_code="ACME-01", issue_date=today - timedelta(days=30),
            due_date=today
        )
        inv5 = InvoiceRow(
            id=5, invoice_number="INV-1005", company_code="NFL", status="Unpaid",
            buyer_business_name="Acme Corp", buyer_iden_code="ACME-01", issue_date=today - timedelta(days=15),
            due_date=today + timedelta(days=15)
        )
        session.add_all([inv4, inv5])
        session.flush()
        session.add(InvoiceDetailRow(id=4, invoice_id=inv4.id, product_code="P-1", quantity=1, unit_price=Decimal("1000.00"), sales_value=Decimal("1000.00")))
        session.add(InvoiceDetailRow(id=5, invoice_id=inv5.id, product_code="P-2", quantity=1, unit_price=Decimal("2000.00"), sales_value=Decimal("2000.00")))
        
        pmt5 = PaymentRow(
            id=5, reference="BULK-PAY", amount=Decimal("3000.00"), payment_date=today,
            sender_name="Acme Corporation", allocation_status="UNALLOCATED"
        )
        session.add(pmt5)

        session.commit()
        print("Database seeded with 5 invoices and 5 payments!")

if __name__ == "__main__":
    seed()
