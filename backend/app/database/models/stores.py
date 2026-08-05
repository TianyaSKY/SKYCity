"""Store + store product rows: shop locations and their stock (M5 economy).

``sell_price`` is what customers pay to buy from the store; ``buy_price`` is
what the store pays when buying from agents. Stock starts at stock_cap and is
restocked at the store's daily open hour (R15).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Store(Base):
    """A shop: one per location (location_id soft reference to locations)."""

    __tablename__ = "stores"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    store_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    location_id: Mapped[str] = mapped_column(String(64), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Store(world_id={self.world_id!r}, store_id={self.store_id!r})"


class StoreProduct(Base):
    """One product a store sells/buys, with live stock."""

    __tablename__ = "store_products"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "store_id"],
            ["stores.world_id", "stores.store_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["world_id", "item_id"],
            ["items.world_id", "items.item_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False)
    # M12: anchor price promos reset to each day (base = non-promo price).
    base_sell_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buy_price: Mapped[int] = mapped_column(Integer, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    restock_daily: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StoreProduct(store={self.store_id!r}, item={self.item_id!r}, stock={self.stock})"
