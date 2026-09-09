---
name: headroom-for-mobile-and-web
description: Shrink large files before they enter the context window, using the Headroom compression library as a plain Python call - no proxy, no MCP server, no API key. Built for sandboxed chat surfaces (Claude mobile and web, ChatGPT Work, and any agent with a scratch shell) where a persistent proxy cannot run. Use this whenever a JSON/NDJSON/CSV payload, an API response dump, a log file, or any other machine-generated file is too big to read comfortably, whenever reading a file would eat a large share of the budget, and whenever the user mentions token savings, context limits, compression, Headroom, or says a file is "too big" - in English or in Japanese (トークン節約, コンテキスト圧迫, 圧縮, 大きすぎるファイル, ログが長い). Always measure with `scan` before deciding, and never trust a compressed rendering whose reported fidelity is below 100%.
license: MIT
---

# Headroom for mobile and web

Sandboxed chat surfaces cannot run Headroom the way its documentation assumes.
There is no persistent proxy to route traffic through, no MCP server the host
will keep alive, and no CCR store that survives the session. What they do have
is a shell and a filesystem, and the compression pipeline itself is an ordinary
Python function.

This skill uses that function directly, and rebuilds the part that makes
compression safe -- the ability to get the original back -- out of the one thing
the sandbox does provide: files on disk.

## The loop

**Compress → keep → retrieve.** Never read a large machine-generated file
straight into context. Compress it, read the compressed rendering, and pull
exact detail back from the stored original when a specific value actually
matters. The original never leaves the sandbox, so retrieval is always exact.

```bash
S=path/to/skills/headroom-for-mobile-and-web/scripts/hr.py

python3 $S scan .                        # what is big, and what is worth compressing
python3 $S compress data/response.json   # measure savings + fidelity, store both copies
python3 $S retrieve <id> --grep ERROR    # exact lines from the original, on demand
python3 $S stats                         # what this session has saved so far
```

`compress` installs `headroom-ai` on first use (~30-60s, needs network). Every
command takes `--json` for machine-readable output, before or after the
subcommand.

## Start with scan, not compress

`scan` costs nothing and prevents the two ways this skill wastes time: running
compression on something that will not compress, and compressing something small
enough to just read. It prints a token estimate per file and a verdict.

Do not compress a file under ~2,000 tokens. The round trip through the
workspace costs more attention than the tokens it saves.

## Reading the compress output

```
tokens       10,980 -> 4,699   (saved 6,281, 57.2%)
fidelity     100.0% of 300 sampled values retained  [lossless]
transforms   router:smart_crusher:0.35
```

**`fidelity` is the number that matters, not the ratio.** Headroom routes
content to different compressors: some restructure losslessly (a schema header
followed by bare rows, or factoring a shared prefix out of every log line), and
some drop rows outright. A high ratio tells you nothing about which one ran.

The fidelity check samples rare values from the original -- per-record ids,
one-off error strings, outlier numbers -- and verifies they survived. Those are
exactly what a sampler destroys first.

| verdict | meaning | what to do |
|---|---|---|
| `lossless` | every sampled value survived | read the compressed form freely |
| `near-lossless` | ≥95% survived | fine for shape and trends; retrieve before quoting a specific record |
| `lossy` | <95% survived | do not reason about individual records from it; retrieve, or read the original |
| `no-op` | nothing compressed | read the original; there is nothing to gain here |

A result is auto-rejected when savings fall below `--min-ratio` (default 15%) or
fidelity below `--min-fidelity` (default 95%). A rejection is a useful answer,
not a failure -- it means read the file directly.

Report the fidelity verdict to the user when you rely on a compressed rendering
for anything they might act on. Silently reasoning from sampled data is the one
way this skill can actively mislead.

## What compresses, and what does not

- **Structured records compress well.** JSON arrays of objects, NDJSON, CSV-ish
  tables, API response dumps. Roughly 40-60%, usually lossless, because the
  compressor factors out the repeated schema.
- **Repetitive logs compress modestly.** Often 15-30% by factoring shared
  prefixes. Frequently below the accept threshold, which is the correct outcome.
- **Source code is a no-op here, by design, not by omission.** Headroom's AST
  compressor strips function and method bodies to produce a structure-only
  skeleton -- it is not a safe size reduction, and this skill does not use it.
  Read the relevant range or grep for the symbol instead. That is both cheaper
  and lossless. See "Why source code does not compress here" in
  `references/tuning.md` before touching this -- there is a reachable internal
  switch that looks like a fix and is not one.
- **Prose gains nothing.** Short or already-dense text comes back unchanged.

If a file does not fall in the first category, check `scan` first and expect a
rejection.

## Retrieving

Retrieval reads the stored original, never the compressed copy, so it is always
exact. Minified JSON is pretty-printed first, so line numbers and grep work on a
single-line payload.

```bash
python3 $S retrieve <id> --grep "NullReference" --context 3
python3 $S retrieve <id> --grep "id=4\d\d" --regex
python3 $S retrieve report.json --json-index 67     # element 67 of a JSON array
python3 $S retrieve service.log --lines 500-520
```

`<id>` is the short id from `compress`, but a path or filename works too. Long
lines are clipped, and grep stops after `--max-hits` matches, so a broad pattern
cannot flood the context it was meant to protect.

## When not to use this skill

- The file is small, or you need only its first few lines -- just read it.
- You need a specific known value -- `grep` the original directly.
- The file is source code -- read the relevant range.
- There is no network access on first use -- `headroom-ai` cannot install, and
  the script says so rather than failing quietly. Fall back to targeted reads.

Compression is worth its indirection when you must reason across a whole large
payload. For anything narrower, a targeted read wins on both cost and fidelity.

## Reference

`references/tuning.md` covers the profile flags, aggressive mode, threshold
tuning, the workspace layout, and how the fidelity check is built. Read it when
a default is getting in the way, not before.
