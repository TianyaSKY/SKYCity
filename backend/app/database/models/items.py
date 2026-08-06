"""Item rows: the world's item catalog, seeded per world (M5 economy).

Copied from world_data/items/items.json at world creation so each world can
evolve independently (prices/stocks are per-world state).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Item(Base):
    """A buyable/sellable/usable item definition (per world)."""

    __tablename__ = "items"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # food|material|tool|decoration
    satiety_restore: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # M12: mood restoration (usable non-food items), work wage bonus %,
    # extra yield per produced unit.
    mood_restore: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    work_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yield_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Item(world_id={self.world_id!r}, item_id={self.item_id!r})"
