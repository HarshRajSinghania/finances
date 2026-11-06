#!/usr/bin/env python3
"""
llm_enrich.py — use a local LLM to clean up merchant names and categories.

Talks to any OpenAI-compatible local server (Ollama, LM Studio, llama.cpp,
vLLM, text-generation-webui). Nothing leaves your machine.

The input is the shared store or a CSV, and the labels go back where they came
from: run it against finances.db and the improved categories are there for
every other tool, with no export step in between.

    finances enrich                              # finances.db, labels written back
    finances enrich finances.db -o enriched.csv  # and a CSV as well
    finances enrich transactions.csv -o transactions_enriched.csv

    finances enrich --endpoint http://localhost:11434/v1 --model llama3.1:8b
    finances enrich --list-models            # what fits on this machine
    finances enrich --only-uncategorized     # faster
    finances enrich --dry-run                # show the prompt, send nothing

Model choice is automatic and size-aware: it reads how much RAM you have, skips
anything that would not fit in 60% of it, and never auto-picks a model above
--max-params (14B by default). A 120B model is never loaded unless you ask for it
by name with --force. Something in the 3B-8B range labels merchants perfectly well.

Why this is cheap: it asks about *unique merchant strings*, not transactions.
A year of statements is usually 1,000+ rows but only 150–300 distinct merchants,
and answers are cached in merchant_cache.json, so re-runs cost almost nothing.

Standard library only.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from finances.database import DEFAULT_DB, FinanceDB
from finances.engine import load_transactions
from finances.models import Transaction

try:
    from finances.categorizer import CATEGORIES          # one taxonomy for every script
except ImportError:
    CATEGORIES = [
        "Groceries", "Dining", "Coffee", "Delivery", "Alcohol/Bars", "Transport",
        "Gas/Auto", "Travel", "Lodging", "Shopping", "Big Box", "Electronics",
        "Clothing", "Home", "Subscriptions", "Software/Tools", "Gaming",
        "Utilities", "Phone/Internet", "Rent/Housing", "Insurance", "Health",
        "Pharmacy", "Fitness", "Personal Care", "Entertainment", "Education",
        "Pets", "Childcare", "Charity", "Gifts", "Fees/Interest", "Taxes",
        "Debt Payment", "Transfers/Cash", "Payment", "Credits/Refunds",
        "Income", "Uncategorized",
    ]

SYSTEM = (
    "You label bank and credit card merchant strings. You reply with JSON only — "
    "no prose, no markdown fences. You never invent merchants that are not in the input."
)

PROMPT = """Label each merchant string from a card statement.

Return a JSON array. One object per input, same order, with these keys:
  "raw"        the input string, copied exactly
  "name"       the real-world merchant name, cleaned (drop store numbers, city,
               state, phone numbers, reference codes). If unrecognisable, repeat the input.
  "category"   exactly one of: {cats}
  "recurring"  true if this is normally a subscription or a regular bill, else false
  "essential"  true if this is normally a necessity (housing, utilities, groceries,
               medical, transport to work), false for discretionary spending
  "confidence" "high", "medium" or "low"

Rules:
- Use "Uncategorized" and confidence "low" rather than guessing wildly.
- A payment to the card itself is "Payment". Moving money to another account you
  own (Venmo, Zelle, brokerage, ATM) is "Transfers/Cash".
- Interest, late fees and annual fees are "Fees/Interest".

