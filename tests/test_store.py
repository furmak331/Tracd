"""The database: settings and watches survive upgrades."""

from tracd.store import Store


def test_a_database_from_an_older_version_gets_the_new_columns(tmp_path):
    import sqlite3

    from tracd.store import SCHEMA

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.replace(",\n    acked_at REAL", ""))
    con.execute("INSERT INTO watches (shop, product_url, poll_url, title, size_id, size_label, created_at) "
                "VALUES ('flipkart', 'https://f/p', 'https://f/p', 'Shoe', 'x', 'UK 9', 1)")
    con.commit()
    con.close()
    s = Store(db)
    w = s.watches()[0]
    assert w.title == "Shoe" and w.acked_at is None
    assert s.update_watch(w.id, acked_at=5.0).acked_at == 5.0
