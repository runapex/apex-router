# LAN mesh: apex-router nodes find each other and organize (opt-in)

Status: v1. Default OFF. Nothing listens and nothing is sent until `apex-router mesh up` runs.

## Goal

1. Open a public interface on the local network (LAN) for one apex-router node.
2. Let apex-router nodes on the same LAN find each other with no central server.
3. Let the nodes organize: one stable leader, and a pick of the best peer for a role.

## Security model

- A mesh is a group of nodes that share one secret (`mesh.key`, 32 random bytes).
- Every datagram and every HTTP body is signed with HMAC-SHA256 over the secret.
- A node drops a message with a bad MAC, a clock skew > 30 s, or a seen nonce (replay).
- The secret never goes on the wire. Copy it to other machines out of band (`mesh init --export`).
- The interface binds to ONE private LAN address (RFC 1918 / link-local / ULA), never `0.0.0.0`.
  `--bind` sets another address. A public (internet-routable) address needs `--allow-public`.
- Payloads carry node metadata only: node id, name, host, version, roles, port, load.
  No prompt text, no file contents, no paths, no session ids.
- All state lives under `~/.apex-router/mesh/` (override: `APEX_MESH_HOME`). Files are mode 0600.

## Modules (`src/apex_router/mesh/`)

| Module | Owns | Network |
|---|---|---|
| `identity.py` | node id, secret key, config load/save | no |
| `protocol.py` | message encode/decode, HMAC, skew, replay window | no |
| `peers.py` | peer table, TTL expiry, leader election, role pick | no |
| `netif.py` | choose the LAN bind address, private-address check | reads only |
| `beacon.py` | UDP multicast announce + listen | UDP |
| `server.py` | HTTP interface (stdlib `http.server`) | TCP |
| `cli.py` | `apex-router mesh …` subcommands | via the above |

Stdlib only. No third-party dependency.

## Wire contract

Constants (in `protocol.py`):

```
PROTO = "apex-mesh/1"
MCAST_GROUP = "239.255.77.77"     # admin-scoped (RFC 2365), stays on the LAN
MCAST_PORT = 47777
DEFAULT_HTTP_PORT = 47778
MAX_SKEW_S = 30
MAX_DATAGRAM = 1400               # one Ethernet frame, no fragmentation
BEACON_INTERVAL_S = 5
PEER_TTL_S = 20                   # 4 missed beacons
```

Envelope (JSON, UTF-8, compact separators, sorted keys):

```
{"proto": "apex-mesh/1", "kind": "<kind>", "mesh": "<mesh_id>", "ts": <float unix>,
 "nonce": "<16 hex>", "body": {...}, "mac": "<hex hmac-sha256>"}
```

- `mesh_id` = first 16 hex of `sha256(b"apex-mesh-id" + secret)`. It lets a node ignore other
  meshes fast. It does not show the secret.
- `mac` = HMAC-SHA256(secret, canonical JSON of the envelope WITHOUT `mac`).
- Kinds: `announce` (beacon), `bye` (clean stop), `card` (HTTP response body).

Node card (the `body` of `announce` and `card`):

```
{"node_id": "<32 hex>", "name": "<str, max 64>", "host": "<ip>", "port": <int>,
 "version": "<apex-router version>", "roles": ["<str>", ...], "load": <int >= 0>,
 "started_at": <float unix>}
```

## Python API (the shared contract between modules)

```python
# identity.py
def mesh_home() -> Path                      # APEX_MESH_HOME or ~/.apex-router/mesh
@dataclass(frozen=True)
class Identity:
    node_id: str; name: str; secret: bytes
    @property
    def mesh_id(self) -> str
def init(home: Path | None = None, *, secret: bytes | None = None,
         name: str | None = None, force: bool = False) -> Identity
def load(home: Path | None = None) -> Identity           # raises MeshNotInitialized
def export_secret(ident: Identity) -> str                 # "apexmesh1:<base64url>"
def parse_secret(token: str) -> bytes                     # ValueError on a bad token

# protocol.py
class ReplayGuard:                       # bounded nonce memory, thread-safe
    def __init__(self, max_entries: int = 4096) -> None
    def seen(self, nonce: str, ts: float) -> bool         # True = replay, drop it
def encode(kind: str, body: dict, ident: Identity, *, now: float | None = None) -> bytes
def decode(data: bytes, ident: Identity, guard: ReplayGuard | None,
           *, now: float | None = None) -> dict            # raises ProtocolError; returns envelope

# peers.py
@dataclass
class Peer:
    node_id: str; name: str; host: str; port: int; version: str
    roles: tuple[str, ...]; load: int; started_at: float; last_seen: float
class PeerTable:                          # thread-safe
    def __init__(self, self_card: dict, ttl_s: float = PEER_TTL_S) -> None
    def update(self, card: dict, *, now: float | None = None) -> Peer | None   # bad card -> None
    def remove(self, node_id: str) -> None
    def alive(self, *, now: float | None = None) -> list[Peer]  # includes self, sorted by node_id
    def leader(self, *, now: float | None = None) -> Peer       # min (started_at, node_id) of alive
    def pick(self, role: str, *, now: float | None = None) -> Peer | None
                                          # alive peers with role; min (load, node_id)
    def snapshot(self, *, now: float | None = None) -> dict     # JSON-safe view

# netif.py
def is_private(ip: str) -> bool
def default_lan_ip() -> str | None        # UDP connect trick, no packet sent
def choose_bind(bind: str | None, *, allow_public: bool = False) -> str   # raises ValueError

# beacon.py
class Beacon:
    def __init__(self, ident, table, card_fn, *, group=MCAST_GROUP, port=MCAST_PORT,
                 iface_ip: str, interval_s=BEACON_INTERVAL_S, targets: list[tuple[str,int]] | None = None)
    def start(self) -> None; def stop(self) -> None       # stop sends `bye`
    # `targets` = extra unicast destinations (tests, networks with no multicast)

# server.py
class MeshServer:
    def __init__(self, ident, table, card_fn, *, host: str, port: int = DEFAULT_HTTP_PORT)
    def start(self) -> None; def stop(self) -> None
    @property
    def address(self) -> tuple[str, int]
    # GET /mesh/v1/card   -> signed `card` envelope (no auth needed to read: it is signed, not secret)
    # GET /mesh/v1/peers  -> needs header X-Apex-Mesh: <signed envelope kind "auth">; else 401
    # GET /mesh/v1/leader -> same auth as /peers
    # All other paths -> 404. No POST in v1.
```

