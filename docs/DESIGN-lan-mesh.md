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
- Payloads carry node metadata only: node id, name, host, version, roles, port, load, models.
  No prompt text, no file contents, no paths, no session ids. Exception: model calls (opt-in,
  see "Model calls") carry the prompt and the answer.
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
| `infer.py` | model-call request check, upstream ollama call, `ask` client | TCP |
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
- Kinds: `announce` (beacon), `bye` (clean stop), `card` (HTTP response body), `infer`
  (model-call response).

Node card (the `body` of `announce` and `card`):

```
{"node_id": "<32 hex>", "name": "<str, max 64>", "host": "<ip>", "port": <int>,
 "version": "<apex-router version>", "roles": ["<str>", ...], "load": <int >= 0>,
 "started_at": <float unix>, "models": ["<model>", ...]}
```

`models` is optional. A card without it is valid (old nodes); the Peer gets `models=()`. When
present: a list of at most 16 str, each 1..128 chars of `^[A-Za-z0-9._:/-]+$`. Else the card is
rejected.

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
def auth_header(ident: Identity, path: str, to: str, *, now: float | None = None,
                body_sha256: str | None = None) -> str
    # the only builder of the X-Apex-Mesh header; server.py, collect.py and infer.py use it
    # body_sha256 (POST only) adds "body_sha256" to the auth body

# peers.py
@dataclass
class Peer:
    node_id: str; name: str; host: str; port: int; version: str
    roles: tuple[str, ...]; load: int; started_at: float; last_seen: float
    models: tuple[str, ...] = ()          # served models; empty for old cards
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
    # `targets` = extra unicast destinations (tests, networks with no multicast).
    # Each target is (host, UDP beacon port) of the other node, not its HTTP port.

# server.py
class MeshServer:
    def __init__(self, ident, table, card_fn, *, host: str, port: int = DEFAULT_HTTP_PORT,
                 shared=frozenset(), models=(), infer_slots: int = 1, ollama: str | None = None)
    def start(self) -> None; def stop(self) -> None
    @property
    def address(self) -> tuple[str, int]
    # GET /mesh/v1/card   -> signed `card` envelope (no auth needed to read: it is signed, not secret)
    # GET /mesh/v1/peers  -> needs header X-Apex-Mesh: <signed envelope kind "auth">; else 401
    # GET /mesh/v1/leader -> same auth as /peers
    # POST /mesh/v1/infer -> only with models (`--serve-model`); else 404. See "Model calls".
    # All other paths -> 404. POST to any other path, and PUT/DELETE/... -> 501.
    @property
    def infer_running(self) -> int        # model calls that run now
```

## CLI

```
apex-router mesh init [--name N] [--import TOKEN] [--export] [--force]
apex-router mesh up [--bind IP] [--port P] [--allow-public] [--role R]... [--peer HOST[:PORT]]...
                    [--share NAME]... [--serve-model NAME]... [--infer-slots N]
apex-router mesh peers [--json]       # reads ~/.apex-router/mesh/peers.json written by `up`
apex-router mesh leader [--json]
apex-router mesh pick ROLE [--json]   # exit 0 = found, 1 = none
apex-router mesh status [--json]      # identity + whether `up` runs (flock on mesh.lock)
apex-router mesh models [--json]      # node, model for every served model
apex-router mesh ask --model M [--node NAME] [--max-tokens N] [--system S] [--json] PROMPT
```

`--peer` adds a unicast beacon target. PORT is the UDP beacon port of that node (default
`MCAST_PORT` = 47777), not its HTTP port. Use `[V6]:PORT` for an IPv6 address with a port.

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

Auth header `X-Apex-Mesh` = signed envelope kind `auth`, body
`{"path": "<path?query>", "to": "<node_id of the server>"}`. Build it only with
`protocol.auth_header(ident, path, to)`. The server checks MAC, skew, replay, that `body.path`
equals the request path + query, and that `body.to` equals its own `node_id`. So a header sent
to one node cannot be replayed to another node of the mesh.

### CLI

```
apex-router mesh collect [NAME ...] [--json] [--timeout S]   # default names: all allowlisted
apex-router mesh advise [--json]                             # pooled route_advice table
```

`mesh collect` reads peers from `peers.json` (written by `mesh up`), so a short-lived agent
command needs no beacon of its own.

## Model calls (opt-in)

One node runs a chat prompt on the local ollama of another node. Model calls only: no shell, no
tools, no agent run.

### Serve

`mesh up --serve-model NAME` (repeatable, max 16) turns on `POST /mesh/v1/infer` and adds
`models` to the card. With no `--serve-model` the route gives 404. `--infer-slots N` (1..4,
default 1) sets the parallel upstream calls. Upstream: `APEX_MESH_OLLAMA` (default
`http://127.0.0.1:11434`), path `/v1/chat/completions`, `stream: false`, proxy-free opener,
timeout 600 s. `mesh up` refuses (exit 2) a bad model name, a bad `--infer-slots`, a bad
`APEX_MESH_OLLAMA`, or a card that does not fit in one beacon datagram.

### Request

