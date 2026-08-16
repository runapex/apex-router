"""Test fixtures for the proxy_engine suite.

Importable as top-level `fixtures` because pytest (prepend import mode) puts each test file's
directory — here `tests/proxy_engine/` — on sys.path during collection, so `from fixtures.build_replay_corpus
import …` resolves to this package.
"""