## CLI

```
apex-router mesh init [--name N] [--import TOKEN] [--export] [--force]
apex-router mesh up [--bind IP] [--port P] [--allow-public] [--role R]... [--peer HOST:PORT]...
apex-router mesh peers [--json]       # reads ~/.apex-router/mesh/peers.json written by `up`
apex-router mesh leader [--json]
apex-router mesh pick ROLE [--json]   # exit 0 = found, 1 = none
apex-router mesh status [--json]      # identity + whether `up` runs (pid file)
```

`mesh up` runs in the foreground until SIGINT/SIGTERM. It rewrites `peers.json` (atomic
replace, 0600) on each beacon cycle, so other tools read the mesh without network access.

## Data plane: analyze metrics from many machines (research speed-up)

Agents on one machine must read and pool the metrics of every machine in the mesh. One
call fans out to all alive peers in parallel and returns one merged result.

### Datasets (allowlist, `datasets.py`)

Only these names are served. Each is an aggregate: numbers and enums, no free text.

| Name | Source | Notes |
|---|---|---|
| `pressure` | `snapshot.pressure_block()` | level, causes, families. No telemetry path. |
| `errors15m` | `snapshot.errors_block()` | counts by cause |
| `measure` | `snapshot.measure_block()` | limit %, cost, ctx % |
| `route_rates` | `route_log.read_rates()` | `{task_type: {n, escalated, rate, null_ts?}}` |
| `route_advice` | `route_advise.advise()` | local verdicts |
| `worker` | `agents.worker()` | counts only; drop `error`, `label` |

NOT served: `agents` (repo, session_id), widget history (session ids), `adapters` (free text),
`quality`, `labels/`, `worldmodel/`, raw `route_log.jsonl` rows, any telemetry rows.

- Every dataset is passed through `scrub()`: it drops keys named `error`, `path`, `telemetry`,
  `session`, `session_id`, `repo`, `cwd`, `description`, `note`, `url`, and any str value that
  starts with `/` or `~` or contains `\\`.
- `mesh up --share NAME` (repeatable) limits the served set. Default: all allowlisted names.
  `--share none` serves no data (discovery only).
- A dataset function error returns `{"error": "unavailable"}` for that dataset. It never raises.

### Pooling (`collect.py`)

```python
def fetch(peer: Peer, ident: Identity, names: list[str], *, timeout: float = 3.0) -> dict
    # GET /mesh/v1/data?names=a,b ; signed auth header; verifies the signed response.
    # returns {"node_id", "name", "ok": bool, "data": {name: value}, "error"?: str}
def collect(table: PeerTable, ident: Identity, names: list[str], *,
            include_self: bool = True, timeout: float = 3.0, max_workers: int = 16) -> dict
    # parallel fetch (ThreadPoolExecutor); self is read locally, not over HTTP.
    # returns {"nodes": [fetch results], "pooled": pool(...), "n_nodes": int, "n_ok": int}
def pool(results: list[dict]) -> dict
    # route_rates: sum n, escalated, null_ts per task_type; rate = escalated / n.
    # route_advice_pooled: route_advise.advise(rates=<pooled route_rates>)  -> larger n, same BH gate.
    # pressure: worst level across nodes (RED > AMBER > GREEN > UNKNOWN) + per-node levels.
    # errors15m: sum n and by_cause.
    # measure: per-node only (not summed: limits are per account).
```

### HTTP

`GET /mesh/v1/data?names=a,b` -> needs the auth header (same as `/peers`). Response: signed
envelope kind `data`, body `{"node_id": ..., "data": {name: value}}`. An unknown or not-shared
name returns `{"error": "not_shared"}` for that name.

Auth header `X-Apex-Mesh` = signed envelope kind `auth`, body `{"path": "<path?query>"}`.
The server checks MAC, skew, replay, and that `body.path` equals the request path + query.

### CLI

```
apex-router mesh collect [NAME ...] [--json] [--timeout S]   # default names: all allowlisted
apex-router mesh advise [--json]                             # pooled route_advice table
```

`mesh collect` reads peers from `peers.json` (written by `mesh up`), so a short-lived agent
command needs no beacon of its own.