`POST /mesh/v1/infer` (exact path, no query). Server check order:

| Step | Check | Fail |
|---|---|---|
| 1 | `X-Apex-Mesh` header, BEFORE any body byte: MAC, skew, kind `auth`, body keys exactly `{path, to, body_sha256}`, `path` = `/mesh/v1/infer`, `to` = own node_id, `body_sha256` = 64 hex | 401 `unauthorized` |
| 2 | `Transfer-Encoding` present or no `Content-Length` | 411 `length_required` |
| 3 | `Content-Length` not one decimal value | 400 `bad_length` |
| 4 | `Content-Length` > 256 KiB | 413 `too_large` |
| 5 | Body read under its own absolute 10 s deadline | 408 `body_timeout` (or the connection closes) |
| 6 | sha256(body) = `body_sha256` (constant-time compare) | 401 `unauthorized` |
| 7 | Replay guard on the auth nonce | 401 `unauthorized` |
| 8 | JSON body (see below) | 400 `bad_json`, `bad_request`, `bad_model`, `bad_messages`, `bad_max_tokens`, `bad_temperature` |
| 9 | `model` in the served set | 403 `not_served` |
| 10 | A free slot (non-blocking semaphore) | 429 `busy`, at once |
| 11 | Upstream call | 502 `upstream_unreachable`, `upstream_timeout`, `upstream_http_<code>`, `upstream_too_large`, `upstream_bad_response` |

The hash check runs before the replay check. So a body that does not match does not use up the
nonce of the real request. A GET auth header (body `{path, to}`) never opens the POST route, and
a POST auth header never opens a GET route.

Body (no other keys):

```
{"model": "<model name>", "messages": [{"role": "system|user|assistant", "content": "<str>"}, ...],
 "max_tokens": <int 1..8192>, "temperature": <number 0..2, optional>}
```

`messages` has 1..64 items. Each item has exactly the keys `role` and `content`.

### Response

Signed envelope kind `infer`, body:

```
{"node_id": "<server node_id>", "model": "<model>", "content": "<str>",
 "reasoning": "<str>|null", "finish_reason": "<str>|null",
 "usage": {"prompt_tokens"?, "completion_tokens"?, "total_tokens"?}, "latency_s": <float>}
```

`reasoning` comes from `message.reasoning` (or `message.reasoning_content`). `usage` keeps only
the three int keys. `latency_s` is the upstream time on the server. Errors are plain JSON
`{"error": "<code>"}`, never a traceback.

While a model call runs, the card `load` is the agent load plus the number of running calls.

### Client (`infer.py`)

```python
def ask(peer: Peer, ident: Identity, model: str, messages: list, max_tokens: int,
        temperature: float | None = None, timeout: float = 600, *, opener=None) -> dict
    # never raises. Returns {ok, node_id, name, model, content, reasoning, finish_reason,
    #   usage, latency_s, error?}. Checks the request with the server rules first.
    # Verifies MAC, skew, response replay, kind "infer", body.node_id == peer.node_id, model.
```

`mesh ask` reads `peers.json`, keeps alive nodes whose card serves `--model` (self too, over
HTTP), keeps only `--node` (name or node_id) when given, and picks min `(load, node_id)`. It does
not try a second node when the first one fails. The socket is pinned to the LAN interface of the
running `mesh up` (same as `collect`). Exit 0 = ok, 1 = no node or the call failed, 2 = usage.
`PROMPT` `-` reads stdin. Text mode prints the content on stdout and one status line on stderr.

## Known limits (v1)

| Limit | Effect | Mitigation |
|---|---|---|
| Plain HTTP, signed but not encrypted | A host that sees LAN traffic can read served aggregates. | Serve aggregates only. Use `--share` to limit them. |
| Pre-auth connection flood | A host that reconnects in a loop can fill the 32 slots for 5 s at a time. | Absolute 5 s header deadline; slots always recover. Use a trusted LAN. |
| One beacon port per host | Two `mesh up` on one host need different UDP ports. | One node per machine; `mesh.lock` refuses a second `up`. |
| Wire change in auth body (`to`) | Pre-release nodes without `to` get 401. | Upgrade all nodes together. |
| Unix only (`fcntl`) | `mesh up` / `mesh status` do not run on Windows. | — |
| Model calls: prompts and answers cross the LAN in plain text, signed but not encrypted | A host that sees LAN traffic can read every prompt and answer. | Use model calls only on a trusted LAN. Do not send secrets in a prompt. |
| Model calls: any key holder can use the GPU | Every node with `mesh.key` can call a served model, up to `--infer-slots` at a time. | Serve only on a trusted mesh. Keep `--infer-slots` low. |
| Model calls: no cancel | When the client goes away, the upstream call runs to its end (max 600 s) and holds its slot. | `--infer-slots` > 1, or restart `mesh up`. |
| Model calls: one node per `ask` | `mesh ask` does not fail over to the next node. | Run `ask` again, or use `--node`. |
| Wire change in auth body (`body_sha256`) | Only `POST /infer` uses it; GET auth is unchanged. Old nodes have no `/infer` (501). | Upgrade nodes that serve or ask. |
