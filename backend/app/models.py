from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class AppUser(Base):
    """
    Пользователь приложения. Идентифицируется по email.
    Если пользователь вводит другой API ключ, но с той же почтой - это тот же пользователь.
    """

    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    credentials: Mapped[list["ApiCredential"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    todo_lists: Mapped[list["TodoList"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    todo_tasks: Mapped[list["TodoTask"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    gantt_states: Mapped[list["GanttState"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    improve_task_orders: Mapped[list["ImproveTaskOrder"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    team_configs: Mapped[list["TeamConfig"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")
    custom_teams: Mapped[list["CustomTeam"]] = relationship(back_populates="app_user", cascade="all, delete-orphan")


class ApiCredential(Base):
    """
    Серверное хранилище Jira API ключа.

    В cookie-сессии храним только session_key (идентификатор), сам ключ хранится в БД.
    Привязан к AppUser по email.
    """

    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    jira_api_key: Mapped[str] = mapped_column(String(512), nullable=False)
    jira_email: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    app_user: Mapped["AppUser"] = relationship(back_populates="credentials")
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


class TeamTelegramSetting(Base):
    """
    Настройки Telegram-рассылки по Jira-команде.
    """

    __tablename__ = "team_telegram_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, unique=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("api_credentials.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    team: Mapped[Team] = relationship()
    credential: Mapped[ApiCredential] = relationship()


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
    """
    Связь пользователей Jira с командами.
    Это глобальная связь - все пользователи видят одних и тех же участников команды.
    """
    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="teams")

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)


class TeamConfig(Base):
    """
    Конфигурация команды для конкретного пользователя приложения.
    Хранит, каких пользователей Jira выбрал пользователь для отображения в команде.
    """
    __tablename__ = "team_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False) # Может быть id из teams или custom_teams
    jira_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    app_user: Mapped["AppUser"] = relationship(back_populates="team_configs")
    # team_id может ссылаться на `teams.id` ИЛИ `custom_teams.id` (в зависимости от is_custom),
    # поэтому здесь нельзя объявлять обычный relationship() без ForeignKey/primaryjoin.
    # Если понадобится навигация, сделаем две viewonly связи с явным primaryjoin.
    jira_user: Mapped[User] = relationship()

    __table_args__ = (UniqueConstraint("app_user_id", "team_id", "jira_user_id", "is_custom", name="uq_team_config"),)


class CustomTeam(Base):
    """
    Пользовательские команды, созданные пользователем приложения.
    """
    __tablename__ = "custom_teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(30), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    app_user: Mapped["AppUser"] = relationship(back_populates="custom_teams")


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


class ImproveTaskOrder(Base):
    """
    Хранение пользовательского порядка задач в табе Improve.
    Порядок привязан к пользователю приложения (AppUser).
    """
    
    __tablename__ = "improve_task_order"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "SDCS-123"
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # Порядковый номер (0, 1, 2, ...)
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    app_user: Mapped["AppUser"] = relationship(back_populates="improve_task_orders")
    
    __table_args__ = (UniqueConstraint("app_user_id", "task_key", name="uq_improve_task_order"),)


class GanttState(Base):
    """
    Хранение состояния диаграммы Ганта (позиции задач, связи, режим).
    Состояние привязано к пользователю приложения (AppUser) и team.
    """
    
    __tablename__ = "gantt_state"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    state_data: Mapped[str] = mapped_column(String(10000), nullable=False)  # JSON строка с состоянием
    auto_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    app_user: Mapped["AppUser"] = relationship(back_populates="gantt_states")
    team: Mapped[Team] = relationship()
    
    __table_args__ = (UniqueConstraint("app_user_id", "team_id", name="uq_gantt_state"),)


class TodoList(Base):
    """
    Пользовательские списки задач Todo.
    Привязаны к пользователю приложения (AppUser).
    """
    
    __tablename__ = "todo_lists"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Порядок отображения
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    app_user: Mapped["AppUser"] = relationship(back_populates="todo_lists")
    tasks: Mapped[list["TodoTask"]] = relationship(back_populates="list", cascade="all, delete-orphan")


class TodoTask(Base):
    """
    Задачи Todo.
    Могут быть привязаны к списку или к системному списку (my-day, important, etc.).
    Привязаны к пользователю приложения (AppUser).
    """
    
    __tablename__ = "todo_tasks"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    list_id: Mapped[int | None] = mapped_column(ForeignKey("todo_lists.id", ondelete="CASCADE"), nullable=True)
    list_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 'my-day', 'important', 'planned', 'all', 'completed' или null
    
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal", server_default="'normal'")  # 'normal' или 'important'
    
    due_date: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)  # Дата выполнения
    reminder: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)  # Напоминание
    repeat: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'daily', 'weekly', 'monthly' или null
    
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # Заметки
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Порядок в списке
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    app_user: Mapped["AppUser"] = relationship(back_populates="todo_tasks")
    list: Mapped["TodoList | None"] = relationship("TodoList", back_populates="tasks")
    subtasks: Mapped[list["TodoSubtask"]] = relationship("TodoSubtask", back_populates="task", cascade="all, delete-orphan")


class TodoSubtask(Base):
    """
    Подзадачи Todo.
    Привязаны к основной задаче.
    """
    
    __tablename__ = "todo_subtasks"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("todo_tasks.id", ondelete="CASCADE"), nullable=False)
    
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Порядок в списке подзадач
    
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    task: Mapped["TodoTask"] = relationship("TodoTask", back_populates="subtasks")
