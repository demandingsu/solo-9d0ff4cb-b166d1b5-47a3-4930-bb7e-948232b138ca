"""Process-local cache of effective rule graphs for conversion queries.

A conversion query for an item on a shipping day needs the graph of *all* rules
effective on that day, even though it only asks about one unit pair. Building
that graph means reading every active rule row and constructing the adjacency
structure. Publishers re-computing a monthly shipping report issue many unit
pairs (件/盒/套 ...) for the same item on the same day, so the graph is cached
once per ``(item_code, shipping_date)`` and shared by every unit-pair query.

Design points
-------------

* **Cache key** is ``(item_code, shipping_date)``; the cached value is the
  adjacency graph plus the sorted rule codes it was built from. On a hit the
  caller performs no rule-row read and no graph construction.
* **Per-key single-flight.** Concurrent first-time queries for the same key
  build exactly one graph together; different keys have independent locks.
* **No blocking on slow builds.** The registry lock only guards in-memory map
  mutations and is never held during the database read / graph construction,
  so a build in progress for one date neither blocks cache hits for other
  dates nor rule submissions (invalidation itself is a pure in-memory step).
* **Generation counters make late builds harmless.** Every key carries a
  generation. An accepted batch bumps the generation (and drops the entry) for
  same-item keys whose day lies inside any new rule's validity window, both
  boundary days included. A builder that finishes after such an invalidation
  notices the generation moved, discards its stale graph instead of
  overwriting the newer entry, and rebuilds.
* **Nothing is persisted.** The cache is pure process memory; SQLite stays the
  single source of truth. A newly started (or another) process rebuilds from
  committed rules, so accepted rules take effect without any cross-process
  cache protocol, including for unit pairs that previously had no route.
* Rejected batches never invalidate anything: their rules were never
  committed, so the original graphs stay valid and keep being reused.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Tuple

# (item_code, shipping_date)
CacheKey = Tuple[str, date]

# A builder reads the rules effective on the key's day and returns the
# adjacency graph together with the sorted codes of the rules it contains.
GraphBuilder = Callable[[], Tuple[Dict[str, List[Tuple[str, Tuple[int, int], str]]],
                                  Tuple[str, ...]]]

# Safety bound for rebuild attempts: a builder can only go stale when an
# accepted batch invalidates its key while it is running, so in practice the
# loop runs at most twice; the cap just guarantees termination under abuse.
_MAX_BUILD_ATTEMPTS = 100


@dataclass(frozen=True)
class GraphEntry:
    """A cached effective-rule graph for one item/day."""

    adj: Dict[str, List[Tuple[str, Tuple[int, int], str]]]
    rule_codes: Tuple[str, ...]
    generation: int


class RuleGraphCache:
    """Thread-safe, process-local rule-graph cache."""

    def __init__(self) -> None:
        # Guards every map/counter below. Only ever held for short, allocation
        # free in-memory operations — never across DB I/O or graph builds.
        self._registry = threading.RLock()
        self._entries: Dict[CacheKey, GraphEntry] = {}
        # Generation exists independently of entries so that a key whose first
        # graph is still being built can already be invalidated.
        self._generations: Dict[CacheKey, int] = {}
        # Per-key single-flight locks (created on demand).
        self._key_locks: Dict[CacheKey, threading.Lock] = {}
        self._hits = 0
        self._misses = 0
        self._builds = 0
        self._stale_discarded = 0
        self._invalidations = 0

    def _lock_for(self, key: CacheKey) -> threading.Lock:
        with self._registry:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _fresh_entry_locked(self, key: CacheKey) -> Tuple[bool, GraphEntry | None, int]:
        """Return (hit, entry, current generation); caller holds registry."""
        generation = self._generations.get(key, 0)
        entry = self._entries.get(key)
        if entry is not None and entry.generation == generation:
            return True, entry, generation
        return False, None, generation

    def _register_key_locked(self, key: CacheKey) -> None:
        """Ensure a key has a generation entry before its first build starts.

        Without this, an invalidation arriving while the very first graph is
        still being built would find nothing to bump: the late builder could
        then store a graph based on the old rules and overwrite what the
        newer rules require.
        """
        if key not in self._generations:
            self._generations[key] = 0

    def get_or_build(self, key: CacheKey, builder: GraphBuilder) -> GraphEntry:
        """Return the graph for ``key``, building it once on a miss.

        Same-key concurrent calls collapse into a single build (single-flight);
        a graph finished after an invalidation is discarded and rebuilt so a
        late old graph can never overwrite a newer one.
        """
        with self._registry:
            hit, entry, _gen = self._fresh_entry_locked(key)
            if hit:
                self._hits += 1
                return entry  # type: ignore[return-value]
            self._misses += 1

        key_lock = self._lock_for(key)
        with key_lock:
            # Double-check: another same-key request may have built it while
            # we were waiting for the per-key lock.
            with self._registry:
                hit, entry, _gen = self._fresh_entry_locked(key)
                if hit:
                    self._hits += 1
                    return entry  # type: ignore[return-value]
                # Register the generation up front so an invalidation during
                # this build can bump it even though no entry exists yet.
                self._register_key_locked(key)

            for _attempt in range(_MAX_BUILD_ATTEMPTS):
                # Capture the generation before the slow build; no cache lock
                # is held while the builder reads SQLite and constructs the
                # graph, so other keys and submissions keep moving.
                with self._registry:
                    generation = self._generations.get(key, 0)
                adj, rule_codes = builder()
                with self._registry:
                    if self._generations.get(key, 0) != generation:
                        # The key was invalidated while we were building:
                        # dropping this graph is mandatory — storing it would
                        # overwrite the entry belonging to the new rules.
                        self._stale_discarded += 1
                        continue
                    entry = GraphEntry(
                        adj=adj, rule_codes=rule_codes, generation=generation
                    )
                    self._entries[key] = entry
                    self._builds += 1
                    return entry

        # Defensive fallback (unreachable in normal operation): serve a fresh
        # graph without caching rather than failing the query. Reference the
        # generation captured by the last loop iteration before this block.
        adj, rule_codes = builder()
        return GraphEntry(
            adj=adj, rule_codes=rule_codes,
            generation=self._generations.get(key, 0),
        )

    def invalidate(
        self,
        item_code: str,
        windows: List[Tuple[date, date]],
    ) -> int:
        """Drop cached graphs of ``item_code`` for days inside any window.

        ``windows`` are ``(start_date, end_date)`` pairs and both boundary days
        count. Keys of other items, and same-item days outside every window,
        are left untouched. Returns the number of invalidated keys.
        """
        if not windows:
            return 0
        affected = 0
        with self._registry:
            # A key can have a generation (build in flight) without an entry.
            keys = set(self._entries) | set(self._generations)
            for key in keys:
                cached_item, cached_day = key
                if cached_item != item_code:
                    continue
                if any(start <= cached_day <= end for start, end in windows):
                    self._generations[key] = self._generations.get(key, 0) + 1
                    self._entries.pop(key, None)
                    affected += 1
                    self._invalidations += 1
        return affected

    def stats(self) -> Dict[str, int]:
        """Snapshot of cache counters (used by /health and tests)."""
        with self._registry:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "builds": self._builds,
                "stale_discarded": self._stale_discarded,
                "invalidations": self._invalidations,
                "entries": len(self._entries),
                "keys": len(self._generations),
            }

    def reset_stats(self) -> None:
        with self._registry:
            self._hits = self._misses = self._builds = 0
            self._stale_discarded = self._invalidations = 0

    def clear(self) -> None:
        """Empty the cache (tests and simulated process restarts)."""
        with self._registry:
            self._entries.clear()
            self._generations.clear()
            self._key_locks.clear()
            self.reset_stats()


# Module-level singleton shared by every request handled in this process.
graph_cache = RuleGraphCache()
