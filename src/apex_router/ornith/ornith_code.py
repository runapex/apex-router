"""Code generation on local Ornith — offload well-specified small coding tasks off Opus/Codex.

MEASURED the reference window: on well-specified small functions with executable ground-truth tests,
Ornith writes CORRECT code at 4/4 pass, ~1.5s/task — but ONLY with thinking OFF. With thinking
ON it hangs: 0/3, every task burned its whole token budget inside <think> and returned no
answer (OrnithProtocolError). So code-gen here is thinking-OFF by hard default; turning it on is
the documented failure mode, not an upgrade.

SCOPE (the offload is only a win inside this envelope):
  - WELL-SPECIFIED, SELF-CONTAINED functions: clear signature + behavior, no repo context, no
    multi-file reasoning. Ornith is a fidelity model, not a reasoner (it asserted a WRONG root
    cause on a reasoning task — see the local-model-verdict memory) — so it generates from a
    precise spec, it does not design.
  - The caller MUST verify the output (run the tests). Offload saves Opus/Codex tokens ONLY when
    the generated code is correct; wrong code that Opus then has to fix costs MORE than not
    offloading. Treat this as "draft, then verify", never "trust".

NOT for: refactors, architecture, cross-file changes, anything needing judgment about the
existing codebase. Those stay on Opus (model-routing heavy tier).
"""
from __future__ import annotations

import re

from . import ornith_client as oc

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*(.*?)```", re.S)


def extract_code(answer: str) -> str:
    """Pull the fenced code block from a model answer; fall back to the raw text if unfenced."""
    m = _CODE_BLOCK.search(answer)
    return m.group(1).strip() if m else answer.strip()


def generate_code(
    spec: str,
    *,
    language: str = "python",
    max_tokens: int = 1200,
    enable_thinking: bool = False,
    temperature: float = 0.0,
) -> str:
    """Generate code for a well-specified task. Returns the extracted code string.

    `enable_thinking` defaults **False** — MEASURED: thinking-on hangs on code-gen (runs the
    whole budget inside <think>, no answer). Do not flip it on without re-measuring.

    The caller is responsible for verifying the result (run its tests). This is a draft
    generator to offload token spend from Opus/Codex on tasks precise enough to verify — not a
    trusted authority. Raises OrnithProtocolError on an empty/truncated answer.
    """
    prompt = (
        f"Write {language} code for this task. Return ONLY the code in a ```{language} block, "
        f"no explanation.\n\nTASK:\n{spec}"
    )
    result = oc.chat_messages(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        temperature=temperature,
    )
    return extract_code(result.answer)
