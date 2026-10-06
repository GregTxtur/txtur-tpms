"""Thin layer between the pages and whatever auth/ currently is.

auth/ has been swapped once already (shared password -> the Microsoft 365 gate) and the two
versions describe the signed-in person differently: the old one as a plain name, the new one
as a dict with email, name and roles. Pages use these two helpers so they work with either.
"""
from auth import current_user, login_required


@login_required
def _signed_in():
    return None


def require_login():
    """For Blueprint.before_request: returns auth's own redirect when nobody is signed in."""
    return _signed_in()


def user_label():
    """Who to record against an entry: the email when auth knows it, otherwise the name given."""
    user = current_user()
    if not user:
        return ""
    if isinstance(user, dict):
        return user.get("email") or user.get("name") or ""
    return str(user)
