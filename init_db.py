from app import app, init_db, seed_db

with app.app_context():
    init_db()
    seed_db()
    print('DB initialized and seeded.')
