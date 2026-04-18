#!/bin/bash
# Backup diario de fassa_ops.db con retención de 30 días

BASE_DIR="/Users/anamarperezmarrero/Mvp_Arias_Fassa"
DB="$BASE_DIR/fassa_ops.db"
BACKUPS="$BASE_DIR/backups"
mkdir -p "$BACKUPS"

TS=$(date +%Y%m%d_%H%M%S)
DEST="$BACKUPS/fassa_ops_daily_$TS.db"

# sqlite3 .backup es atómico y seguro con la DB en uso
/usr/bin/sqlite3 "$DB" ".backup '$DEST'"

# Prune: mantener solo daily de los últimos 30 días
find "$BACKUPS" -name 'fassa_ops_daily_*.db' -type f -mtime +30 -delete
