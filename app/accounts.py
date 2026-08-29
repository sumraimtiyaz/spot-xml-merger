"""
The seam where accounts and billing plug in later.

Today the service is anonymous: nobody signs in, nothing is stored, and every
request is served identically. That is deliberate - the first job of this site
is to find out whether anyone wants the tool, and a sign-up form is the fastest
way to never find out.

When that question is answered, this module is where it changes. Everything
else in the app already asks `current_actor()` who it is talking to and
`can(actor, capability)` what they may do, so adding accounts should not touch
the merge path at all.

The intended shape:

    * `current_actor()` reads a session cookie or bearer token, looks the user
      up, and returns an `Actor` with `kind="user"`.
    * `Capability.HISTORY` and friends become real once FEATURE_HISTORY is on.
    * Storage arrives as a separate module. Note that saved arrivals are
      personal data under GDPR - a stored-history product is a materially
      different compliance proposition from this one, and the landing page
      copy about "nothing is stored" must change with it.
"""

from dataclasses import dataclass, field
from typing import Optional, Set

from flask import current_app


class Capability:
    MERGE = "merge"            # available to everyone today
    HISTORY = "history"        # needs accounts + storage
    REMINDERS = "reminders"    # needs accounts + a scheduler
    MULTI_PROPERTY = "multi"   # needs accounts
    API_KEY = "api"            # needs accounts + billing


ANONYMOUS_CAPABILITIES = {Capability.MERGE}


@dataclass(frozen=True)
class Actor:
    kind: str = "anonymous"
    identifier: Optional[str] = None
    capabilities: Set[str] = field(default_factory=lambda: set(ANONYMOUS_CAPABILITIES))

    @property
    def is_anonymous(self) -> bool:
        return self.kind == "anonymous"


def current_actor() -> Actor:
    """Who is making this request. Anonymous until accounts exist."""
    if not current_app.config.get("FEATURE_ACCOUNTS"):
        return Actor()
    # When accounts land: resolve the session/token here and return a user
    # Actor with the capabilities their plan grants.
    return Actor()


def can(actor: Actor, capability: str) -> bool:
    return capability in actor.capabilities
