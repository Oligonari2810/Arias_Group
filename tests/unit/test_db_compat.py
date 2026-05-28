from db.compat import translate_sql


def test_translate_sql_keeps_normal_insert_strict():
    sql = "INSERT INTO pending_offers (offer_number) VALUES (?)"

    translated = translate_sql(sql)

    assert translated == "INSERT INTO pending_offers (offer_number) VALUES (%s)"
    assert "ON CONFLICT DO NOTHING" not in translated


def test_translate_sql_converts_insert_or_ignore_only():
    sql = "INSERT OR IGNORE INTO doc_sequences (prefix, last_number) VALUES (?, ?)"

    translated = translate_sql(sql)

    assert translated == (
        "INSERT INTO doc_sequences (prefix, last_number) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING"
    )
