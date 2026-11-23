"""Deterministic stop/route name matching with an explicit audit trail.

Audit labels
------------
exact       raw text is byte-for-byte equal to a GTFS name after strip()
verified    deterministic documented normalisation / curated alias, unique target
ambiguous   several equally plausible GTFS targets, or a low-confidence fuzzy hit
unmatched   no plausible GTFS target

`verified` is rule-based verification (including a manually inspected alias
table), not per-row human review of 2.5 million tickets.  The `method` field
records which rule fired.
"""

from __future__ import annotations

import difflib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_PUNCT = re.compile(r"[^A-Z0-9/]+")
_SPLIT = re.compile(r"\s*/\s*")
_TOKEN_EDGE = re.compile(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])")

_TOKEN_ALIASES = {
    "FAMLIY": "FAMILY",
    "FAMLIYPLANING": "FAMILYPLANNING",
    "PLANING": "PLANNING",
    "SATION": "STATION",
    "STN": "STATION",
    "DD": "DDA",
    "SEC": "SEC",
    "SECTOR": "SEC",
    "BDR": "BORDER",
    "TERMINAL": "T",
    "XING": "CROSSING",
    "X": "CROSSING",
    "ING": "CROSSING",
    "NH": "NH",
    "OPP": "OPPOSITE",
    "PWD": "PWD",
}

