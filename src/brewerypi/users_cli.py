"""``brewerypi-users``: manage who may sign in, from the server's shell.

This is the bootstrap -- the admin tools need an admin to exist, so the first
one is created here:

    brewerypi-users add you@brewery.com --name "Your Name" --superuser

and then, for everyone else:

    brewerypi-users add brewer@brewery.com
    brewerypi-users grant brewer@brewery.com BRW --role operator
    brewerypi-users grant brewer@brewery.com BRW --role operator --site ATL
    brewerypi-users update brewer@brewery.com --timezone America/Denver
    brewerypi-users update brewer@brewery.com --no-active      # has left
    brewerypi-users list

Enterprises and sites are given by id or abbreviation. ``grant`` sets the
membership outright (creating or replacing it): no ``--site`` means every
site, including future ones. Reads ``DATABASE_URL`` like everything else.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import services
from brewerypi.config import DATABASE_URL
from brewerypi.models import MEMBERSHIP_ROLES, Enterprise, Site, User
from brewerypi.services import NotFoundError, ServiceError


def main(
    argv: Sequence[str] | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> int:
    """Run one command; return the process exit status."""
    args = _parser().parse_args(argv)
    if session_factory is None:
        session_factory = sessionmaker(create_engine(DATABASE_URL))
    with session_factory() as session:
        try:
            args.command(session, args)
        except ServiceError as exc:
            session.rollback()
            print(f"error: {exc}", file=sys.stderr)
            return 1
        session.commit()
    return 0


def _add(session: Session, args: argparse.Namespace) -> None:
    user = services.create_user(
        session,
        args.email,
        name=args.name,
        timezone=args.timezone,
        is_superuser=args.superuser,
    )
    print(f"added {_describe(user)}")
    if not user.is_superuser:
        print("  no access yet: grant them a role in an enterprise")


def _list(session: Session, args: argparse.Namespace) -> None:
    users = services.list_users(session)
    if not users:
        print("no users")
    for user in users:
        _print_user(session, user)


def _show(session: Session, args: argparse.Namespace) -> None:
    _print_user(session, services.get_user_by_email(session, args.email))


def _update(session: Session, args: argparse.Namespace) -> None:
    user = services.get_user_by_email(session, args.email)
    services.update_user(
        session,
        user.id,
        name=args.name,
        timezone=args.timezone,
        is_superuser=args.superuser,
        is_active=args.active,
    )
    print(f"updated {_describe(user)}")


def _reset_sign_in(session: Session, args: argparse.Namespace) -> None:
    user = services.get_user_by_email(session, args.email)
    services.reset_sign_in(session, user.id)
    print(f"{user.email} will be re-linked by email at their next sign-in")


def _remove(session: Session, args: argparse.Namespace) -> None:
    user = services.get_user_by_email(session, args.email)
    if not args.yes:
        raise services.ValidationError(
            f"this deletes {user.email} and their memberships; to keep the "
            "record use 'update --no-active' instead, or re-run with --yes"
        )
    services.delete_user(session, user.id)
    print(f"removed {user.email}")


def _grant(session: Session, args: argparse.Namespace) -> None:
    user = services.get_user_by_email(session, args.email)
    enterprise = _enterprise(session, args.enterprise)
    site_ids = (
        [_site(session, enterprise, ref).id for ref in args.site]
        if args.site
        else None
    )
    membership = services.find_membership(session, user.id, enterprise.id)
    if membership is None:
        membership = services.create_membership(
            session, user.id, enterprise.id, args.role, site_ids=site_ids
        )
    else:
        services.update_membership(
            session,
            membership.id,
            role=args.role,
            has_all_sites=True if site_ids is None else None,
            site_ids=site_ids,
        )
    print(f"{user.email}: {_describe_membership(session, membership)}")


def _revoke(session: Session, args: argparse.Namespace) -> None:
    user = services.get_user_by_email(session, args.email)
    enterprise = _enterprise(session, args.enterprise)
    membership = services.find_membership(session, user.id, enterprise.id)
    if membership is None:
        raise NotFoundError(
            f"{user.email} has no membership in {enterprise.name}"
        )
    services.delete_membership(session, membership.id)
    print(f"{user.email}: removed from {enterprise.name}")


def _enterprise(session: Session, ref: str) -> Enterprise:
    """Find an enterprise by id or abbreviation."""
    if ref.isdigit():
        return services.get_enterprise(session, int(ref))
    found = session.scalar(
        select(Enterprise).where(Enterprise.abbreviation == ref)
    )
    if found is None:
        raise NotFoundError(f"no enterprise with abbreviation {ref!r}")
    return found


def _site(session: Session, enterprise: Enterprise, ref: str) -> Site:
    """Find a site of ``enterprise`` by id or abbreviation."""
    if ref.isdigit():
        # Membership validation reports a site from another enterprise.
        return services.get_site(session, int(ref))
    found = session.scalar(
        select(Site).where(
            Site.enterprise_id == enterprise.id, Site.abbreviation == ref
        )
    )
    if found is None:
        raise NotFoundError(
            f"no site with abbreviation {ref!r} in {enterprise.name}"
        )
    return found


def _describe(user: User) -> str:
    flags = []
    if user.is_superuser:
        flags.append("superuser")
    if not user.is_active:
        flags.append("INACTIVE")
    if user.auth_subject is None:
        flags.append("not signed in yet")
    name = f" ({user.name})" if user.name else ""
    extra = f" [{', '.join(flags)}]" if flags else ""
    return f"{user.email}{name}{extra}"


def _describe_membership(session: Session, membership) -> str:
    enterprise = session.get(Enterprise, membership.enterprise_id)
    site_ids = services.membership_site_ids(membership)
    if site_ids is None:
        sites = "all sites"
    elif not site_ids:
        sites = "no sites"
    else:
        sites = ", ".join(
            session.get(Site, site_id).abbreviation for site_id in site_ids
        )
    return f"{membership.role} at {enterprise.name} ({sites})"


def _print_user(session: Session, user: User) -> None:
    zone = f"  timezone {user.timezone}" if user.timezone else ""
    print(f"{_describe(user)}{zone}")
    for membership in services.list_memberships(session, user_id=user.id):
        print(f"  {_describe_membership(session, membership)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brewerypi-users",
        description="Manage who may sign in to Brewery Pi.",
    )
    commands = parser.add_subparsers(required=True, metavar="command")

    add = commands.add_parser("add", help="add a person to the user list")
    add.add_argument("email")
    add.add_argument("--name")
    add.add_argument("--timezone", help="IANA zone, e.g. America/Denver")
    add.add_argument(
        "--superuser",
        action="store_true",
        help="server-wide access, including creating enterprises",
    )
    add.set_defaults(command=_add)

    lst = commands.add_parser("list", help="list users and their access")
    lst.set_defaults(command=_list)

    show = commands.add_parser("show", help="show one user's access")
    show.add_argument("email")
    show.set_defaults(command=_show)

    update = commands.add_parser("update", help="change a user")
    update.add_argument("email")
    update.add_argument("--name", help="blank clears it")
    update.add_argument(
        "--timezone", help="IANA zone; blank means use each site's"
    )
    update.add_argument(
        "--superuser", action=argparse.BooleanOptionalAction, default=None
    )
    update.add_argument(
        "--active",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--no-active blocks sign-in but keeps the record",
    )
    update.set_defaults(command=_update)

    reset = commands.add_parser(
        "reset-sign-in",
        help="re-link at next sign-in (account recreated, provider changed)",
    )
    reset.add_argument("email")
    reset.set_defaults(command=_reset_sign_in)

    remove = commands.add_parser("remove", help="delete a user")
    remove.add_argument("email")
    remove.add_argument("--yes", action="store_true")
    remove.set_defaults(command=_remove)

    grant = commands.add_parser(
        "grant", help="set a user's role in an enterprise"
    )
    grant.add_argument("email")
    grant.add_argument("enterprise", help="id or abbreviation")
    grant.add_argument("--role", required=True, choices=MEMBERSHIP_ROLES)
    grant.add_argument(
        "--site",
        action="append",
        default=[],
        help="restrict to this site (id or abbreviation); repeatable",
    )
    grant.set_defaults(command=_grant)

    revoke = commands.add_parser(
        "revoke", help="remove a user from an enterprise"
    )
    revoke.add_argument("email")
    revoke.add_argument("enterprise", help="id or abbreviation")
    revoke.set_defaults(command=_revoke)

    return parser


if __name__ == "__main__":
    sys.exit(main())
