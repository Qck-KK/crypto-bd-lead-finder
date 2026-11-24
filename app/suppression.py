"""Which people buyer feedback keeps out of later deliveries.

A "left the company" or "wrong person" mark is one report. A person is suppressed only while reports from at
least SUPPRESS_MIN_REPORTS different orders are younger than SUPPRESS_TTL_DAYS: one buyer's mistaken click does
not remove a person for everyone, and people who change jobs back or are fixed in the provider's data come back.

Reports carry one-way hashes, never names (app.router.person_hash and person_full_hash). A report on a full name
counts only for that full name; a report on a masked name ("Wei Z***g") counts for every name with the same
coarse key, because that is all that is known about it.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

SUPPRESS_MIN_REPORTS = 2   # distinct orders (feedback refs) that must report a person
SUPPRESS_TTL_DAYS = 180    # reports older than this no longer count


@dataclass(frozen=True)
class Report:
    ref: str                  # the order (feedback ref) the report came from
    hash: str                 # person_hash: coarse, always set
    full_hash: Optional[str]  # person_full_hash: None when the delivered name was masked (or the row predates it)


class Suppression:
    """Answers `matches(hash, full_hash)` for a candidate lead. Build it from the reports that are still
    within the TTL; `always` holds coarse hashes that are suppressed outright (tools and tests)."""

    def __init__(self, reports: Iterable[Report] = (), min_reports: int = SUPPRESS_MIN_REPORTS,
                 always: Iterable[str] = ()):
        self.min_reports = max(1, int(min_reports))
        self.always = frozenset(always)
        self._by_hash: dict[str, list[Report]] = defaultdict(list)
        for r in reports:
            self._by_hash[r.hash].append(r)

    @classmethod
    def from_hashes(cls, hashes: Iterable[str]) -> "Suppression":
        return cls(always=hashes)

    def matches(self, hash_: str, full_hash: Optional[str] = None) -> bool:
        if hash_ in self.always:
            return True
        refs = {r.ref for r in self._by_hash.get(hash_, ())
                if full_hash is None or r.full_hash is None or r.full_hash == full_hash}
        return len(refs) >= self.min_reports

    def active_count(self) -> int:
        """How many reported identities are suppressed now (admin summary)."""
        identities = {(r.hash, r.full_hash) for reports in self._by_hash.values() for r in reports}
        active = {f or h for h, f in identities if self.matches(h, f)}
        return len(active | self.always)

    def __bool__(self) -> bool:
        return bool(self.always or self._by_hash)
