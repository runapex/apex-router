"""Opt-in LAN mesh: apex-router nodes on one local network find each other and organize.

Nodes that share one secret (``mesh.key``) form a mesh. Every message is HMAC-SHA256 signed and
checked for clock skew and replay (``protocol``). Each node keeps a peer table with TTL expiry,
a stable leader and a per-role pick (``peers``). Identity and secret live under
``~/.apex-router/mesh/`` or ``APEX_MESH_HOME`` (``identity``). Nothing listens or sends until
``apex-router mesh up`` runs. The contract is ``docs/DESIGN-lan-mesh.md``. Stdlib only.
"""
