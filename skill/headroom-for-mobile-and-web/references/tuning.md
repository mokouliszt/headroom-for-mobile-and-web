# Tuning and internals

Read this when a default is getting in the way.

## Contents

- [Thresholds](#thresholds)
- [Profiles](#profiles)
- [Aggressive mode](#aggressive-mode)
- [Workspace layout](#workspace-layout)
- [How the fidelity check works](#how-the-fidelity-check-works)
- [Why source code does not compress here](#why-source-code-does-not-compress-here)
- [Troubleshooting](#troubleshooting)

## Thresholds

`compress` rejects its own result when it is not worth using:

| flag | default | meaning |
|---|---|---|
| `--min-ratio` | `0.15` | reject below this saving |
| `--min-fidelity` | `0.95` | reject below this share of sampled values retained |

Rejection still stores the entry and reports the numbers; it just marks the
result as not recommended. Lower `--min-ratio` when even a modest saving matters
on a very large file. Raise `--min-fidelity` to `1.0` when every record must be
exact and you would rather read the original than a near-miss.

`--no-store` measures without writing anything -- useful for deciding whether
compression is worth it at all, though you then have no original to retrieve
from beyond the source file itself.

## Profiles

`--profile` selects one of Headroom's bundled savings profiles.

| profile | behavior |
|---|---|
| *(default, unset)* | protections on, no target ratio -- safest |
| `general` | compress assistant/system content only |
| `coding` | compresses user content, low minimum threshold |
| `balanced` | targets ~30% reduction, keeps recent turns protected |
| `agent-90` | targets ~90% reduction; expect `lossy` verdicts |

`agent-90` exists for agent transcripts, not for data you intend to read
carefully. On a records file it will usually trip the fidelity floor, which is
the correct outcome.

## Aggressive mode

`--aggressive` disables Headroom's content protections (`protect_recent=0`,
`protect_analysis_context=False`) and lowers the minimum size a block must reach
before it is considered. Those protections exist to stop the pipeline mangling
content the model is actively working with. Turning them off buys a few points
on stubborn files and raises the chance of a `lossy` verdict.

Reach for it only after a normal run came back `no-op` on a file you are
confident is compressible -- and check the fidelity line afterwards.

## Workspace layout

Default `.headroom/` in the working directory; override with `--workspace` or
`$HR_WORKSPACE`.

```
.headroom/
├── manifest.json      # one entry per compression: ids, metrics, verdicts
├── originals/         # exact source text, the retrieval target
└── compressed/        # the compressed rendering
```

`manifest.json` is the session's whole record; `stats` just renders it. The
workspace dies with the sandbox, which is the point -- it is a session-scoped
stand-in for the CCR store that the proxy would normally provide.

Delete `.headroom/` to reset. Nothing outside it is ever written.

## How the fidelity check works

The problem: `compression_ratio` cannot distinguish a lossless restructure from
a sampler that threw away 80% of the rows. Both look like a big number.

The check:

1. Extract atoms from the original -- runs of 3+ characters from
   `[A-Za-z0-9_.:/@-]`. Length 3 is deliberate: it captures the `784` in
   `id=784`, and those per-record identifiers are the first casualty of row
   sampling.
2. Prefer atoms appearing once or twice. Rare values are what a sampler
   destroys; boilerplate survives any transform and would inflate the score.
   If the rare pool is too small to sample from (common in logs, where every
   line shares a vocabulary), top it up with frequent atoms and flag the result
   as low confidence rather than reporting a meaningless 100%.
3. Sample up to 300 and test membership against the atom *set* of the compressed
   text. Set membership rather than substring search, for two reasons: lossless
   routes reshape the syntax around a value, so a substring test would report
   prefix factoring as data loss; and a short atom like `784` would spuriously
   match inside `17840`.

Measured behavior on controls: identical text scores 100%; dropping 3% of
records scores ~97%; keeping 15 of 400 records scores ~5%.

It is a sampling estimate, not a proof. A 100% score means no sampled value was
lost, which is strong evidence but not a guarantee about the unsampled
remainder. For anything that must be exact, retrieve from the original.

## Why source code does not compress here

This is not a proxy-vs-library gap and not something a retry or a wait fixes.
Headroom's own router ships with AST-aware code compression turned off by
default, in code, with an explicit maintainer comment: `enable_code_aware:
bool = False  # Disabled: use code graph MCP tools instead`. Detection works
fine (Python, C#, and TypeScript all classify as `source_code` with reasonable
confidence); the router deliberately declines to act on it and passes code
through unmangled. That comment names the intended alternative: a code-graph
MCP tool such as Serena. On a surface where the only MCP servers reachable are
Anthropic's hosted connector directory -- no locally-run server like Serena is
an option -- that intended alternative does not exist, but the workaround below
still should not be used.

**The flag is reachable without touching any installed file.** Bypassing
`headroom.compress()` and calling the lower-level `ContentRouter` directly with
`ContentRouterConfig(enable_code_aware=True)` does turn on AST compression --
confirmed against real output, not just documentation. But measuring it is why
this skill leaves it off:

| language | tokens saved | fidelity (rare-value retention) |
|---|---|---|
| Python (stdlib module) | 85.7% | **47.7%** |
| C# (synthetic classes) | 52.5% | **87.3%** |
| TypeScript (synthetic handlers) | 55.0% | 100% |

The low fidelity is not a bug to route around -- it is what the feature does.
`CodeStructureHandler` preserves imports, signatures, and class/interface
declarations, and treats **function and method bodies as compressible**. That
is a skeleton extractor for getting the shape of an unfamiliar file, not a
size reduction that keeps the content readable. Turning it on and feeding the
result into the same accept/reject loop this skill uses for JSON and logs
would be actively misleading: a `compress` verdict implies "safe to reason
from," and a rendering with half its logic missing is not that, no matter what
the fidelity number says once you know what it is measuring.

**Left off on purpose. Do not flip this default** without also building a
separate code path that is labeled for what it actually is -- a structure-only
outline, kept out of the fidelity-gated `compress`/`retrieve` loop, with the
dropped-bodies caveat surfaced every time it is used. Nobody asked for that
mode here, so it does not exist in this skill. If a future session is tempted
to "fix" the no-op by flipping `enable_code_aware`, don't -- read the file
directly instead.

## Troubleshooting

**Install fails.** The script tries `pip --break-system-packages`, plain `pip`,
then `uv pip --system`. If all three fail the sandbox has no network access;
there is no offline fallback, so use targeted reads instead.

**Everything comes back `no-op`.** Usually source code or prose. Check `scan`'s
verdict column -- it predicts this before you spend a run on it.

**Ratio is high but fidelity is low.** A sampler ran. Do not read the compressed
form as if it were the data. Retrieve the records you actually need, or read the
original.

**Fidelity says LOW CONFIDENCE.** Fewer than 60 distinct atoms were available to
sample, so the percentage is not meaningful. Treat the file as unverified.

**`retrieve` says the original is gone.** The workspace was cleared or the
session restarted. The manifest falls back to the recorded source path, so if
that file still exists retrieval still works; otherwise re-run `compress`.
