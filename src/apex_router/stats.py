"""Back-compat shim — the implementations moved to apex_router.core.stats (pce-core).

bradley_terry was removed: it had no caller outside its own tests (spec §16)."""
from apex_router.core.stats import (  # noqa: F401  (re-exported API)
    benjamini_hochberg,
    paired_bootstrap_ci,
    paired_bootstrap_pvalue,
    wilson_ci,
)
