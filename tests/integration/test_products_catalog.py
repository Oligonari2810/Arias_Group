import bcrypt

from app import now_iso


def _create_user(db, username: str, role: str) -> int:
    password_hash = bcrypt.hashpw(b'test-password-1234', bcrypt.gensalt()).decode()
    db.execute(
        '''INSERT INTO users (username, password_hash, role, full_name, email, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (username) DO NOTHING''',
        (username, password_hash, role, username, f'{username}@example.test', now_iso()),
    )
    db.commit()
    return db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()['id']


def _login_as(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


def test_catalog_does_not_mark_everything_inactive_without_soft_delete_columns(app, db):
    user_id = _create_user(db, 'catalog-viewer-test', 'viewer')

    with app.test_client() as client:
        _login_as(client, user_id)
        response = client.get('/products')

    assert response.status_code == 200
    assert b'SKUs activos' in response.data
    assert '⊘'.encode() not in response.data
    assert b'text-decoration:line-through' not in response.data
    assert b'Extra%' not in response.data


def test_catalog_price_sync_ignores_legacy_extra_discount(app, db, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    admin_id = _create_user(db, 'catalog-admin-price-test', 'admin')
    product = db.execute(
        'SELECT id FROM products WHERE pvp_eur_unit IS NOT NULL ORDER BY id LIMIT 1'
    ).fetchone()

    with app.test_client() as client:
        _login_as(client, admin_id)
        response = client.post(
            f'/api/products/{product["id"]}',
            json={'pvp_eur_unit': 100, 'discount_pct': 40},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['product']['precio_arias_eur_unit'] == 60
    assert payload['product']['unit_price_eur'] == 60
