"""SQLAlchemy ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _new_link_id() -> str:
    """Generate a 32-char URL-safe random ID (no dashes, no padding)."""
    return uuid.uuid4().hex


class User(Base):
    """A registered user with a specific role.

    Admins have full access to all files and the admin panel.
    Uploaders can create links and see only their own files in the admin panel.
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="uploader")  # "admin" or "uploader"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    links: Mapped[List["Link"]] = relationship(
        "Link",
        back_populates="uploader",
        foreign_keys="Link.uploader_id",
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self) -> str:
        return f"<User username={self.username!r} role={self.role!r}>"


class Setting(Base):
    """Arbitrary key-value application settings (e.g. footer text)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<Setting key={self.key!r}>"


class Link(Base):
    """A shareable link pointing to one or more files."""

    __tablename__ = "links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_link_id)

    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Set to True the moment a successful download completes (or admin revokes).
    # "Pending" links are those with is_downloaded = False. A link lives
    # forever until either happens — there is no time-based expiry.
    is_downloaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Which user created this link (None for links created before migration)
    uploader_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        ForeignKey("users.username", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    files: Mapped[List["File"]] = relationship(
        "File",
        back_populates="link",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    uploader: Mapped[Optional[User]] = relationship(
        "User",
        back_populates="links",
        foreign_keys=[uploader_id],
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Link id={self.id!r} downloaded={self.is_downloaded} files={len(self.files)}>"


class File(Base):
    """A single file belonging to a Link."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filepath: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    link: Mapped[Link] = relationship("Link", back_populates="files")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<File id={self.id} name={self.original_filename!r}>"
