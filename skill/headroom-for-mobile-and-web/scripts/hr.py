#!/usr/bin/env python3
"""hr.py - Headroom for sandboxed chat agents.

Shrink a large file before it enters the model's context, keep the original on
disk, and pull exact detail back on demand.

Subcommands:
    ensure     Install headroom-ai if it is missing.
    scan       Estimate token cost of files; say what is worth compressing.
    compress   Compress a file into the workspace; report savings and fidelity.
    retrieve   Pull exact content back out of the stored original.
    stats      Cumulative savings for this workspace.

Every command accepts --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

WORKSPACE_ENV = "HR_WORKSPACE"
DEFAULT_WORKSPACE = ".headroom"
MANIFEST_NAME = "manifest.json"

# Extensions where library-mode Headroom reliably pays off. See references/tuning.md.
STRUCTURED_EXT = {".json", ".ndjson", ".jsonl", ".csv", ".tsv", ".log", ".txt", ".out", ".err", ".xml", ".yaml", ".yml"}
CODE_EXT = {
    ".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".rb", ".php", ".kt", ".swift", ".scala", ".sh", ".ps1",
}


# --------------------------------------------------------------------------
# dependency handling
# --------------------------------------------------------------------------

def _pip_install(spec: str) -> tuple[bool, str]:
    """Try the install strategies that work across sandboxes, in order."""
    attempts = [
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", spec],
        [sys.executable, "-m", "pip", "install", "-q", spec],
        ["uv", "pip", "install", "--system", "-q", spec],
    ]
    last = ""
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            last = f"{cmd[0]}: {exc}"
            continue
        if proc.returncode == 0:
            return True, " ".join(cmd)
        last = (proc.stderr or proc.stdout or "").strip()[-500:]
    return False, last


def ensure_headroom(with_code: bool = False, quiet: bool = False) -> None:
    """Import headroom, installing it first if necessary.

    The sandbox is wiped between sessions, so this runs on every fresh session.
    It is a no-op once the package is present.
    """
    try:
        import headroom  # noqa: F401
    except ImportError:
        spec = "headroom-ai[code]" if with_code else "headroom-ai"
        if not quiet:
            print(f"installing {spec} (one-time, ~30-60s)...", file=sys.stderr)
        ok, detail = _pip_install(spec)
        if not ok:
            sys.exit(
                f"could not install headroom-ai.\n{detail}\n"
                "If this sandbox has no network access, this skill cannot run; "
                "fall back to targeted reads (head/tail/grep/jq) instead."
            )
        try:
            import headroom  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            sys.exit(f"headroom-ai installed but not importable: {exc}")


# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------

def workspace_dir(explicit: str | None = None) -> Path:
    root = Path(explicit or os.environ.get(WORKSPACE_ENV) or DEFAULT_WORKSPACE)
    (root / "originals").mkdir(parents=True, exist_ok=True)
    (root / "compressed").mkdir(parents=True, exist_ok=True)
    return root


def load_manifest(ws: Path) -> dict:
    path = ws / MANIFEST_NAME
    if not path.exists():
        return {"entries": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"entries": []}


def save_manifest(ws: Path, data: dict) -> None:
    (ws / MANIFEST_NAME).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_entry(ws: Path, ref: str) -> dict | None:
    """Look up a stored file by entry id, exact path, or basename."""
    entries = load_manifest(ws)["entries"]
    for e in entries:
        if e["id"] == ref:
            return e
    for e in entries:
        if e["source"] == ref or Path(e["source"]).name == Path(ref).name:
            return e
    return None


# --------------------------------------------------------------------------
# token counting and fidelity
# --------------------------------------------------------------------------

def count_tokens(text: str, model: str) -> int:
    from headroom import count_tokens_text

    try:
        return int(count_tokens_text(text, model=model))
    except Exception:
        return max(1, len(text) // 4)


# Length 3 so that per-record identifiers -- the "784" in "id=784" -- become
# atoms. Those are exactly the values a row sampler destroys, and a longer
# minimum silently misses them.
_ATOM_RE = re.compile(r"[A-Za-z0-9_.:/@-]{3,}")

MIN_CONFIDENT_SAMPLE = 60


def fidelity_sample(original: str, compressed: str, sample_size: int = 300, seed: int = 0) -> dict:
    """Estimate how much of the original survived compression.

    Some Headroom routes restructure losslessly (a schema header plus bare rows,
    or shared-prefix factoring); others drop rows outright. The ratio alone
    cannot tell you which one ran, so measure it: sample atoms from the
    original -- rare ones first, since a sampler destroys those before it
    touches boilerplate -- and check they survived.

    Membership is tested against the atom *set* of the compressed text, not by
    substring search. Lossless routes reshape the syntax around a value, so a
    substring test would report prefix factoring as data loss; and a short atom
    like "784" would spuriously match inside "17840".
    """
    atoms = _ATOM_RE.findall(original)
    if not atoms:
        return {"coverage": 1.0, "sampled": 0, "confident": False,
                "missing_examples": [], "note": "no comparable atoms"}

    freq: dict[str, int] = {}
    for a in atoms:
        freq[a] = freq.get(a, 0) + 1

    rare = [a for a, n in freq.items() if n <= 2]
    pool = list(rare)
    if len(pool) < sample_size:  # a tiny pool must not be able to fake 100%
        pool.extend(a for a, n in freq.items() if n > 2)
    pool = list(dict.fromkeys(pool))

    present = set(_ATOM_RE.findall(compressed))
    rng = random.Random(seed)
    sample = rng.sample(pool, min(sample_size, len(pool)))
    missing = [a for a in sample if a not in present]
    coverage = 1.0 - (len(missing) / len(sample))
    return {
        "coverage": round(coverage, 4),
        "sampled": len(sample),
        "confident": len(sample) >= MIN_CONFIDENT_SAMPLE,
        "rare_pool": len(rare),
        "missing_count": len(missing),
        "missing_examples": [m[:80] for m in missing[:8]],
    }


def classify(ratio: float, coverage: float) -> str:
    if ratio < 0.05:
        return "no-op"
    if coverage >= 0.995:
        return "lossless"
    if coverage >= 0.95:
        return "near-lossless"
    return "lossy"


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

@dataclass
class ScanRow:
    path: str
    bytes: int
    tokens: int
    kind: str
    verdict: str


def verdict_for(path: Path, tokens: int) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext in CODE_EXT:
        kind = "code"
    elif ext in STRUCTURED_EXT:
        kind = "structured"
    else:
        kind = "other"

    if tokens < 2000:
        return kind, "skip (small - just read it)"
    if kind == "code":
        return kind, "skip (library mode rarely compresses source; read targeted ranges instead)"
    if kind == "structured":
        return kind, "compress"
    return kind, "try (measure before trusting)"


def cmd_scan(args: argparse.Namespace) -> int:
    ensure_headroom(quiet=args.json)
    rows: list[ScanRow] = []
    targets: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_dir():
            targets.extend(sorted(q for q in p.rglob("*") if q.is_file()))
        elif p.is_file():
            targets.append(p)

    for p in targets:
        try:
            size = p.stat().st_size
            if size > args.max_bytes:
                rows.append(ScanRow(str(p), size, -1, "large", f"skip (>{args.max_bytes} bytes)"))
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tokens = count_tokens(text, args.model)
        kind, verdict = verdict_for(p, tokens)
        rows.append(ScanRow(str(p), size, tokens, kind, verdict))

    rows.sort(key=lambda r: r.tokens, reverse=True)
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print("no readable files found")
        return 0
    print(f"{'tokens':>9}  {'bytes':>9}  {'kind':<10}  path")
    for r in rows:
        tok = "-" if r.tokens < 0 else f"{r.tokens:,}"
        print(f"{tok:>9}  {r.bytes:>9,}  {r.kind:<10}  {r.path}")
    print()
    total = sum(r.tokens for r in rows if r.tokens > 0)
    print(f"total ~{total:,} tokens across {len(rows)} file(s)")
    worth = [r for r in rows if r.verdict == "compress"]
    if worth:
        print("\nworth compressing:")
        for r in worth:
            print(f"  {r.path}  (~{r.tokens:,} tokens)")
    return 0


# --------------------------------------------------------------------------
# compress
# --------------------------------------------------------------------------

def compress_text(text: str, model: str, profile: str, aggressive: bool) -> tuple[str, list[str]]:
    """Run text through Headroom's pipeline as a single user message."""
    from headroom import compress

    kwargs: dict = {"compress_user_messages": True}
    if aggressive:
        kwargs.update(protect_recent=0, protect_analysis_context=False, min_tokens_to_compress=120)
    else:
        kwargs.update(protect_recent=0, protect_analysis_context=True)
    if profile:
        kwargs["savings_profile"] = profile

    result = compress([{"role": "user", "content": text}], model=model, **kwargs)
    out = result.messages[0]["content"]
    if isinstance(out, list):  # block-style content
        out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return _unquote(out), list(result.transforms_applied)