Input:
{items}"""


# --------------------------------------------------------------------------
# Talking to the local server
# --------------------------------------------------------------------------

COMMON_ENDPOINTS = [
    ("http://localhost:11434/v1", "Ollama"),
    ("http://localhost:1234/v1", "LM Studio"),
    ("http://localhost:8080/v1", "llama.cpp server"),
    ("http://localhost:8000/v1", "vLLM"),
    ("http://localhost:5000/v1", "text-generation-webui"),
    ("http://localhost:4891/v1", "GPT4All"),
]

# Rough bytes per parameter once quantised. Local setups are q4 unless stated.
QUANT_BYTES = {"q2": 0.42, "q3": 0.52, "q4": 0.62, "q5": 0.75, "q6": 0.88,
               "q8": 1.10, "f16": 2.10, "fp16": 2.10, "bf16": 2.10, "f32": 4.10}
DEFAULT_QUANT = 0.62
RUNTIME_OVERHEAD_GB = 1.4          # server, KV cache, context at our small prompts


def total_ram_gb():
    """Physical RAM in GB. Unified memory on Apple Silicon counts as RAM."""
    try:
        names = os.sysconf_names
        if "SC_PAGE_SIZE" in names and "SC_PHYS_PAGES" in names:
            v = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if v > 0:
                return v / 1e9
    except (ValueError, OSError, AttributeError):
        pass
    try:
        import subprocess
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip()) / 1e9
    except Exception:
        pass
    try:
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys / 1e9
    except Exception:
        pass
    return None


# Effective memory bandwidth in GB/s. Token generation is bandwidth-bound: the
# model's weights are read once per token, so tokens/sec ≈ bandwidth ÷ model size.
APPLE_BANDWIDTH = [("ultra", 800), ("max", 400), ("pro", 200)]
NVIDIA_BANDWIDTH = [("5090", 1790), ("4090", 1010), ("4080", 717), ("4070", 504),
                    ("4060", 272), ("3090", 936), ("3080", 760), ("3070", 448),
                    ("3060", 360), ("a100", 1555), ("h100", 3350), ("a6000", 768),
                    ("l40", 864), ("v100", 900), ("t4", 320), ("2080", 448),
                    ("2070", 448), ("1080", 320), ("1660", 192)]


def detect_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            name, mem = out.stdout.strip().splitlines()[0].split(",")
            vram = float(re.sub(r"[^\d.]", "", mem)) / 1024.0     # MiB -> GB
            low = name.lower()
            bw = next((b for k, b in NVIDIA_BANDWIDTH if k in low), 400)
            return {"kind": "nvidia", "name": name.strip(), "vram": vram, "bandwidth": bw}
    except Exception:
        pass
    return None


def detect_device():
    import platform
    d = {"os": platform.system(), "arch": platform.machine(),
         "cores": os.cpu_count() or 4, "ram": total_ram_gb(),
         "gpu": None, "chip": None}

    if d["os"] == "Darwin" and d["arch"] == "arm64":
        chip = ""
        try:
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                 capture_output=True, text=True, timeout=3)
            chip = out.stdout.strip()
        except Exception:
            pass
        low = chip.lower()
        bw = next((b for k, b in APPLE_BANDWIDTH if k in low), 110)
        d.update({"backend": "apple", "chip": chip or "Apple Silicon", "bandwidth": bw,
                  "vram": d["ram"]})           # unified memory
    else:
        gpu = detect_gpu()
        if gpu:
            d.update({"backend": "nvidia", "gpu": gpu, "bandwidth": gpu["bandwidth"],
                      "vram": gpu["vram"]})
        else:
            # DDR4 dual channel is ~40 GB/s; more cores usually means more channels.
            d.update({"backend": "cpu",
                      "bandwidth": 35 + min(d["cores"], 16) * 2.5, "vram": 0})
    return d


def estimated_tokens_per_sec(model_gb, device):
    """First-order estimate. The probe replaces this with a measurement."""
    if not model_gb:
        return None
    bw = device["bandwidth"]
    if device["backend"] == "nvidia" and model_gb > device["vram"] * 0.9:
        bw = 35 + min(device["cores"], 16) * 2.5        # spills to CPU, loses the GPU
    return max(bw * 0.72 / model_gb, 0.5)


TOKENS_PER_MERCHANT = 60          # one JSON object of labels


def estimate_minutes(model_gb, device, n_merchants, workers=1):
    tps = estimated_tokens_per_sec(model_gb, device)
    if not tps:
        return None
    parallel = 1.6 if device["backend"] in ("nvidia", "apple") and workers > 1 else 1.0
    return (n_merchants * TOKENS_PER_MERCHANT) / (tps * parallel) / 60.0


def describe_device(d):
    ram = f"{d['ram']:.0f} GB RAM" if d.get("ram") else "RAM unknown"
    if d["backend"] == "apple":
        return f"{d['chip']}, {ram}, ~{d['bandwidth']:.0f} GB/s unified memory"
    if d["backend"] == "nvidia":
        return (f"{d['gpu']['name']} ({d['gpu']['vram']:.0f} GB VRAM), {ram}, "
                f"~{d['bandwidth']:.0f} GB/s")
    return f"CPU only, {d['cores']} cores, {ram}, ~{d['bandwidth']:.0f} GB/s"


def params_from_name(name):
    """Billions of parameters implied by a model name. None if it can't be read."""
    n = name.lower()
    m = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", n)      # mixtral 8x7b
    if m:
        return int(m.group(1)) * float(m.group(2))
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", n)
    if m:
        return float(m.group(1))
    for hint, val in (("mini", 3.8), ("small", 7.0), ("medium", 14.0),
                      ("large", 70.0), ("tiny", 1.1)):
        if hint in n:
            return val
    return None


