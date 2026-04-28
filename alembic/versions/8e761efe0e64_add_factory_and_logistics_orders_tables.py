"""add factory and logistics orders tables

Revision ID: 8e761efe0e64
Revises: 0019
Create Date: 2026-04-28 14:47:47.564436

"""
from typing import Union, Sequence
from alembic import op
import sqlalchemy as sa


revision: str = '8e761efe0e64'
down_revision: Union[str, None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('factory_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('offer_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False, server_default='draft'),
        sa.Column('partner_ref', sa.String(), nullable=False, server_default='FASSA'),
        sa.Column('date_planned', sa.String(), nullable=True),
        sa.Column('sent_to_factory_at', sa.String(), nullable=True),
        sa.Column('confirmed_at', sa.String(), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['offer_id'], ['pending_offers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_factory_orders_offer', 'factory_orders', ['offer_id'])
    
    op.create_table('logistics_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('offer_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False, server_default='draft'),
        sa.Column('route_id', sa.Integer(), nullable=True),
        sa.Column('container_type', sa.String(), nullable=True),
        sa.Column('booking_ref', sa.String(), nullable=True),
        sa.Column('departure_date', sa.String(), nullable=True),
        sa.Column('eta_date', sa.String(), nullable=True),
        sa.Column('delivered_at', sa.String(), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['offer_id'], ['pending_offers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['route_id'], ['shipping_routes.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_logistics_orders_offer', 'logistics_orders', ['offer_id'])


def downgrade() -> None:
    op.drop_index('idx_logistics_orders_offer', table_name='logistics_orders')
    op.drop_table('logistics_orders')
    op.drop_index('idx_factory_orders_offer', table_name='factory_orders')
    op.drop_table('factory_orders')
