from dataclasses import dataclass, field

from app.accounting import AccountingStore


@dataclass(frozen=True)
class Intention:
    idempotency_key: str
    expect_row: bool
    vendor_id: int | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    status: str | None = None
    subtotal_cents: int | None = None
    tax_cents: int | None = None
    total_cents: int | None = None


@dataclass
class Verification:
    ok: bool
    mismatches: list[str] = field(default_factory=list)
    observed_id: int | None = None
    observed_status: str | None = None


def verify(store: AccountingStore, intention: Intention) -> Verification:
    observed = store.get_by_idempotency(intention.idempotency_key)
    if not intention.expect_row:
        if observed is None:
            return Verification(ok=True)
        return Verification(
            ok=False,
            mismatches=["unexpected_row"],
            observed_id=int(observed["id"]),
            observed_status=observed["status"],
        )
    if observed is None:
        return Verification(ok=False, mismatches=["missing_row"])
    mismatches: list[str] = []
    comparisons = {
        "vendor_id": (intention.vendor_id, observed["vendor_id"]),
        "invoice_number": (intention.invoice_number, observed["invoice_number"]),
        "po_number": (intention.po_number, observed["po_number"]),
        "status": (intention.status, observed["status"]),
        "subtotal_cents": (intention.subtotal_cents, observed["subtotal_cents"]),
        "tax_cents": (intention.tax_cents, observed["tax_cents"]),
        "total_cents": (intention.total_cents, observed["total_cents"]),
    }
    for name, (expected, actual) in comparisons.items():
        if expected != actual:
            mismatches.append(name)
    return Verification(
        ok=not mismatches,
        mismatches=mismatches,
        observed_id=int(observed["id"]),
        observed_status=observed["status"],
    )
