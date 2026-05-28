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
    row = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    return row['id']


def _login_as(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


def _create_offer(db) -> int:
    offer_number = f'TEST-PERM-{now_iso()}'
    db.execute(
        '''INSERT INTO pending_offers
           (offer_number, client_name, project_name, waste_pct, margin_pct, fx_rate,
            lines_json, total_product_eur, total_logistic_eur, total_final_eur,
            status, incoterm, container_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            offer_number, 'Cliente Permisos', 'Proyecto Permisos',
            5, 33, 1.18, '[]', 100, 0, 149.25,
            'pending', 'EXW', 0, now_iso(),
        ),
    )
    db.commit()
    row = db.execute(
        'SELECT id FROM pending_offers WHERE offer_number = ?', (offer_number,)
    ).fetchone()
    return row['id']


def test_viewer_cannot_open_admin_config_pages(app, db):
    viewer_id = _create_user(db, 'viewer-permissions-test', 'viewer')

    with app.test_client() as client:
        _login_as(client, viewer_id)

        masters = client.get('/masters')
        config = client.get('/config')

    assert masters.status_code == 403
    assert config.status_code == 403


def test_viewer_does_not_see_admin_navigation_or_offer_actions(app, db):
    viewer_id = _create_user(db, 'viewer-ui-permissions-test', 'viewer')
    _create_offer(db)

    with app.test_client() as client:
        _login_as(client, viewer_id)

        dashboard = client.get('/')
        budgets = client.get('/presupuestos')
        logistics = client.get('/logistics')

    assert b'Configuraci' not in dashboard.data
    assert b'Aprobar' not in budgets.data
    assert b'Rechazar' not in budgets.data
    assert b'Borrar' not in budgets.data
    assert b'Confirmar' not in logistics.data


def test_viewer_cannot_mutate_offer_admin_endpoints(app, db, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    viewer_id = _create_user(db, 'viewer-offer-test', 'viewer')

    with app.test_client() as client:
        _login_as(client, viewer_id)

        responses = [
            client.post('/api/update-full-offer', json={'editId': 1}),
            client.post('/api/update-offer', json={'id': 1}),
            client.post('/api/delete-offer', json={'id': 1}),
            client.post('/api/offer-status', json={'id': 1, 'status': 'approved'}),
        ]

    assert [r.status_code for r in responses] == [403, 403, 403, 403]


def test_admin_reaches_offer_endpoint_validation(app, db, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    admin_id = _create_user(db, 'admin-permissions-test', 'admin')

    with app.test_client() as client:
        _login_as(client, admin_id)
        response = client.post('/api/update-offer', json={})

    assert response.status_code == 400
