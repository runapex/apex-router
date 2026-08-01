"""Repo-agnostic code-Q&A harness over the local Ornith server.

Deterministic tools traverse (retriever.py: ripgrep + optional clangd index); Ornith
reads the retrieved, cited chunks and answers (driver.py), with the repo's architecture
digest pinned as a frozen preamble for prompt-cache reuse. Repos are registered as
codeqa/repos/<name>.json — sample-cpp (C++) and sample-ruby (Ruby) today.
"""