_PHRASE_ALIASES = {
    "D D A": "DDA",
    "DDA FLAT": "DDA FLATS",
    "DDA FLA": "DDA FLATS",
    "X ING": "CROSSING",
    "BUS STAND": "BUS STAND",
    "OLD DELHI RAILWAY STATION T": "OLD DELHI RAILWAY STATION T",
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm(text: str) -> str:
    """Canonical comparison key for a stop name."""
    s = html.unescape(strip_accents(str(text).upper()))
    s = s.replace("&", " / ")
    s = _TOKEN_EDGE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    for phrase, repl in _PHRASE_ALIASES.items():
        s = re.sub(rf"\b{re.escape(phrase)}\b", repl, s)
    tokens = []
    for tok in s.split():
        tokens.append(_TOKEN_ALIASES.get(tok, tok))
    s = " ".join(tokens)
    for _ in range(3):
        prev = s
        s = re.sub(r"\bCROSSING CROSSING\b", "CROSSING", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s == prev:
            break
    return s


def name_parts(text: str) -> list[str]:
    """Alternative names inside a single GTFS/ticket field (split on '/')."""
    out = []
    for part in _SPLIT.split(html.unescape(str(text))):
        key = norm(part)
        if len(key) >= 4:
            out.append(key)
    return out


def norm_route(text: str) -> str:
    s = html.unescape(strip_accents(str(text).upper()))
    return re.sub(r"[^A-Z0-9]", "", s)


def route_direction(text: str) -> str:
    """Preserve meaningful UP / DOWN suffixes even for branches (e.g. 717AUP)."""
    s = norm_route(text)
    m = re.match(r"^(.*?)(UP|DOWN)(\d*)$", s)
    if m:
        base, direction, numeric = m.groups()
        return f"{base}{numeric}:{direction}"
    return f"{s}:UNKNOWN"


@dataclass(frozen=True)
class Match:
    label: str
    method: str
    ids: tuple[str, ...]


class NameMatcher:
    """Indexes GTFS stop names (whole field and '/' parts) and route names."""

    def __init__(self, stops: dict[str, str], routes: Iterable[str], aliases_path: Path | None = None):
        self.stops = stops
        self.route_names = set(routes)
        self.alias_map: dict[str, str] = {}
        if aliases_path and aliases_path.exists():
            with aliases_path.open(encoding="utf-8") as fh:
                self.alias_map = {norm(k): str(v) for k, v in json.load(fh).items()}
        self.raw_index: dict[str, list[str]] = {}
        self.norm_index: dict[str, list[str]] = {}
        self.part_index: dict[str, list[str]] = {}
        for sid, name in stops.items():
            self.raw_index.setdefault(name.strip(), []).append(sid)
            self.norm_index.setdefault(norm(name), []).append(sid)
            for part in set(name_parts(name)):
                self.part_index.setdefault(part, []).append(sid)
        self._norm_names = sorted(self.norm_index)
        self._token_index: dict[str, set[str]] = {}
        for name in self._norm_names:
            for token in set(name.split()):
                if len(token) >= 3:
                    self._token_index.setdefault(token, set()).add(name)
        self._stop_cache: dict[str, Match] = {}
        self._route_cache: dict[str, Match] = {}
        self.route_norm: dict[str, list[str]] = {}
        for name in self.route_names:
            self.route_norm.setdefault(norm_route(name), []).append(name)

    def _fuzzy_candidates(self, key: str) -> list[str]:
        tokens = [t for t in key.split() if len(t) >= 3]
        names: set[str] = set()
        for token in tokens:
            names.update(self._token_index.get(token, ()))
        if not names:
            lo, hi = int(len(key) * 0.65), int(len(key) * 1.45) + 3
            names = {n for n in self._norm_names if lo <= len(n) <= hi}
        return sorted(names) or self._norm_names

    def match_stop(self, raw: str, allowed_ids: Iterable[str] | None = None) -> Match:
        """Resolve one ticket stop name, optionally restricted to a trip's stops."""
        allowed = set(allowed_ids) if allowed_ids is not None else None
        if allowed is None and raw in self._stop_cache:
            return self._stop_cache[raw]
        rid = raw.strip()
        result = None

        def keep(ids: Iterable[str]) -> tuple[str, ...]:
            return tuple(sorted(sid for sid in ids if allowed is None or sid in allowed))

        if rid in self.raw_index:
            hits = keep(self.raw_index[rid])
            if hits:
                result = Match("exact", "raw_name", hits)
        if result is None:
            key = norm(raw)
            if key in self.alias_map:
                target = self.alias_map[key]
                hits = keep(self.raw_index.get(target, []) + self.norm_index.get(norm(target), []))
                if hits:
                    result = Match("verified", "curated_alias", hits)
            if result is None and key in self.norm_index:
                hits = keep(self.norm_index[key])
                if hits:
                    result = Match("verified", "normalised_full", hits)
            if result is None:
                part_hits: list[str] = []
                for part in name_parts(raw):
                    part_hits.extend(self.part_index.get(part, []))
                if part_hits:
                    hits = keep(set(part_hits))
                    if hits:
                        result = Match("verified", "name_part", hits)
            if result is None:
                near = difflib.get_close_matches(key, self._fuzzy_candidates(key), n=3, cutoff=0.88)
                if near:
                    best = difflib.SequenceMatcher(None, key, near[0]).ratio()
                    second = difflib.SequenceMatcher(None, key, near[1]).ratio() if len(near) > 1 else 0.0
                    if best - second >= 0.03:
                        hits = keep(self.norm_index[near[0]])
                    else:
                        hits = tuple(sorted({s for n in near[:2] for s in self.norm_index[n]
                                             if allowed is None or s in allowed}))
                    if hits:
                        result = Match("ambiguous" if best < 0.95 else "verified", f"fuzzy:{best:.3f}", hits)
        if result is None:
            result = Match("unmatched", "none", ())
        if allowed is None:
            self._stop_cache[raw] = result
        return result

    def match_route(self, raw: str) -> Match:
        if raw in self._route_cache:
            return self._route_cache[raw]
        rid = norm_route(raw)
        if not rid:
            result = Match("unmatched", "empty", ())
        elif raw.strip() in self.route_names:
            result = Match("exact", "raw_name", (raw.strip(),))
        elif rid in self.route_norm:
            names = self.route_norm[rid]
            result = Match("verified" if len(names) == 1 else "ambiguous", "normalised_route", tuple(names))
        else:
            near = difflib.get_close_matches(rid, list(self.route_norm), n=2, cutoff=0.92)
            result = Match("ambiguous", "fuzzy_route", tuple(sorted(self.route_norm.get(near[0], ())))) if near else Match("unmatched", "none", ())
        self._route_cache[raw] = result
        return result
