"""Service-layer CRUD for users, plus linking a sign-in to one.

A user is identified by verified email. Their first sign-in stores the
identity provider's subject (``link_sign_in``); after that the subject alone
finds them (``get_user_by_subject``). Being a user grants nothing by itself
-- access comes from memberships (see ``memberships``) or ``is_superuser``.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import User
from brewerypi.services._validation import (
    clean_str,
    clean_timezone,
    optional_str,
)
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


def normalize_email(email: str) -> str:
    """Trim and lowercase an email; reject anything that is not one."""
    email = clean_str(email, "email", 255).lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or any(c.isspace() for c in email):
        raise ValidationError(f"{email!r} is not an email address")
    return email


def list_users(session: Session) -> list[User]:
    """Return every user, ordered by email."""
    return list(session.scalars(select(User).order_by(User.email)).all())


def get_user(session: Session, user_id: int) -> User:
    """Return one user, or raise NotFoundError."""
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"no user with id {user_id}")
    return user


def get_user_by_email(session: Session, email: str) -> User:
    """Return the user with this email (any case), or raise NotFoundError."""
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        raise NotFoundError(f"no user with email {email!r}")
    return user


def get_user_by_subject(session: Session, subject: str) -> User | None:
    """Return the user already linked to this sign-in subject, if any."""
    return session.scalar(select(User).where(User.auth_subject == subject))


def create_user(
    session: Session,
    email: str,
    name: str | None = None,
    timezone: str | None = None,
    is_superuser: bool = False,
) -> User:
    """Add a person to the allowlist (email unique, case-insensitive)."""
    email = normalize_email(email)
    if session.scalar(select(User.id).where(User.email == email)):
        raise ConflictError(f"a user with email {email!r} already exists")
    user = User(
        email=email,
        name=_clean_name(name),
        timezone=_optional_timezone(timezone),
        is_superuser=is_superuser,
    )
    session.add(user)
    session.flush()
    return user


def update_user(
    session: Session,
    user_id: int,
    name: str | None = None,
    timezone: str | None = None,
    is_superuser: bool | None = None,
    is_active: bool | None = None,
) -> User:
    """Update a user; only provided fields change.

    A blank ``name`` or ``timezone`` clears it -- with no timezone of their
    own, a user sees each site's local time.
    """
    user = get_user(session, user_id)
    if name is not None:
        user.name = _clean_name(name)
    if timezone is not None:
        user.timezone = _optional_timezone(timezone)
    if is_superuser is not None:
        user.is_superuser = is_superuser
    if is_active is not None:
        user.is_active = is_active
    session.flush()
    return user


def reset_sign_in(session: Session, user_id: int) -> User:
    """Forget a user's linked sign-in so their next one re-links by email.

    For when someone's identity-provider account was deleted and recreated
    (new subject, same email), or after switching identity providers.
    """
    user = get_user(session, user_id)
    user.auth_subject = None
    session.flush()
    return user


def delete_user(session: Session, user_id: int) -> None:
    """Delete a user and their memberships.

    Prefer ``update_user(is_active=False)`` for someone who has left: once
    readings and batches record who made them, the record has to stay.
    """
    session.delete(get_user(session, user_id))
    session.flush()


def link_sign_in(
    session: Session, subject: str, email: str | None, email_verified: bool
) -> User:
    """Match a first-time sign-in to the allowlist by email and link it.

    Refuses (PermissionDeniedError) an unverified email, one not on the
    list, and a user already linked to a *different* subject -- the latter
    means their account was recreated, which an administrator confirms with
    ``reset_sign_in`` rather than the server assuming it.
    """
    existing = get_user_by_subject(session, subject)
    if existing is not None:
        return existing
    if not email or not email_verified:
        raise PermissionDeniedError(
            "your sign-in has no verified email address, so it can't be "
            "matched to a Brewery Pi user"
        )
    email = normalize_email(email)
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        raise PermissionDeniedError(
            f"{email} is not a Brewery Pi user; ask an administrator to "
            "add you"
        )
    if user.auth_subject is not None:
        raise PermissionDeniedError(
            f"{email} is already linked to a different sign-in; ask an "
            "administrator to reset it"
        )
    user.auth_subject = subject
    session.flush()
    return user


def _clean_name(name: str | None) -> str | None:
    name = optional_str(name)
    if name is not None and len(name) > 100:
        raise ValidationError("name exceeds 100 characters")
    return name


def _optional_timezone(timezone: str | None) -> str | None:
    timezone = optional_str(timezone)
    return clean_timezone(timezone) if timezone is not None else None
