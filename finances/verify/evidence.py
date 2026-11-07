"""
Re-checking an evidence report someone else produced.

Three levels, each stronger than the last:

1. the report is intact: its content digest matches its content;
2. the inputs are the ones it describes: each file found hashes to its digest;
3. the result reproduces: running the engine again on those files, with the
   recorded options and reviewer decisions, gives the same content digest.

Level 3 is the one that means something to an auditor. It needs the same
engine version; a different one is reported rather than treated as a failure.
"""

from __future__ import annotations

import json
import os

from finances.verify.model import digest

_NOT_COVERED = ("run", "notice", "content_digest")


def _find(name: str, root: str) -> str | None:
    for dp, _, files in os.walk(root):
        if name in files:
            return os.path.join(dp, name)
    return None


def check_report(path: str, inputs_dir: str | None = None, rerun: bool = False) -> dict:
    """Returns {"ok": bool, "results": [(status, message), ...]} where status is
    "ok", "fail" or "skip"."""
    from finances.verify.engine import VerificationEngine, _version, file_sha256

    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    pred = doc.get("predicate") or {}
    results = []

    body = {k: v for k, v in pred.items() if k not in _NOT_COVERED}
    stated = (pred.get("content_digest") or {}).get("sha256")
    subject = ((doc.get("subject") or [{}])[0].get("digest") or {}).get("sha256")
    actual = digest(body)
    if stated == actual == subject:
        results.append(("ok", f"content digest {actual[:16]}… matches the report"))
    else:
        results.append(("fail", "the report was changed after it was written: content "
                                f"digest is {actual[:16]}…, the report states {str(stated)[:16]}…"))

    found = {}
    if inputs_dir:
        for inp in pred.get("inputs", []):
            name, want = inp["name"], inp["digest"]["sha256"]
            if inp.get("format") == "rows":
                results.append(("skip", f"{name}: rows handed over in code, no file to hash"))
                continue
            p = _find(name, inputs_dir)
            if not p:
                results.append(("skip", f"{name}: not found under {inputs_dir}"))
                continue
            got = file_sha256(p)
            if got == want:
                found[inp["id"]] = p
                results.append(("ok", f"{name}: sha256 matches"))
            else:
                results.append(("fail", f"{name}: sha256 is {got[:16]}…, the report "
                                        f"recorded {want[:16]}…"))

    if rerun:
        inputs = pred.get("inputs", [])
        if not inputs_dir or len(found) != len(inputs):
            results.append(("skip", "re-run needs every input file (pass --inputs)"))
        else:
            o = pred.get("options", {})
            eng = VerificationEngine(
                currency=o.get("currency", "USD"), window_days=o.get("window_days", 3),
                group_window_days=o.get("group_window_days", 7),
                group_size=o.get("group_size", 6),
                include_records="records" in pred)
            eng.opts.tolerance_minor = o.get("tolerance_minor", 0)
            for inp in inputs:
                opts = dict(inp.get("options") or {})
                adapter = inp["adapter"]["name"]
                if adapter == "export":
                    opts["fmt"] = inp["format"]
                eng.add(found[inp["id"]], role=inp["role"], adapter=adapter, **opts)
            for m in pred.get("matches", []):
                ov = m.get("override")
                if ov:
                    eng.decide(m["id"], ov["to"], ov.get("by", ""), ov.get("note", ""),
                               ov.get("at"))
            again = eng.run().content_digest
            version = (pred.get("engine") or {}).get("version")
            if again == actual:
                results.append(("ok", "re-running the engine on these inputs reproduces "
                                      "the report exactly"))
            elif version != _version():
                results.append(("skip", f"re-run differs, but the report was made by "
                                        f"version {version} and this is {_version()}"))
            else:
                results.append(("fail", "re-running the engine on these inputs gives a "
                                        "different result"))
    return {"ok": all(s != "fail" for s, _ in results), "results": results,
            "summary": pred.get("summary", {})}
