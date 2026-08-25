"""
SQLAlchemy models for the BK menu DB (Neon Postgres).

Schema shape:
- categories / items / item_sizes: standard shape.
- combos: composite primary key (id, is_veg) -- a combo "family" (id)
  exists in both a veg and non-veg row.
- combo_items: composite primary key (combo_id, combo_is_veg, item_id),
  with a composite foreign key back to combos(id, is_veg).
"""

from typing import Optional, List
from sqlalchemy import (
    Integer, String, Float, Boolean, ForeignKey, ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    items: Mapped[List["Item"]] = relationship(back_populates="category")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("name", "category_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    calories: Mapped[Optional[int]] = mapped_column(Integer)
    protein_g: Mapped[Optional[float]] = mapped_column(Float)
    piece_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_veg: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    category: Mapped["Category"] = relationship(back_populates="items")
    sizes: Mapped[List["ItemSize"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class ItemSize(Base):
    __tablename__ = "item_sizes"
    __table_args__ = (UniqueConstraint("item_id", "size"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    size: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)

    item: Mapped["Item"] = relationship(back_populates="sizes")


class Combo(Base):
    """A combo 'family' identified by id, existing in a veg and a
    non-veg variant -- both share the same id, differentiated by is_veg.
    This is why (id, is_veg) is the composite primary key rather than
    just id."""
    __tablename__ = "combos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_veg: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    num_people: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    combo_items: Mapped[List["ComboItem"]] = relationship(
        back_populates="combo", cascade="all, delete-orphan"
    )


class ComboItem(Base):
    __tablename__ = "combo_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["combo_id", "combo_is_veg"], ["combos.id", "combos.is_veg"],
            ondelete="CASCADE",
        ),
    )

    combo_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    combo_is_veg: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    combo: Mapped["Combo"] = relationship(back_populates="combo_items")
    item: Mapped["Item"] = relationship()
