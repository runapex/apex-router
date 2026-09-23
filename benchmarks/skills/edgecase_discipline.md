<!-- Seed skill for the skill-quality benchmark (measure-only; adopted only if it clears apex's gate over >=2 windows). Targets the off-by-one / boundary failure modes the codegen benchmark exercises. -->

# Edge-case discipline for small Python functions

Before writing the function, restate the exact contract, then handle the boundaries FIRST.

- MUST honor the stated indexing base literally. If the spec says ONE-BASED, index `s[i-1]`, not `s[i]`. Never assume zero-based.
- MUST raise the exact exception the spec names (e.g. `IndexError`, `ValueError`) on out-of-range or invalid input, and MUST check the range with the spec's exact bounds (`i < 1 or i > len(s)`), not an approximation.
- ALWAYS handle the empty input and the single-element input explicitly before the general case.
- ALWAYS return the exact type the spec asks for (a character vs a length-1 string, an int vs a float).
- NEVER silently clamp, wrap, or skip an out-of-range index — raise as specified.
- VERIFY your function against the spec's own examples in your head before finalizing: substitute the example inputs and confirm the stated output.