def _unquote(text: str) -> str:
    """Undo JSON string quoting that some routes leave on their output.

    The structured routes can hand back a quoted literal whose newlines are the
    two characters backslash-n. Left alone it reads as one enormous line and
    every value ends up glued to an escape, so decode it back to real text.
    """
    s = text.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"' and "\\n" in s:
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
    return text


def cmd_compress(args: argparse.Namespace) -> int:
    ensure_headroom(quiet=args.json)
    src = Path(args.file)
    if not src.is_file():
        sys.exit(f"not a file: {src}")

    text = src.read_text(encoding="utf-8", errors="replace")
    ws = workspace_dir(args.workspace)

    before = count_tokens(text, args.model)
    compressed, transforms = compress_text(text, args.model, args.profile, args.aggressive)
    after = count_tokens(compressed, args.model)
    ratio = 0.0 if before == 0 else max(0.0, 1.0 - after / before)

    fid = fidelity_sample(text, compressed)
    verdict = classify(ratio, fid["coverage"])

    # A compression that keeps most of the tokens is not worth the indirection,
    # and one that quietly drops rare values is worse than not compressing.
    rejected = None
    if ratio < args.min_ratio:
        rejected = f"savings {ratio:.1%} below --min-ratio {args.min_ratio:.0%}"
    elif fid["coverage"] < args.min_fidelity:
        rejected = f"fidelity {fid['coverage']:.1%} below --min-fidelity {args.min_fidelity:.0%}"

    entry_id = hashlib.sha256(f"{src.resolve()}:{time.time()}".encode()).hexdigest()[:10]
    stored_original = ws / "originals" / f"{entry_id}{src.suffix or '.txt'}"
    stored_compressed = ws / "compressed" / f"{entry_id}.txt"

    if not args.no_store:
        stored_original.write_text(text, encoding="utf-8")
        stored_compressed.write_text(compressed, encoding="utf-8")

    entry = {
        "id": entry_id,
        "source": str(src.resolve()),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": args.model,
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": before - after,
        "ratio": round(ratio, 4),
        "transforms": transforms,
        "fidelity": fid,
        "verdict": verdict,
        "accepted": rejected is None,
        "rejected_reason": rejected,
        "original_path": str(stored_original) if not args.no_store else None,
        "compressed_path": str(stored_compressed) if not args.no_store else None,
        "lines": text.count("\n") + 1,
    }
    if not args.no_store:
        data = load_manifest(ws)
        data["entries"].append(entry)
        save_manifest(ws, data)

    if args.json:
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0

    print(f"id           {entry_id}")
    print(f"source       {src}")
    print(f"tokens       {before:,} -> {after:,}   (saved {before - after:,}, {ratio:.1%})")
    conf = "" if fid.get("confident", True) else "  (LOW CONFIDENCE - small sample)"
    print(f"fidelity     {fid['coverage']:.1%} of {fid['sampled']} sampled values retained  [{verdict}]{conf}")
    if fid.get("missing_examples"):
        print(f"  dropped e.g. {', '.join(fid['missing_examples'][:5])}")
    print(f"transforms   {', '.join(transforms) or 'none'}")
    if rejected:
        print(f"\nNOT RECOMMENDED: {rejected}")
        print("Read the original directly, or use targeted reads (head/tail/grep/jq).")
        return 0
    print(f"compressed   {stored_compressed}" if not args.no_store else "")
    print(f"original     {stored_original}" if not args.no_store else "")
    print(f"\nRetrieve exact detail with:  hr.py retrieve {entry_id} --grep PATTERN")
    if args.emit:
        print("\n----- BEGIN COMPRESSED -----")
        print(compressed)
        print("----- END COMPRESSED -----")
    return 0


