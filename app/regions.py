"""
Region profiles.

Italy has no single tourism-statistics format: every region runs its own
portal and its own file layout. Puglia (DMS/SPOT) is the only one implemented,
because it is the only one we have official schema documentation and real
sample files for.

Adding a region means adding a RegionProfile here and a merge adapter that
satisfies `merge(uploads) -> result dict`. Nothing else in the service knows
about Puglia specifically, so the UI, the API and the tests come along for
free. Do not add a region on guesswork - get the official XSD first.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .engine import merge_istat


@dataclass(frozen=True)
class RegionProfile:
    key: str
    name: str
    portal: str
    portal_url: str
    schema_name: str
    deadline: str
    merge: Callable
    notes: List[str] = field(default_factory=list)
    enabled: bool = True


def _merge_puglia(uploads, prefix_mode="auto", listing_state=None):
    return merge_istat.merge_uploads(uploads, prefix_mode=prefix_mode,
                                     listing_state=listing_state)


PUGLIA = RegionProfile(
    key="puglia",
    name="Puglia",
    portal="DMS Puglia / SPOT",
    portal_url="https://www.dms.puglia.it/portal/spot",
    schema_name="movimentogiornaliero-0.6.xsd",
    deadline="the 10th of the following month",
    merge=_merge_puglia,
    notes=[
        "One <movimento> block per day, children in schema order.",
        "Counters summed; arrivals and departures kept as separate records.",
        "MP beats NM beats EC when listings disagree.",
        "Guest codes made unique per listing so SPOT cannot conflate people.",
    ],
)

# Placeholders so the UI can show what is not covered yet without pretending.
PLANNED = [
    ("lazio", "Lazio", "Radar"),
    ("umbria", "Umbria", "Turismatica"),
    ("ross1000", "Ross1000 regions", "Ross1000"),
]

REGIONS = {PUGLIA.key: PUGLIA}
DEFAULT_REGION = PUGLIA.key


def get_region(key: Optional[str]) -> RegionProfile:
    return REGIONS.get((key or DEFAULT_REGION).lower(), PUGLIA)


def enabled_regions() -> List[RegionProfile]:
    return [r for r in REGIONS.values() if r.enabled]