def quant_bytes_from_name(name):
    n = name.lower()
    for tag, val in QUANT_BYTES.items():
        if re.search(r"\b%s(?:_|-|\b)" % re.escape(tag), n):
            return val
    return DEFAULT_QUANT


def estimate_gb(model):
    """Best estimate of RAM needed to run this model, in GB."""
    if model.get("bytes"):
        return model["bytes"] / 1e9 + RUNTIME_OVERHEAD_GB
    p = model.get("params")
    if not p:
        return None
    return p * model.get("quant", DEFAULT_QUANT) + RUNTIME_OVERHEAD_GB


def http_json(url, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer local"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def discover(endpoint=None):
    """Find a running local server and every model it offers, with sizes."""
    # Inside a container "localhost" is the container itself, so compose points
    # this at the ollama service instead.
    endpoint = endpoint or os.environ.get("FINANCES_LLM_ENDPOINT")
    tries = [(endpoint, "specified")] if endpoint else COMMON_ENDPOINTS
    for url, label in tries:
        base = url.rstrip("/")
        try:
            listing = http_json(base + "/models", timeout=4)
            ids = [m.get("id") for m in listing.get("data", []) if m.get("id")]
        except Exception:
            continue
        if not ids:
            continue

        models = [{"name": i, "params": params_from_name(i),
                   "quant": quant_bytes_from_name(i), "bytes": None} for i in ids]

        # Ollama reports real on-disk size and parameter count — much better
        # than guessing from the name.
        root = re.sub(r"/v1/?$", "", base)
        try:
            tags = http_json(root + "/api/tags", timeout=4)
            info = {}
            for m in tags.get("models", []):
                d = m.get("details") or {}
                ps = d.get("parameter_size", "")
                pm = re.search(r"(\d+(?:\.\d+)?)", str(ps))
                info[m.get("name", "")] = {
                    "bytes": m.get("size"),
                    "params": float(pm.group(1)) if pm else None,
                    "quant_label": d.get("quantization_level", ""),
                }
            for mo in models:
                hit = info.get(mo["name"]) or info.get(mo["name"] + ":latest")
                if hit:
                    mo["bytes"] = hit["bytes"]
                    mo["params"] = hit["params"] or mo["params"]
                    mo["quant_label"] = hit["quant_label"]
        except Exception:
            pass

        return base, models, label
    return None, [], None


def probe_speed(endpoint, model, sample, warm=True):
    """Measure real seconds-per-merchant. Beats any lookup table.

    The first call to a cold model includes load time, so a throwaway call runs
    first — otherwise a fast model looks slow purely because it was on disk.
    """
    if warm:
        try:
            label_batch(endpoint, model, sample[:1], retries=0)
        except Exception:
            pass
    t0 = time.time()
    result, err = label_batch(endpoint, model, sample, retries=0)
    elapsed = time.time() - t0
    if err or not result:
        return None, {}
    return elapsed / max(len(sample), 1), result


def choose_model(models, ram_gb, max_params, prefer, force):
    """Models that physically fit. Speed is decided separately."""
    budget = ram_gb * 0.60 if ram_gb else None
    ok, too_big, unknown = [], [], []
    for m in models:
        gb = estimate_gb(m)
        m["gb"] = gb
        p = m.get("params")
        if gb is None or p is None:
            unknown.append(m)
        elif p > max_params or (budget and gb > budget):
            too_big.append(m)
        else:
            ok.append(m)

    pool = ok or (unknown if force else [])
    if not pool:
        return None, ok, too_big, unknown
    sized = [m for m in pool if m.get("params")]
    if not sized:
        return pool[0], ok, too_big, unknown
    sized.sort(key=lambda m: m["params"], reverse=(prefer == "largest"))
    return sized[0], ok, too_big, unknown


def pick_for_time(g_print, endpoint, candidates, device, n_merchants, target_min,
                  workers, cache, probe=True):
    """Largest model that still finishes inside the time budget.

    Tries the best candidate first, measures it, and steps down a size if the
    measurement says it won't make the deadline.
    """
    speeds = cache.get("__meta__", {}).get("seconds_per_merchant", {})
    ranked = sorted([m for m in candidates if m.get("params")],
                    key=lambda m: -m["params"])
    if not ranked:
        return (candidates[0] if candidates else None), None, []

    notes = []
    for m in ranked:
        est = estimate_minutes(m["gb"], device, n_merchants, workers)
        known = speeds.get(m["name"])
        if known:
            mins = known * n_merchants / 60.0 / (1.6 if workers > 1 and
                                                 device["backend"] != "cpu" else 1.0)
            notes.append((m, mins, "measured earlier"))
            if mins <= target_min:
                return m, mins, notes
            continue
        if est is not None and est > target_min * 3:
            notes.append((m, est, "estimated, far over budget — skipped"))
            continue
        if not probe:
            notes.append((m, est, "estimated"))
            if est is None or est <= target_min:
                return m, est, notes
            continue

        g_print(f"  timing {m['name']} …")
        sec_each, labels = probe_speed(endpoint, m["name"], PROBE_SAMPLE)
        if sec_each is None:
            notes.append((m, None, "did not respond"))
            continue
        cache.setdefault("__meta__", {}).setdefault("seconds_per_merchant", {})[
            m["name"]] = round(sec_each, 3)
        mins = sec_each * n_merchants / 60.0 / (1.6 if workers > 1 and
                                                device["backend"] != "cpu" else 1.0)
        notes.append((m, mins, f"measured {sec_each:.2f}s per merchant"))
        if mins <= target_min:
            return m, mins, notes
        g_print(f"    {mins:.0f} min for {n_merchants} merchants — too slow, "
                f"trying something smaller")

    # Nothing hits the budget. Take the fastest thing available and be honest
    # about what it will cost in time.
    smallest = ranked[-1]
    known = speeds.get(smallest["name"])
    if known:
        mins = known * n_merchants / 60.0
    else:
        mins = estimate_minutes(smallest["gb"], device, n_merchants, workers)
    return smallest, mins, notes


PROBE_SAMPLE = ["STARBUCKS STORE 09112 JERSEY CITY NJ",
                "AMAZON.COM*RT4G92H1 AMZN.COM/BILL WA",
                "SHELL OIL 57444789002 BAYONNE NJ",
                "NETFLIX.COM 866-579-7172 CA",
                "TRADER JOE S #542 NEW YORK NY"]


def describe(m):
    p = f"{m['params']:g}B" if m.get("params") else "size unknown"
    gb = f"~{m['gb']:.1f} GB" if m.get("gb") else "footprint unknown"
    q = f", {m['quant_label']}" if m.get("quant_label") else ""
    return f"{m['name']}  ({p}, {gb}{q})"


def complete(endpoint, model, prompt, temperature=0.0, timeout=240, system=None):
    """Send one completion to the local server.

    ``system`` defaults to the merchant-labelling prompt. It is a parameter
    because the discovery, sizing and transport code here is worth reusing for
    other local-model tasks — ask.py writes SQL with it — and a system prompt
    telling the model to reply with merchant JSON steers it badly anywhere else.
    """
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system or SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    out = http_json(endpoint + "/chat/completions", body, timeout=timeout)
    return out["choices"][0]["message"]["content"]


def extract_json_array(text):
    """Local models like to wrap JSON in prose or fences. Dig it out."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("[")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------

def label_batch(endpoint, model, batch, retries=2):
    prompt = PROMPT.format(cats=", ".join(CATEGORIES),
                           items="\n".join(f"- {m}" for m in batch))
    for attempt in range(retries + 1):
        try:
            raw = complete(endpoint, model, prompt,
                           temperature=0.0 if attempt == 0 else 0.2)
        except Exception as e:
            if attempt == retries:
                return {}, f"request failed: {e}"
            time.sleep(1.5 * (attempt + 1))
            continue
        arr = extract_json_array(raw)
        if not isinstance(arr, list):
            if attempt == retries:
                return {}, "model did not return a JSON array"
            continue
        out = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            key = str(item.get("raw", "")).strip()
            if key not in batch:                      # tolerate light rewording
                cand = [b for b in batch if b.lower() == key.lower()]
                if not cand:
                    continue
                key = cand[0]
            cat = str(item.get("category", "")).strip()
            if cat not in CATEGORIES:
                cat = next((c for c in CATEGORIES if c.lower() == cat.lower()), "")
            out[key] = {
                "name": str(item.get("name") or key).strip()[:60],
                "category": cat,
                "recurring": bool(item.get("recurring")),
                "essential": bool(item.get("essential")),
                "confidence": str(item.get("confidence", "low")).lower(),
            }
        if out:
            return out, None
    return {}, "no usable rows in response"


def no_server_message():
    return ("No local LLM server answered.\n"
            "  Ollama:    ollama serve   (then: ollama pull qwen2.5:7b)\n"
            "  LM Studio: start the local server from the Developer tab\n"
            "  llama.cpp: llama-server -m model.gguf --port 8080\n"
            "Then re-run, or pass --endpoint http://host:port/v1")


def show_models(models, ram, endpoint, label, args):
    print(f"\n{label} at {endpoint}"
          + (f", {ram:.0f} GB RAM detected" if ram else ", RAM unknown"))
    budget = ram * 0.60 if ram else None
    for m in sorted(models, key=lambda m: m.get("params") or 0):
        if m.get("gb") and budget:
            verdict = "fits" if m["gb"] <= budget and \
                (m.get("params") or 0) <= args.max_params else "too big"
        else:
            verdict = "unknown"
        print(f"  [{verdict:>8}]  {describe(m)}")
    if budget:
        print(f"\nBudget: {budget:.1f} GB (60% of RAM), "
              f"parameter ceiling {args.max_params:g}B (--max-params to change)")


def show_device_info(models, ram, n_todo, args):
    dev = detect_device()
    print(f"\n{describe_device(dev)}")
    budget = ram * 0.60 if ram else None
    print(f"\n  {'model':<34}{'size':>8}{'fits':>7}{'est. time':>12}")
    for m in sorted(models, key=lambda m: m.get("params") or 0):
        fits = "yes" if (m.get("gb") and budget and m["gb"] <= budget) else "no"
        est = estimate_minutes(m.get("gb"), dev, n_todo, args.workers)
        t = f"{est:.0f} min" if est else "—"
        sz = f"{m['params']:g}B" if m.get("params") else "?"
        print(f"  {m['name'][:33]:<34}{sz:>8}{fits:>7}{t:>12}")
    print(f"\n  for {n_todo} merchants, target {args.target_minutes:g} min")
    print("  estimates only — the real run measures the model before committing")


def write_store(path, txns):
    with FinanceDB(path) as db:
        db.upsert_transactions(txns)


def write_csv(path, txns, extras):
    """Transaction columns plus the LLM's own opinions, which the schema has no
    room for: whether it thinks the charge recurs, whether it is essential, and
    how sure it was."""
    extra_cols = ["recurring_guess", "essential_guess", "llm_confidence",
                  "category_llm"]
    cols = Transaction.columns() + [c for c in extra_cols
                                    if any(c in e for e in extras)]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t, e in zip(txns, extras):
            row = t.to_row()
            row.update(e)
            w.writerow({c: row.get(c, "") for c in cols})


def main():
    ap = argparse.ArgumentParser(description="Label merchants with a local LLM.")
    ap.add_argument("source", nargs="?", default=DEFAULT_DB,
                    help=f"a .db or a .csv (default {DEFAULT_DB})")
    ap.add_argument("--db", help="the store to read, same as the positional argument")
    ap.add_argument("-o", "--output",
                    help="also write a CSV (the default when the source is a CSV)")
    ap.add_argument("--endpoint", help="e.g. http://localhost:11434/v1 (auto-detected if omitted)")
    ap.add_argument("--model", help="model name (otherwise the largest that fits this machine)")
    ap.add_argument("--max-params", type=float, default=14.0, metavar="B",
                    help="never auto-pick a model larger than this many billion "
                         "parameters (default 14)")
    ap.add_argument("--prefer", choices=["smallest", "largest"], default="largest",
                    help="among models that fit the time budget, prefer the largest "
                         "(better labels) or smallest (faster). Default largest")
    ap.add_argument("--ram-gb", type=float, metavar="N",
                    help="override detected RAM (use when the server runs on another "
                         "machine)")
    ap.add_argument("--target-minutes", type=float, default=5.0, metavar="M",
                    help="how long the whole run may take; the largest model that "
                         "fits this budget is chosen (default 5)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the timing probe and use estimates only")
    ap.add_argument("--device-info", action="store_true",
                    help="show detected hardware and per-model time estimates, then exit")
    ap.add_argument("--list-models", action="store_true",
                    help="show every local model with its estimated footprint and "
                         "whether it fits, then exit")
    ap.add_argument("--force", action="store_true",
                    help="run a model that exceeds the safe memory budget anyway")
    ap.add_argument("--batch", type=int, default=25, help="merchants per request")
    ap.add_argument("--workers", type=int, default=2, help="parallel requests")
    ap.add_argument("--cache", default="merchant_cache.json")
    ap.add_argument("--only-uncategorized", action="store_true",
                    help="only ask about merchants the rules couldn't place")
    ap.add_argument("--min-count", type=int, default=1,
                    help="skip merchants seen fewer than N times")
    ap.add_argument("--dry-run", action="store_true", help="print one prompt and stop")
    ap.add_argument("--keep-rule-category", action="store_true",
                    help="keep the rule-based category, add the LLM's as a second column")
    args = ap.parse_args()

    source = args.db or args.source
    rows = load_transactions(source)
    if not rows:
        sys.exit(f"{source} has no transactions")

    def merchant_of(t):
        return (t.merchant or t.description or "").strip()

    counts = Counter(merchant_of(t) for t in rows)
    targets = [m for m, c in counts.items() if m and c >= args.min_count]
    if args.only_uncategorized:
        uncat = {merchant_of(t) for t in rows
                 if (t.category or "Uncategorized") in ("", "Uncategorized")}
        targets = [m for m in targets if m in uncat]

    cache = {}
    if os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache))
        except json.JSONDecodeError:
            print(f"cache {args.cache} was unreadable, starting fresh")
    todo = [m for m in targets if m not in cache and not m.startswith("__")]

    print(f"{source}: {len(rows):,} transactions, {len(counts)} distinct merchants")
    n_cached = len([k for k in cache if not k.startswith("__")])
    print(f"{len(targets)} in scope, {n_cached} already cached, {len(todo)} to label")

    if args.dry_run:
        print("\n--- prompt that would be sent (nothing was sent) ---\n")
        print(PROMPT.format(cats=", ".join(CATEGORIES),
                            items="\n".join(f"- {m}" for m in todo[:args.batch])))
        return

    # Asking what this machine can run is a question about the machine, not
    # about the data, so it is answered before anything decides to call a model.
    if args.list_models or args.device_info:
        endpoint, models, label = discover(args.endpoint)
        if not endpoint:
            sys.exit(no_server_message())
        ram = args.ram_gb or total_ram_gb()
        for m in models:
            m["gb"] = estimate_gb(m)
        if args.device_info:
            show_device_info(models, ram, len(todo), args)
        else:
            show_models(models, ram, endpoint, label, args)
        return

    if todo:
        endpoint, models, label = discover(args.endpoint)
        if not endpoint:
            sys.exit(no_server_message())

        ram = args.ram_gb or total_ram_gb()
        if args.ram_gb is None and args.endpoint and not re.search(
                r"://(localhost|127\.0\.0\.1|\[::1\])", args.endpoint):
            ram = None          # the model runs elsewhere; local RAM is irrelevant
            print("remote endpoint — not checking this machine's memory")
        for m in models:
            m["gb"] = estimate_gb(m)

        if args.model:
            chosen = next((m for m in models if m["name"] == args.model),
                          {"name": args.model, "params": params_from_name(args.model),
                           "quant": quant_bytes_from_name(args.model), "bytes": None})
            chosen["gb"] = estimate_gb(chosen)
            budget = ram * 0.60 if ram else None
            if budget and chosen["gb"] and chosen["gb"] > budget and not args.force:
                sys.exit(
                    f"{describe(chosen)} needs roughly {chosen['gb']:.1f} GB, but this "
                    f"machine has {ram:.0f} GB and the safe budget is {budget:.1f} GB.\n"
                    f"Loading it will push the machine into swap and it may stop "
                    f"responding.\n"
                    f"Run --list-models to see what fits, or --force to try anyway.")
            model = chosen["name"]
        else:
            device = detect_device()
            chosen, ok, too_big, unknown = choose_model(
                models, ram, args.max_params, args.prefer, args.force)
            if not chosen:
                lines = [f"None of the {len(models)} available models are safe to run here."]
                if ram:
                    lines.append(f"This machine has {ram:.0f} GB RAM, so the budget is "
                                 f"{ram * 0.6:.1f} GB.")
                for m in sorted(too_big, key=lambda m: m.get("params") or 0)[:6]:
                    lines.append(f"  too big: {describe(m)}")
                lines.append("\nSomething in the 3B-8B range is plenty for labelling "
                             "merchants. For example:")
                lines.append("  ollama pull qwen2.5:7b      (~5 GB)")
                lines.append("  ollama pull llama3.2:3b     (~2 GB)")
                lines.append("\nOr pass --model NAME --force to override, or raise "
                             "--max-params.")
                sys.exit("\n".join(lines))
            print(f"using {label} at {endpoint}")
            print(f"device: {describe_device(device)}")

            fitting = ok or ([chosen] if chosen else [])
            picked, mins, notes = pick_for_time(
                print, endpoint, fitting, device, len(todo), args.target_minutes,
                args.workers, cache, probe=not args.no_probe)
            if picked:
                chosen = picked
            model = chosen["name"]

            for m, mm, why in notes:
                if m["name"] != model:
                    t = f"{mm:.0f} min" if mm else "unknown"
                    print(f"  {m['name']}: {t} ({why}) — passed over")
            skipped = len(too_big)
            if mins is not None and mins > args.target_minutes:
                print(f"  {describe(chosen)} is the fastest thing you have, but it still "
                      f"needs about {mins:.0f} min.")
                if (chosen.get("params") or 0) > 3.5:
                    print("  Something smaller would fix it:  ollama pull llama3.2:3b")
                else:
                    print("  That is as fast as this machine gets. Leave it running — "
                          "the cache means")
                    print("  you only pay this once, and Ctrl-C is safe.")
            elif mins is not None:
                t = "under a minute" if mins < 1 else f"about {mins:.0f} min"
                print(f"model: {describe(chosen)} — {t} for {len(todo)} merchants")
            else:
                print(f"model: {describe(chosen)}")
            if skipped:
                print(f"  ({skipped} larger model{'s' if skipped != 1 else ''} "
                      f"skipped as too heavy for this machine)")

        # Big models and parallel requests together are what actually locks a
        # laptop up, so back off automatically.
        workers = max(1, args.workers)
        params = (chosen or {}).get("params") or 0
        if params >= 20 and workers > 1:
            print(f"  running one request at a time — {params:g}B is heavy for "
                  f"parallel calls")
            workers = 1
        elif args.model is None and device.get("backend") == "cpu" and workers > 2:
            print("  CPU only, so parallel requests just fight each other — using 2")
            workers = 2
        print()

        batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
        done, t0 = 0, time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch, (result, err) in zip(
                    batches, pool.map(lambda b: label_batch(endpoint, model, b), batches)):
                done += 1
                if err:
                    print(f"  batch {done}/{len(batches)}: {err}")
                else:
                    cache.update(result)
                    print(f"  batch {done}/{len(batches)}: labelled {len(result)}/{len(batch)}")
                json.dump(cache, open(args.cache, "w"), indent=1)   # save as we go
                if done == 1 and len(batches) > 2:
                    each = time.time() - t0
                    if each > 45:
                        print(f"  that batch took {each:.0f}s — the full run will take "
                              f"about {each * len(batches) / 60:.0f} min. A smaller model "
                              f"or --batch 10 would speed this up. Ctrl-C is safe, "
                              f"progress is cached.")

    # ---- apply ----
    changed, low_conf = 0, 0
    moves = Counter()
    extras = []
    for t in rows:
        e = {}
        extras.append(e)
        hit = cache.get(merchant_of(t))
        if not hit:
            continue
        old = t.category or ""
        if hit.get("confidence") == "low":
            low_conf += 1
        if hit.get("name"):
            t.merchant = hit["name"]
        e["recurring_guess"] = "yes" if hit.get("recurring") else ""
        e["essential_guess"] = "yes" if hit.get("essential") else ""
        e["llm_confidence"] = hit.get("confidence", "")
        newcat = hit.get("category") or old
        if args.keep_rule_category:
            e["category_llm"] = newcat
        elif newcat and newcat != old:
            t.category = newcat
            changed += 1
            moves[f"{old or '(blank)'} -> {newcat}"] += 1

    print(f"\n{changed:,} transactions recategorised, {low_conf:,} flagged low confidence")
    for move, k in moves.most_common(8):
        print(f"  {k:>5}  {move}")

    to_store = source.lower().endswith(".db")
    if to_store:
        write_store(source, rows)
        print(f"\nWrote the labels back into {source}")
    if args.output or not to_store:
        out = args.output or "transactions_enriched.csv"
        write_csv(out, rows, extras)
        print(f"Wrote {out}")
    print(f"Cache kept in {args.cache} — delete it to re-label from scratch.")


if __name__ == "__main__":
    main()