# --------------------------------------------------------------------------
# retrieve
# --------------------------------------------------------------------------

MAX_LINE_CHARS = 400


def _clip(line: str) -> str:
    """Keep one pathological line from flooding the context window."""
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]} ... [+{len(line) - MAX_LINE_CHARS} chars]"


def _as_lines(text: str) -> list[str]:
    """Split into addressable lines, pretty-printing minified JSON first.

    Machine-written JSON is often a single line holding the entire payload.
    Line ranges and grep are meaningless against that -- one match returns the
    whole file -- so give it real structure before addressing it.
    """
    lines = text.splitlines()
    if len(lines) > 4:
        return lines
    stripped = text.lstrip()
    if not stripped[:1] in ("{", "["):
        return lines
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False).splitlines()
    except (json.JSONDecodeError, RecursionError):
        return lines


def cmd_retrieve(args: argparse.Namespace) -> int:
    ws = workspace_dir(args.workspace)
    entry = find_entry(ws, args.ref)
    if entry is None:
        sys.exit(f"no stored entry matching {args.ref!r}. Run: hr.py stats")
    origin = entry.get("original_path")
    path = Path(origin) if origin and Path(origin).exists() else Path(entry["source"])
    if not path.exists():
        sys.exit(f"original no longer available for {entry['id']}")

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = _as_lines(text)

    if args.lines:
        m = re.fullmatch(r"(\d+)-(\d+)", args.lines)
        if not m:
            sys.exit("--lines expects START-END, e.g. 120-180")
        a, b = int(m.group(1)), int(m.group(2))
        out = [f"{i}: {_clip(lines[i - 1])}" for i in range(max(1, a), min(len(lines), b) + 1)]
        print("\n".join(out))
        return 0

    if args.grep:
        pat = re.compile(args.grep) if args.regex else re.compile(re.escape(args.grep))
        hits = 0
        for i, line in enumerate(lines, 1):
            if pat.search(line):
                lo, hi = max(1, i - args.context), min(len(lines), i + args.context)
                for j in range(lo, hi + 1):
                    mark = ">" if j == i else " "
                    print(f"{mark}{j}: {_clip(lines[j - 1])}")
                print("--")
                hits += 1
                if hits >= args.max_hits:
                    print(f"(stopped at --max-hits {args.max_hits})")
                    break
        if hits == 0:
            print(f"no match for {args.grep!r} in {path}")
        return 0

    if args.json_index is not None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            sys.exit(f"original is not valid JSON: {exc}")
        if not isinstance(data, list):
            sys.exit("--json-index requires the original to be a JSON array")
        idx = args.json_index
        if not -len(data) <= idx < len(data):
            sys.exit(f"index {idx} out of range (len={len(data)})")
        print(json.dumps(data[idx], indent=2, ensure_ascii=False))
        return 0

    head = args.head or 40
    print("\n".join(f"{i}: {_clip(l)}" for i, l in enumerate(lines[:head], 1)))
    if len(lines) > head:
        print(f"... ({len(lines) - head} more lines; use --lines A-B or --grep)")
    return 0


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def cmd_stats(args: argparse.Namespace) -> int:
    ws = workspace_dir(args.workspace)
    entries = load_manifest(ws)["entries"]
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print(f"no entries in {ws}")
        return 0
    saved = sum(e["tokens_saved"] for e in entries if e.get("accepted"))
    before = sum(e["tokens_before"] for e in entries if e.get("accepted"))
    print(f"{'id':<12} {'saved':>9} {'ratio':>7} {'verdict':<14} source")
    for e in entries:
        flag = "" if e.get("accepted") else "  (rejected)"
        print(
            f"{e['id']:<12} {e['tokens_saved']:>9,} {e['ratio']:>6.1%} "
            f"{e['verdict']:<14} {Path(e['source']).name}{flag}"
        )
    pct = 0.0 if before == 0 else saved / before
    print(f"\ntotal saved {saved:,} tokens ({pct:.1%} of {before:,})")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hr.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", help=f"workspace dir (default {DEFAULT_WORKSPACE}, or ${WORKSPACE_ENV})")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    # The same two flags are accepted after the subcommand as well, because
    # `hr.py stats --json` is the form everyone reaches for first. SUPPRESS
    # keeps an omitted flag from overwriting a value given before the
    # subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")

    sub = p.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    e = sub.add_parser("ensure", help="install headroom-ai if missing")
    e.add_argument("--code", action="store_true", help="also install tree-sitter grammars (headroom-ai[code])")
    e.set_defaults(func=lambda a: (ensure_headroom(with_code=a.code), print("headroom-ai ready"), 0)[-1])

    s = sub.add_parser("scan", help="estimate token cost; say what is worth compressing")
    s.add_argument("paths", nargs="+")
    s.add_argument("--model", default="claude-sonnet-4-5-20250929")
    s.add_argument("--max-bytes", type=int, default=50_000_000)
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("compress", help="compress a file, measure savings and fidelity")
    c.add_argument("file")
    c.add_argument("--model", default="claude-sonnet-4-5-20250929")
    c.add_argument("--profile", default="", choices=["", "coding", "balanced", "general", "agent-90"])
    c.add_argument("--aggressive", action="store_true", help="disable protections; higher savings, more loss")
    c.add_argument("--min-ratio", type=float, default=0.15, help="reject below this saving (default 0.15)")
    c.add_argument("--min-fidelity", type=float, default=0.95, help="reject below this coverage (default 0.95)")
    c.add_argument("--emit", action="store_true", help="print the compressed text to stdout")
    c.add_argument("--no-store", action="store_true", help="measure only; write nothing")
    c.set_defaults(func=cmd_compress)

    r = sub.add_parser("retrieve", help="pull exact content out of the stored original")
    r.add_argument("ref", help="entry id, path, or filename")
    r.add_argument("--lines", help="line range START-END")
    r.add_argument("--grep", help="show matching lines with context")
    r.add_argument("--regex", action="store_true", help="treat --grep as a regex")
    r.add_argument("--context", type=int, default=2)
    r.add_argument("--max-hits", type=int, default=20)
    r.add_argument("--json-index", type=int, help="print element N of a JSON array original")
    r.add_argument("--head", type=int, help="first N lines (default 40)")
    r.set_defaults(func=cmd_retrieve)

    t = sub.add_parser("stats", help="cumulative savings for this workspace")
    t.set_defaults(func=cmd_stats)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
