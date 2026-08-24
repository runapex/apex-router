"""Local-model TIER selection — which Ornith size is resident, and the env that speaks to it.

This is the LOCAL axis. Do not confuse it with `apex_router.codeqa.tier_router`, which picks the
FRONTIER Claude tier + reasoning effort. That one chooses who to escalate to; this one chooses
what runs on the machine.

Ornith 1.5 ships 9B, 35B-A3B and 397B — there is no 27B, so the two runnable-on-a-laptop sizes are
the ones below. 397B is not offered: it does not fit any single Apple-Silicon box.

Backend is ollama (OpenAI-compatible, :11434). The previous MLX server (`mlx_lm.server` on :8080)
is retired — it served ONE model fixed at process start, which is exactly what makes a tier switch
impossible without a restart. ollama loads per request and unloads on `keep_alive`, so the switch
is a config write plus a warm, not a rebuild.

RESIDENCY IS EXCLUSIVE. Both tiers at once is ~27 GB of weights on a machine whose total is checked
by `fits()`; the switcher unloads the outgoing tier before warming the incoming one. That is a
capacity invariant, not an optimisation — an over-commit here swaps and the box stops responding.

Pure stdlib. `resolve()` takes an injected env + state path so it is deterministic and offline,
matching the testing convention used across this package.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# The env file the switcher writes and the launchd units source. One source of truth: a tier is
# "active" because it is written HERE, not because some process happens to have weights loaded.
STATE_FILE = Path(os.environ.get(
    "APEX_ORNITH_TIER_FILE", Path.home() / ".apex-router" / "ornith.env"))

# ollama's OpenAI-compatible surface. Distinct from the retired MLX default (:8080).
DEFAULT_URL = "http://127.0.0.1:11434"

# ollama gates thinking with `reasoning_effort`, not chat-template kwargs. ornith_client already
# branches on this — see its _apply_backend().
THINKING_STYLE = "reasoning_effort"


@dataclass(frozen=True)
class Tier:
    name: str
    api_model: str      # the id sent in the request body's `model` field
    weights_gb: float   # on-disk / resident size of the quantised weights
    active_b: float     # ACTIVE params per token — what decode speed tracks, not total params
    total_b: float
    note: str

    @property
    def is_moe(self) -> bool:
        return self.active_b < self.total_b


DEFAULT_FAMILY = "ornith"

# Committed families. Ornith 1.5 is the shipped default; other local families are declared
# machine-locally in ~/.apex-router/models.json under local_families and merged in by
# load_families() — never hardcoded here (this is a public, vendor-neutral repo).
# Q4_K_M for both Ornith tiers: the quality/size knee, and the only quant present in BOTH GGUF repos.
FAMILIES: dict[str, dict[str, Tier]] = {
    "ornith": {
        "small": Tier(
            name="small",
            api_model="hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M",
            weights_gb=5.6, active_b=9.0, total_b=9.0,
            note="dense 9B — coexists with nomic-embed and the proxy without pressure",
        ),
        "large": Tier(
            name="large",
            api_model="hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M",
            weights_gb=21.2, active_b=3.0, total_b=34.7,
            note="35B-A3B MoE — 3B active per token, so it decodes near a 3B while reasoning near a 35B",
        ),
    },
}
# Back-compat: existing consumers and tests import TIERS as "the tiers of the active/default family".
TIERS: dict[str, Tier] = FAMILIES[DEFAULT_FAMILY]

DEFAULT_TIER = "small"

# Weights are not the whole footprint: the KV cache, the runtime and the rest of the desktop all
# want RAM. Refuse a tier that would leave less than this free. Ornith 1.5 carries a 262k context,
# so an unbounded KV cache can dwarf the weights — headroom is not padding here.
MIN_FREE_GB = 8.0


def _read_state(path: Path | None = None) -> dict[str, str]:
    """Parse the tier env file (KEY=VALUE, # comments). A missing/unreadable file is not an error —
    it just means 'no tier pinned yet', and the caller falls back to DEFAULT_TIER."""
    p = STATE_FILE if path is None else Path(path)
    out: dict[str, str] = {}
    try:
        text = p.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_families(env: dict | None = None, overlay_path: Path | None = None
                  ) -> dict[str, dict[str, Tier]]:
    """Committed FAMILIES deep-merged with the local_families overlay in ~/.apex-router/models.json.
    Never raises: a missing/malformed overlay yields the committed families only."""
    out: dict[str, dict[str, Tier]] = {fam: dict(tiers) for fam, tiers in FAMILIES.items()}
    p = overlay_path if overlay_path is not None else (
        Path((env or os.environ).get("APEX_MODEL_REGISTRY", ""))
        if (env or os.environ).get("APEX_MODEL_REGISTRY")
        else Path.home() / ".apex-router" / "models.json")
    try:
        overlay = json.loads(p.read_text())
        local_fams = overlay.get("local_families") if isinstance(overlay, dict) else None
        if isinstance(local_fams, dict):
            for fam, tiers in local_fams.items():
                if not isinstance(tiers, dict):
                    continue
                dst = out.setdefault(fam, {})
                tier_map = tiers.get("tiers", tiers)
                if not isinstance(tier_map, dict):
                    continue
                for tname, spec in tier_map.items():
                    # Documented shorthand: a bare string tier value IS the api_model. weights_gb=0
                    # → fits() treats it as unmeasurable/not-gating, same as a pinned Tier.
                    if isinstance(spec, str) and spec:
                        dst[tname] = Tier(name=tname, api_model=spec, weights_gb=0.0,
                                          active_b=0.0, total_b=0.0, note="")
                        continue
                    if not isinstance(spec, dict) or "api_model" not in spec:
                        continue
                    dst[tname] = Tier(
                        name=tname, api_model=str(spec["api_model"]),
                        weights_gb=float(spec.get("weights_gb", 0.0)),
                        active_b=float(spec.get("active_b", 0.0)),
                        total_b=float(spec.get("total_b", 0.0)),
                        note=str(spec.get("note", "")))
    except (OSError, ValueError, TypeError):
        pass
    return out


def resolve(env: dict | None = None, state_file: Path | None = None,
            overlay_path: Path | None = None) -> Tier:
    """Active Tier. Precedence:
      1. explicit ORNITH_API_MODEL  -> honored verbatim (a synthesized 'pinned' Tier if it maps to
         no known family/tier), so a pinned backend is always authoritative.
      2. LOCAL_FAMILY + ORNITH_TIER (env, else the state file), resolved through the merged families.
      3. DEFAULT_FAMILY/DEFAULT_TIER.
    Never raises: any unknown value falls back rather than blocking a batch job."""
    source = dict(os.environ) if env is None else dict(env)
    pinned = source.get("ORNITH_API_MODEL")
    families = load_families(env=source, overlay_path=overlay_path)
    if pinned:
        for tiers in families.values():
            for t in tiers.values():
                if t.api_model == pinned:
                    return t
        return Tier(name="pinned", api_model=pinned, weights_gb=0.0,
                    active_b=0.0, total_b=0.0, note="pinned via ORNITH_API_MODEL")
    state = _read_state(state_file)
    fam = (source.get("LOCAL_FAMILY") or state.get("LOCAL_FAMILY") or DEFAULT_FAMILY).strip().lower()
    tier = (source.get("ORNITH_TIER") or state.get("ORNITH_TIER") or DEFAULT_TIER).strip().lower()
    tiers = families.get(fam) or families[DEFAULT_FAMILY]
    return tiers.get(tier) or families[DEFAULT_FAMILY][DEFAULT_TIER]


def total_ram_gb() -> float:
    """Physical RAM in GB. Read from the OS — the old hardcoded 52 GB ceiling in model_router was
    measured on a different machine and silently mis-gated every other one."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        return 0.0


def fits(tier: Tier, total_gb: float | None = None) -> tuple[bool, str]:
    """Does `tier` fit in RAM with MIN_FREE_GB to spare? Returns (fits, reason).

    An unknown total (sysconf unavailable) returns True: refuse to block work on a number we could
    not measure. The switcher reports the reason either way.
    """
    total = total_ram_gb() if total_gb is None else total_gb
    if total <= 0:
        return True, "RAM unknown — not gating"
    if tier.weights_gb <= 0:
        return True, f"{tier.api_model}: size unknown — not gating"
    free_after = total - tier.weights_gb
    if free_after < MIN_FREE_GB:
        return False, (f"{tier.api_model} needs ~{tier.weights_gb:.1f} GB; only "
                       f"{free_after:.1f} GB would remain of {total:.0f} GB "
                       f"(< {MIN_FREE_GB:.0f} GB floor)")
    return True, f"~{tier.weights_gb:.1f} GB of {total:.0f} GB, {free_after:.1f} GB free after load"


def family_of(tier: Tier) -> str:
    """Which family a Tier belongs to, derived from the merged families by matching api_model.
    A tier that maps to no known family (e.g. a synthesized 'pinned' Tier) is reported under the
    default family — the state file always names a concrete family."""
    for fam, tiers in load_families().items():
        if any(t.api_model == tier.api_model for t in tiers.values()):
            return fam
    return DEFAULT_FAMILY


def client_env(tier: Tier | None = None, url: str | None = None) -> dict[str, str]:
    """The ORNITH_* environment that points ornith_client at `tier`.

    These are exactly the three knobs ornith_client binds at import: the endpoint, the API model id
    (which ollama REQUIRES — the MLX server let clients omit it), and the thinking style. It also
    records LOCAL_FAMILY so resolve() can round-trip the family, not just the tier.
    """
    t = resolve() if tier is None else tier
    return {
        "ORNITH_TIER": t.name,
        "LOCAL_FAMILY": family_of(t),
        "ORNITH_URL": url or os.environ.get("ORNITH_URL") or DEFAULT_URL,
        "ORNITH_API_MODEL": t.api_model,
        "ORNITH_THINKING_STYLE": THINKING_STYLE,
    }


def render_state(tier: Tier, url: str | None = None) -> str:
    """The ornith.env file body for `tier` — what the switcher writes and launchd units source."""
    env = client_env(tier, url=url)
    return (
        "# apex-router local-model tier — WRITTEN BY `apex-router ornith-tier`, do not hand-edit.\n"
        f"# active tier: {tier.name} ({tier.note})\n"
        + "".join(f"{k}={v}\n" for k, v in env.items())
    )
