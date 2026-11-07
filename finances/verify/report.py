"""
The evidence report: what was checked, on what, by which version, with what result.

The shape borrows from standards rather than inventing one:

- the envelope is an in-toto Statement (``_type``, ``subject`` with a sha256
  digest, ``predicateType``), so it can be signed with DSSE later;
- inputs are listed with their digests the way SLSA lists resolved
  dependencies, and the engine as the builder with its version;
- ``provenance`` uses W3C PROV's entities, activities and agents;
- ``to_sarif()`` maps checks and findings onto SARIF 2.1.0 results.

``content_digest`` covers everything except wall-clock times and local paths:
the same inputs, engine version and options give the same digest on any
machine, which is what ``finances evidence check --rerun`` relies on.

A digest proves the retained evidence has not changed since the run. It does
not prove a statement is genuine; a fabricated file hashes as well as a real one.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from finances.verify import money
from finances.verify.model import (AMBIGUOUS, INCOMPLETE, MATCHED, MISMATCH, NEEDS_REVIEW,
                                   STATUSES, UNRESOLVED, UNVERIFIABLE, VERIFIED, digest)

SCHEMA_VERSION = "1.0.0"
PREDICATE_TYPE = "https://github.com/roshanlam/finances/evidence-report/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
NOTICE = ("Input digests show these files have not changed since the run. They do not "
          "show the files are genuine.")

SARIF_KIND = {VERIFIED: "pass", MISMATCH: "fail", UNVERIFIABLE: "open",
              NEEDS_REVIEW: "review", INCOMPLETE: "open"}


class Report:
    def __init__(self, *, engine: dict, options: dict, inputs, records, checks, matches,
                 findings, started: str, finished: str, include_records: bool = True):
        self.engine, self.options = engine, options
        self.inputs, self.records = inputs, records
        self.checks, self.matches, self.findings = checks, matches, findings
        self.started, self.finished = started, finished
        self.include_records = include_records

    # -- reading it ----------------------------------------------------------
    @property
    def summary(self) -> dict:
        checks = Counter(c.status for c in self.checks)
        pairs = defaultdict(Counter)
        for m in self.matches:
            pairs[m.pair][m.decision] += 1
        for f in self.findings:
            if f.kind.startswith("missing_in_"):
                pairs[f.related[0] if f.related else "?"]["unmatched"] += len(f.records)
        return {
            "records": len(self.records),
            "duplicates": sum(1 for r in self.records if r.duplicate_of),
            "checks": {s: checks.get(s, 0) for s in STATUSES},
            "matching": {p: dict(sorted(c.items())) for p, c in sorted(pairs.items())},
            "findings": dict(Counter(f.severity for f in self.findings)),
        }

    @property
    def clean(self) -> bool:
        """Nothing contradicted, nothing waiting on a person, nothing left over."""
        return (not any(c.status in (MISMATCH, NEEDS_REVIEW) for c in self.checks)
                and not any(m.decision in (NEEDS_REVIEW, AMBIGUOUS, UNRESOLVED)
                            for m in self.matches)
                and not any(f.severity != "info" for f in self.findings))

    def needs_review(self) -> list:
        return [m for m in self.matches if m.decision in (NEEDS_REVIEW, AMBIGUOUS, UNRESOLVED)]

    def record(self, rid: str):
        return self._by_id().get(rid)

    def _by_id(self):
        if not hasattr(self, "_index"):
            self._index = {r.id: r for r in self.records}
        return self._index

    # -- writing it ----------------------------------------------------------
    def body(self) -> dict:
        """Everything the content digest covers."""
        return {
            "schema_version": SCHEMA_VERSION,
            "engine": self.engine,
            "options": self.options,
            "conventions": {"sign": "holder: money in positive, money out negative",
                            "amounts": "decimal strings at the ISO 4217 minor unit",
                            "rounding": "ROUND_HALF_EVEN"},
            "inputs": [i.to_dict() for i in self.inputs],
            "checks": [c.to_dict() for c in self.checks],
            "matches": [m.to_dict() for m in self.matches],
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
            "provenance": self.provenance(),
            **({"records": [r.to_dict() for r in self.records]}
               if self.include_records else {}),
        }

    @property
    def content_digest(self) -> str:
        return digest(self.body())

    def to_dict(self) -> dict:
        body = self.body()
        content = digest(body)
        return {
            "_type": STATEMENT_TYPE,
            "subject": [{"name": "evidence-report", "digest": {"sha256": content}}],
            "predicateType": PREDICATE_TYPE,
            "predicate": {**body,
                          "run": {"started": self.started, "finished": self.finished},
                          "notice": NOTICE,
                          "content_digest": {"sha256": content}},
        }

    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return path

    def provenance(self) -> dict:
        """W3C PROV, minimally: who did what to which inputs, producing what."""
        entities, activities, agents, edges = {}, {}, {}, []
        agents["engine"] = {"type": "softwareAgent", **self.engine}
        for i in self.inputs:
            entities[f"input:{i.id}"] = {"name": i.name, "digest": {"sha256": i.sha256}}
            entities[f"records:{i.id}"] = {"count": i.records}
            agents.setdefault(f"adapter:{i.adapter}",
                              {"type": "softwareAgent", "version": i.adapter_version})
            act = f"read:{i.id}"
            activities[act] = {"type": "read"}
            edges += [{"type": "used", "activity": act, "entity": f"input:{i.id}"},
                      {"type": "wasGeneratedBy", "entity": f"records:{i.id}", "activity": act},
                      {"type": "wasAssociatedWith", "activity": act,
                       "agent": f"adapter:{i.adapter}"},
                      {"type": "wasDerivedFrom", "generated": f"records:{i.id}",
                       "used": f"input:{i.id}"}]
        activities["check"] = {"type": "verify", "checks": len(self.checks)}
        entities["report"] = {"type": "evidence-report"}
        for i in self.inputs:
            edges.append({"type": "used", "activity": "check", "entity": f"records:{i.id}"})
        for pair in sorted({m.pair for m in self.matches}):
            act = f"match:{pair}"
            activities[act] = {"type": "match",
                               "matches": sum(1 for m in self.matches if m.pair == pair)}
            for role in pair.split("~"):
                for i in self.inputs:
                    if i.role == role:
                        edges.append({"type": "used", "activity": act,
                                      "entity": f"records:{i.id}"})
            edges.append({"type": "wasInformedBy", "informed": "report", "informant": act})
        for m in self.matches:
            if m.override:
                who = f"person:{m.override.get('by') or 'unknown'}"
                agents.setdefault(who, {"type": "person"})
                act = f"review:{m.id}"
                activities[act] = {"type": "review", "at": m.override.get("at", "")}
                edges.append({"type": "wasAssociatedWith", "activity": act, "agent": who})
        for act in activities:
            edges.append({"type": "wasAssociatedWith", "activity": act, "agent": "engine"}
                         if not act.startswith(("read:", "review:")) else None)
        return {"entities": entities, "activities": activities, "agents": agents,
                "edges": [e for e in edges if e]}

    def to_sarif(self) -> dict:
        """Checks and findings as SARIF 2.1.0 results, for tools that already read it."""
        names = {i.id: i.name for i in self.inputs}
        results = []
        for c in self.checks:
            results.append({
                "ruleId": c.rule, "kind": SARIF_KIND[c.status],
                "level": "error" if c.status == MISMATCH else "none",
                "message": {"text": c.message},
                "locations": [_sarif_location(names.get(e, e), None) for e in c.evidence],
                "properties": {"status": c.status, "subject": c.subject,
                               "expected": money.text(c.expected),
                               "computed": money.text(c.computed)}})
        for f in self.findings:
            locs = []
            for rid in f.records[:20]:
                r = self.record(rid)
                if r and r.source:
                    locs.append(_sarif_location(names.get(r.source.input_id, ""),
                                                r.source.locator))
            results.append({
                "ruleId": f"finding.{f.kind}",
                "kind": "informational" if f.severity == "info" else "review",
                "level": "none",
                "message": {"text": f.message}, "locations": locs,
                "properties": {"severity": f.severity, "amount": money.text(f.amount)}})
        return {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "finances verify",
                                          "version": self.engine.get("version", "")}},
                      "artifacts": [{"location": {"uri": i.name},
                                     "hashes": {"sha-256": i.sha256}} for i in self.inputs],
                      "results": results}],
        }

    # -- for people ----------------------------------------------------------
    def text(self, limit: int = 12) -> str:
        names = {i.id: i.name for i in self.inputs}
        out = []
        s = self.summary
        out.append(f"{len(self.inputs)} input(s), {s['records']} records"
                   + (f" ({s['duplicates']} repeated across overlapping files)"
                      if s["duplicates"] else ""))
        out.append("")
        out.append("Checks")
        for c in self.checks:
            mark = {VERIFIED: "ok", MISMATCH: "XX", UNVERIFIABLE: "--",
                    NEEDS_REVIEW: "??", INCOMPLETE: ".."}[c.status]
            where = c.subject.get("account") or ", ".join(names.get(e, e) for e in c.evidence)
            out.append(f"  [{mark}] {c.rule:<28} {where}")
            out.append(f"       {c.message}")
        if s["matching"]:
            out.append("")
            out.append("Matching")
            for pair, counts in s["matching"].items():
                out.append(f"  {pair:<20} " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        review = self.needs_review()
        if review:
            out.append("")
            out.append(f"Needs a decision ({len(review)})")
            for m in review[:limit]:
                out.append(f"  {m.id}  {m.pair}  {m.decision} ({m.tier})")
                for rid in (*m.left, *m.right):
                    r = self.record(rid)
                    if r:
                        out.append(f"      {r.role:<9} {r.date}  {money.show(r.amount, r.currency):>14}"
                                   f"  {r.description[:44]}")
                for why in m.reasons:
                    out.append(f"      - {why}")
                if m.explanation:
                    out.append(f"      > {m.explanation}")
        real = [f for f in self.findings if f.severity != "info"]
        if real:
            out.append("")
            out.append(f"Findings ({len(real)})")
            for f in real[:limit]:
                out.append(f"  [{f.severity}] {f.message}")
            if len(real) > limit:
                out.append(f"  … and {len(real) - limit} more in the JSON report")
        out.append("")
        out.append("Clean: nothing contradicted, nothing waiting on review."
                   if self.clean else
                   "Not clean: see the mismatches, decisions and findings above.")
        return "\n".join(out)


def _sarif_location(uri: str, locator: dict | None) -> dict:
    loc = {"physicalLocation": {"artifactLocation": {"uri": uri}}}
    if locator:
        region = {}
        if "record" in locator:
            region["startLine"] = locator["record"]
        if "line" in locator:
            region["snippet"] = {"text": locator["line"]}
        if region:
            loc["physicalLocation"]["region"] = region
        loc["properties"] = {k: v for k, v in locator.items() if k not in ("line",)}
    return loc
