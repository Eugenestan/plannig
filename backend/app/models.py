from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class ApiCredential(Base):
    """
    Серверное хранилище Jira API ключа.

    В cookie-сессии храним только session_key (идентификатор), сам ключ хранится в БД.
    """

    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    jira_api_key: Mapped[str] = mapped_column(String(512), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    teams: Mapped[list["CredentialTeam"]] = relationship(back_populates="credential", cascade="all, delete-orphan")
    users: Mapped[list["CredentialUser"]] = relationship(back_populates="credential", cascade="all, delete-orphan")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jira_field_id: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. customfield_10500
    jira_team_id: Mapped[str] = mapped_column(String(128), nullable=False)  # UUID-ish in Jira response
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    members: Mapped[list[TeamMember]] = relationship(back_populates="team", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("jira_field_id", "jira_team_id", name="uq_team_jira_field_team"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jira_account_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    teams: Mapped[list[TeamMember]] = relationship(back_populates="user", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="teams")

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)


class CredentialTeam(Base):
    """
    Связь: какие команды доступны текущему credential (пользователю/браузеру).
    """

    __tablename__ = "credential_teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    credential: Mapped[ApiCredential] = relationship(back_populates="teams")
    team: Mapped[Team] = relationship()

    __table_args__ = (UniqueConstraint("credential_id", "team_id", name="uq_credential_team"),)


class CredentialUser(Base):
    """
    Связь: какие пользователи доступны текущему credential.
    Используется для того, чтобы один пользователь системы не видел пользователей другого.
    """

    __tablename__ = "credential_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    credential: Mapped[ApiCredential] = relationship(back_populates="users")
    user: Mapped[User] = relationship()

    __table_args__ = (UniqueConstraint("credential_id", "user_id", name="uq_credential_user"),)

