# Studio icons

Sprite: `src/apex_router/studio/icons.svg`. Review sheet: `sheet.html` (open it in a browser).
Test: `tests/test_studio_icons.py`.

## Shared rules

1. Use one round badge for each icon. The badge fill is `#24283b` and the edge is `#9096b5`.
2. Draw a chibi head with big dark eyes and a small smile. Do not add fine lines.
3. Use `viewBox="0 0 32 32"`. Keep each symbol at 1.5 KB or less.
4. Use 6 colours or fewer in each symbol. Skin `#f5d0c5` and white count as colours.
5. Use presentation attributes only (`fill`, `stroke`). The page CSP blocks `style=` and external files.
6. Give each symbol a `<title>` with a plain name.
7. Do not show state in the icon. The UI draws a ring (amber or red) around the badge.
8. Draw original characters only. Do not copy a character, mascot or logo.

Reason: one badge and one face make a set. At 20 px, only the silhouette and one accent stay clear.
The edge keeps the badge visible on `#1a1b26` and on `#24283b`.

## Rule for each group

| Group | Mark | Reason |
|---|---|---|
| Agents | Hair colour is the agent hue. The hair shape is different too. | The hue matches the agent line colour. The shape helps users who cannot see the hue. |
| Roles | Grey hair. One hat or tool in amber. | Grey hair keeps the roles neutral. The tool tells the job. |
| Models, Claude family | Lavender hair and an amber bow. Hair length shows the size. | Same base, so the family is clear. Long hair is the big model. |
| Models, GPT family | Cyan spiky hair and a dark headband. The emblem shows the version. | A different base keeps the two families apart. |
| Models, local | Pale hair and amber headphones. | Headphones mean "runs here, on this machine". |
| Processes | A small robot or a spirit, not a person. Cyan eyes. One amber mark. | A process is not a person. The robot face keeps the chibi style. |

## Reason for each icon

| id | Mark | Reason |
|---|---|---|
| `agent-pi` | blue hair, one curl on top | Short and quick, like the pi agent. |
| `agent-claude` | lavender hair, two buns | Two buns give a clear outline at 20 px. |
| `agent-codex` | cyan hair, side ponytail | The ponytail breaks the circle on one side. |
| `role-worker` | amber hard hat | A worker builds the change. |
| `role-xval` | amber monocle with a chain | A second, different eye: a reviewer from another vendor. |
| `role-scout` | pale cap with a long peak | A scout looks ahead. |
| `role-reviewer` | amber magnifier | A reviewer looks closely at the change. |
| `role-coordinator` | amber baton | A coordinator gives the timing to the team. |
| `model-opus` | Claude base, long hair | Long hair is the largest Claude model. |
| `model-sonnet` | Claude base, short hair | Short hair is the medium Claude model. |
| `model-gpt` | GPT base, star on the headband | The star is for "astra". |
| `model-gpt-sol` | GPT base, sun at the top right | The sun is for "sol". It also changes the outline. |
| `model-local` | headphones | The model runs on the local machine. |
| `proc-serve` | robot, antenna, arrows on both sides | The relay sends traffic both ways on :8788. |
| `proc-local-proxy` | robot with a roof hat | The roof means "local". The proxy is on :8789. |
| `proc-collect` | robot behind a full basket | Collect gathers records. |
| `proc-daily` | alarm-clock robot | Daily runs once each day. |
| `proc-snapshot` | camera robot | Snapshot shows the state in the menu bar. |
| `proc-mesh` | robot with three linked nodes | Mesh links the workers. |
| `proc-keepwarm` | flame spirit | Keep-warm keeps sign-in and models warm. |
| `proc-drain` | robot with a funnel hat and a down arrow | Drain empties the queue. |
| `proc-studio` | monitor robot with a beret | The studio is the screen for the owner. |

## Use in the UI

`<svg width="20" height="20"><use href="#agent-pi"/></svg>`. Inline the sprite once in the page.
