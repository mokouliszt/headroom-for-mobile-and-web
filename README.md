# headroom-for-mobile-and-web

[日本語版 README](./README.ja.md)

An Agent Skill that shrinks large files before they enter the context window,
using the [Headroom](https://github.com/headroomlabs-ai/headroom) compression
library as a plain Python call — no proxy, no MCP server, no API key.

Built for sandboxed chat surfaces (Claude mobile and web, ChatGPT Work, and any
agent with a scratch shell) where Headroom's usual deployment modes cannot run.

## The problem

Headroom is normally deployed as an HTTP proxy or an MCP server. Neither is
available in a sandboxed chat surface: there is no persistent process, and the
filesystem is wiped between sessions. So the CCR store that makes Headroom's
compression *reversible* — the thing that lets a model ask for the original when
the summary is not enough — has nowhere to live.

But the compression pipeline itself is an ordinary Python function, and the
sandbox does have a shell and a filesystem.

## The approach

**Compress → keep → retrieve.** Compress the file, read the compressed
rendering, and pull exact detail back from the stored original whenever a
specific value matters. The original never leaves the sandbox, so retrieval is
exact by construction. It is the CCR pattern rebuilt out of files on disk.

On top of that, every compression is **verified**. Headroom routes content to
different compressors: some restructure losslessly, some drop rows. The
compression ratio cannot tell you which one ran. This skill measures it, by
sampling rare values from the original and checking they survived — and refuses
its own result when they did not.

## Repository layout

```
headroom-for-mobile-and-web/
├── README.md          this file
├── README.ja.md
├── LICENSE
└── skill/
    └── headroom-for-mobile-and-web/   the installable skill — copy or package this directory
        ├── SKILL.md
        ├── scripts/hr.py
        └── references/tuning.md
```

## Install

Copy `skill/headroom-for-mobile-and-web/` into your agent's skills directory,
or package that same directory as a `.skill` bundle where the surface supports
uploading one. `headroom-ai` installs itself on first use (requires network).

## Usage

```bash
S=path/to/headroom-for-mobile-and-web/scripts/hr.py

python3 $S scan .                        # what is big, and what is worth compressing
python3 $S compress data/response.json   # measure savings + fidelity, store both copies
python3 $S retrieve <id> --grep ERROR    # exact lines from the original
python3 $S stats                         # cumulative savings
```

```
$ python3 $S compress quotes.json
id           0a2d4b17d7
source       quotes.json
tokens       10,980 -> 4,699   (saved 6,281, 57.2%)
fidelity     100.0% of 300 sampled values retained  [lossless]
transforms   router:smart_crusher:0.35
```

Every command accepts `--json`, before or after the subcommand.

## Reading the result

`fidelity` matters more than the ratio. It samples rare values — per-record ids,
one-off error strings, outliers — and checks they survived compression. Those
are the first casualties of a sampler.

| verdict | meaning |
|---|---|
| `lossless` | every sampled value survived; read the compressed form freely |
| `near-lossless` | ≥95% survived; fine for shape, retrieve before quoting a record |
| `lossy` | <95% survived; do not reason about individual records from it |
| `no-op` | nothing compressed; read the original |

Results below `--min-ratio` (default 15%) or `--min-fidelity` (default 95%) are
auto-rejected. A rejection is a useful answer: it means read the file directly.

## What compresses

| content | typical result |
|---|---|
| JSON arrays of objects, NDJSON, CSV-ish tables | 40–60%, usually lossless |
| repetitive logs | 15–30%, often below the accept threshold |
| source code | no-op — read the relevant range instead |
| prose | no-op |

Source code is a no-op here by design, not by omission. Headroom's AST
compressor strips function and method bodies to produce a structure-only
skeleton, not a safe size reduction — an internal switch exists to turn it on,
and this skill deliberately does not use it. See
[`references/tuning.md`](./references/tuning.md) before touching that switch.

## Commands

| command | purpose |
|---|---|
| `scan PATH...` | token estimate per file, with a verdict on whether to compress |
| `compress FILE` | compress, measure savings and fidelity, store both copies |
| `retrieve REF` | exact content from the stored original (`--grep`, `--lines`, `--json-index`) |
| `stats` | cumulative savings for the workspace |
| `ensure` | install `headroom-ai` explicitly |

Retrieval pretty-prints minified JSON first, so line numbers and grep work on a
single-line payload. Long lines are clipped and matches are capped, so a broad
pattern cannot flood the context it was meant to protect.

## Requirements

Python 3.9+, network access on first use. No API key, no account, no external
service — `headroom-ai` runs entirely locally and nothing leaves the sandbox.

## License

MIT. See [LICENSE](./LICENSE).

Headroom itself is a separate project by Headroom Labs, distributed under
Apache-2.0. This skill only calls its public Python API.
