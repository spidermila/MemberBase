import click
from flask import Flask

from memberbase import people


def init_app(app: Flask) -> None:
    @app.cli.command("expire-grants")
    def expire_grants() -> None:
        """Revoke visibility grants past their expiry."""
        click.echo(f"expired grants: {people.expire_grants()}")

    @app.cli.command("repair-members")
    def repair_members() -> None:
        """Rebuild members groups, strip archived people of roles, add missing
        cn=chair and ou=requests."""
        click.echo(f"entries repaired: {people.repair_members()}")
