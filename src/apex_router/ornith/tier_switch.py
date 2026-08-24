"""Switch which local Ornith tier is resident — the `apex-router ornith-tier` implementation.

A switch is four steps, in this order, and the order is the whole point:

  1. CHECK the target fits physical RAM (local_tier.fits).
  2. UNLOAD the outgoing tier. First, not last — both tiers resident is ~27 GB of weights, and on a
     36 GB box that is the difference between a swap storm and a working machine.
  3. WRITE the new tier to ornith.env, the single source of truth every consumer reads.
  4. WARM the incoming tier and wait for it to actually answer, so the command returns only once
     the tier is genuinely serving. A switch that returns before the model is loaded just moves the
     cold-start cost onto whichever unlucky job arrives first.

Then reload the launchd consumers, which bound their ORNITH_* config at process start and would
otherwise keep talking to the old tier until something restarted them (the exact stale-config
failure `version_guard` exists to catch for source).

Pure stdlib + the `ollama` binary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import local_tier

# launchd units that hold an ORNITH_* config and must be restarted after a switch. The retired MLX
# server unit is NOT here: it is not part of the ollama path and must not be revived by a switch.
CONSUMER_UNITS = ("com.ornith.worker", "com.ornith.overnight")

WARM_TIMEOUT_S = float(os.environ.get("ORNITH_WARM_TIMEOUT_SECS", "600"))


def _ollama(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["ollama", *args], capture_output=True, text=True, timeout=timeout)


def resident_models() -> list[str]:
    """Models ollama has LOADED (from `ollama ps`), not merely pulled. Never raises."""
    try:
        p = _ollama("ps", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [ln.split()[0] for ln in p.stdout.splitlines()[1:] if ln.strip()]


def pulled_models() -> list[str]:
    """Models present on disk (from `ollama list`). Never raises."""
    try:
        p = _ollama("list", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [ln.split()[0] for ln in p.stdout.splitlines()[1:] if ln.strip()]


def unload(model: str) -> bool:
    """Evict `model` from memory by generating with keep_alive=0 — ollama's documented unload
    handshake. Returns whether the model is gone afterwards, which is what the caller cares about;
    a request error on an already-unloaded model is success, not failure."""
    url = (os.environ.get("ORNITH_URL") or local_tier.DEFAULT_URL).rstrip("/")
    body = json.dumps({"model": model, "keep_alive": 0}).encode()
    req = urllib.request.Request(f"{url}/api/generate", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except (urllib.error.URLError, OSError):
        pass  # unreachable / already gone — verified below either way
    return model not in resident_models()


def unload_all_tiers() -> list[str]:
    """Unload every KNOWN tier model that is currently resident. Returns what was unloaded.

    Deliberately scoped to tier models: nomic-embed-text and anything else the user is running are
    not ours to evict.
    """
    # `ollama ps` may print the fully-qualified `name:tag` where the config carries a bare name (and
    # vice versa), so compare on both forms. Not str.rstrip(":latest") — that strips a CHARACTER
    # SET and would mangle any id ending in one of those letters.
    def _forms(m: str) -> set[str]:
        return {m, m[: -len(":latest")]} if m.endswith(":latest") else {m, f"{m}:latest"}

    tier_models = {f for tiers in local_tier.load_families().values()
                   for t in tiers.values() for f in _forms(t.api_model)}
    # A pinned backend (ORNITH_API_MODEL in no family) is resident but excluded above; add the ONE
    # resolved active model so a switch never leaves it loaded alongside the incoming tier. This
    # still evicts nothing unrelated — only forms of the single active model join the candidate set.
    try:
        tier_models |= _forms(local_tier.resolve().api_model)
    except Exception:
        pass
    freed = []
    for m in resident_models():
        if _forms(m) & tier_models and unload(m):
            freed.append(m)
    return freed


def warm(tier: local_tier.Tier, timeout_s: float = WARM_TIMEOUT_S) -> tuple[bool, str]:
    """Load `tier` and block until it answers a trivial generation. Returns (ok, detail).

    Uses a real 1-token generation rather than a liveness ping: ollama's endpoint is up long before
    a 21 GB model is resident, so a ping would report ready while the first real request still eats
    the whole load. The generous default timeout is the cold-load budget for the large tier.
    """
    url = (os.environ.get("ORNITH_URL") or local_tier.DEFAULT_URL).rstrip("/")
    body = json.dumps({
        "model": tier.api_model,
        "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        "max_tokens": 8, "temperature": 0.0,
        "reasoning_effort": "none",
    }).encode()
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return False, f"{type(e).__name__}: {e}"
    took = time.monotonic() - started
    try:
        answer = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, f"unexpected response: {payload!r}"[:300]
    return True, f"warm in {took:.1f}s (answered {answer.strip()[:20]!r})"


def write_state(tier: local_tier.Tier, path: Path | None = None) -> Path:
    """Write ornith.env atomically. Atomic because the launchd consumers may read it at any moment;
    a half-written file would point them at a truncated model id."""
    p = local_tier.STATE_FILE if path is None else Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(local_tier.render_state(tier))
    os.replace(tmp, p)
    return p


def reload_consumers(units: tuple[str, ...] = CONSUMER_UNITS) -> dict[str, str]:
    """Restart the launchd consumers so they re-read the new tier. A unit that is not loaded on
    this machine is reported as such, not treated as an error — the install is modular."""
    uid = os.getuid()
    out = {}
    for u in units:
        target = f"gui/{uid}/{u}"
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0:
            out[u] = "not loaded — skipped"
            continue
        r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
        out[u] = "restarted" if r.returncode == 0 else f"failed: {r.stderr.strip()[:120]}"
    return out


def switch(name: str, *, family: str | None = None, warm_after: bool = True,
           reload_units: bool = True, state_path: Path | None = None) -> int:
    """Perform the full switch. Returns a process exit code and prints progress to stdout.

    `family` selects which local family the tier belongs to (default: the committed default
    family), resolved through the merged families so machine-local overlays are switchable too."""
    key = name.strip().lower()
    fams = local_tier.load_families()
    fam = (family or local_tier.DEFAULT_FAMILY).strip().lower()
    tiers = fams.get(fam)
    if not tiers or key not in tiers:
        print(f"unknown tier {name!r} in family {fam!r} (have: "
              f"{', '.join(sorted(tiers or {}))})", file=sys.stderr)
        return 2
    tier = tiers[key]

    ok, why = local_tier.fits(tier)
    print(f"tier {tier.name}: {tier.api_model}\n  capacity: {why}")
    if not ok:
        print("  refusing to switch — free RAM or pick the small tier", file=sys.stderr)
        return 1

    if tier.api_model not in pulled_models():
        print(f"  NOT PULLED — run first:  ollama pull {tier.api_model}", file=sys.stderr)
        return 1

    freed = unload_all_tiers()
    print(f"  unloaded: {', '.join(freed) if freed else 'nothing resident'}")

    p = write_state(tier, state_path)
    print(f"  wrote: {p}")

    if warm_after:
        ok, detail = warm(tier)
        print(f"  warm: {detail}")
        if not ok:
            print("  tier is written but did NOT answer — jobs will fail until it does",
                  file=sys.stderr)
            return 1

    if reload_units:
        for unit, result in reload_consumers().items():
            print(f"  {unit}: {result}")
    return 0


def status() -> dict:
    """What tier is configured, what is actually loaded, and whether they agree."""
    active = local_tier.resolve()
    resident = resident_models()
    pulled = pulled_models()
    return {
        "configured_tier": active.name,
        "configured_model": active.api_model,
        "state_file": str(local_tier.STATE_FILE),
        "state_file_exists": local_tier.STATE_FILE.exists(),
        "url": os.environ.get("ORNITH_URL") or local_tier.DEFAULT_URL,
        "resident_models": resident,
        # The configured tier can be pulled, written, and still not loaded — ollama loads lazily.
        # Reporting these separately is the point: "configured" never implies "serving".
        "configured_is_pulled": active.api_model in pulled,
        "configured_is_resident": active.api_model in resident,
        "ram_gb": round(local_tier.total_ram_gb(), 1),
        "tiers": {n: {"model": t.api_model, "weights_gb": t.weights_gb,
                      "fits": local_tier.fits(t)[0], "note": t.note}
                  for n, t in local_tier.TIERS.items()},
    }
