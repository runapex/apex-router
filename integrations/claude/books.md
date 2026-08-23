---
description: Top-K local-book references for a problem (local semantic search + local-model reasoning)
allowed-tools: Bash(booksearch:*)
argument-hint: <problem or question>
---

Local book references for the problem: **$ARGUMENTS**

!`booksearch query "$ARGUMENTS" -k 5`

Using the ranked references above (from my local `~/books` library, retrieved by a
local embedding model and explained by a local model), cite the ones that actually
help and briefly say how to use each while solving the problem.
