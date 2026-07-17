"""Synthetic customer profiles covering all four MVP use cases (PRD §10 step 4).

No real PII — every record here is fabricated for demos. Field schemas differ
per use case (Insurance vs. Collections vs. Credit Card vs. Loan), which is
exactly why CRM attributes live in JSON (PRD §06).
"""

from __future__ import annotations

from koel.db.models import UseCase

SEED_CUSTOMERS: list[dict] = [
    {
        "id": "cust_cc_001",
        "name": "Rahul Sharma",
        "phone": "+919000000001",
        "language": "hinglish",
        "bank": "HDFC Bank",
        "use_case": UseCase.credit_card,
        "dnd": False,
        "attributes": {
            "product": "Regalia Credit Card",
            "offer_type": "pre-approved",
            "annual_fee_waived": True,
            "existing_cards": 2,
            "bureau_score": 780,
            "benefits": ["lounge access", "4x reward points on dining"],
        },
    },
    {
        "id": "cust_col_001",
        "name": "Priya Nair",
        "phone": "+919000000002",
        "language": "hinglish",
        "bank": "ICICI Bank",
        "use_case": UseCase.collections,
        "dnd": False,
        "attributes": {
            "outstanding_amount": 14500,
            "due_date": "2026-06-20",
            "dpd": 3,
            "segment": "early_delinquency",
            "min_payment": 2500,
        },
    },
    {
        "id": "cust_loan_001",
        "name": "Arjun Mehta",
        "phone": "+919000000003",
        "language": "en",
        "bank": "HDFC Bank",
        "use_case": UseCase.personal_loan,
        "dnd": False,
        "attributes": {
            "pre_approved_amount": 500000,
            "tenure_months": 36,
            "indicative_emi": 16200,
            "interest_rate": 10.5,
        },
    },
    {
        "id": "cust_ins_001",
        "name": "Meera Iyer",
        "phone": "+919000000004",
        "language": "hi",
        "bank": "Devotica Life",
        "use_case": UseCase.insurance,
        "dnd": False,
        "attributes": {
            "product": "Term Life Plan",
            "sum_assured": 10000000,
            "annual_premium": 12000,
            "nominee_required": True,
            "irdai_disclosures": ["free_look_period", "premium_payment_term"],
        },
    },
    {
        # DND record to exercise the TRAI scrub path (PRD §11).
        "id": "cust_dnd_001",
        "name": "Sanjay Gupta",
        "phone": "+919000000005",
        "language": "hinglish",
        "bank": "ICICI Bank",
        "use_case": UseCase.credit_card,
        "dnd": True,
        "attributes": {"product": "Millennia Credit Card", "offer_type": "pre-approved"},
    },
]
