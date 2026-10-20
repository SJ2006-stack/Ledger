import pytest

from app.accounting import AccountingStore
from app.db import connect, init_db
from app.seed import seed


@pytest.fixture
def store(tmp_path) -> AccountingStore:
    conn = connect(tmp_path / "books.db")
    init_db(conn)
    seed(conn)
    return AccountingStore(conn)
