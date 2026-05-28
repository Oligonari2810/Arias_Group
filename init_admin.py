from __future__ import annotations

import os
import sys

import bcrypt

from app import app, get_db, init_db, now_iso


def main() -> int:
    username = os.environ.get('ADMIN_USERNAME', '').strip()
    password = os.environ.get('ADMIN_PASSWORD', '')
    full_name = os.environ.get('ADMIN_FULL_NAME', username or 'Admin')
    email = os.environ.get('ADMIN_EMAIL', '')

    if not username or not password:
        print('Define ADMIN_USERNAME y ADMIN_PASSWORD antes de crear el admin.')
        return 2
    if len(password) < 12:
        print('ADMIN_PASSWORD debe tener al menos 12 caracteres.')
        return 2

    with app.app_context():
        init_db()
        db = get_db()
        existing = db.execute(
            'SELECT id FROM users WHERE username = ?', (username,)
        ).fetchone()
        if existing:
            print(f'El usuario {username!r} ya existe; no se modificó.')
            return 0

        password_hash = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()
        db.execute(
            '''INSERT INTO users
               (username, password_hash, role, full_name, email, created_at)
               VALUES (?, ?, 'admin', ?, ?, ?)''',
            (username, password_hash, full_name, email, now_iso()),
        )
        db.commit()
        print(f'Admin {username!r} creado correctamente.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
