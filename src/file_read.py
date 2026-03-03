"""Read local files for the read_file tool."""

import gzip
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Truncate file contents to ~20k tokens (~80k chars)
MAX_CHARS = 80_000

# Extensions we know are text — anything else gets a binary sniff check
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".rst", ".csv", ".xml", ".svg",
    ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1",
    ".sql", ".graphql", ".gql",
    ".env", ".gitignore", ".dockerignore", ".editorconfig",
    ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb",
    ".php", ".swift", ".kt", ".kts", ".scala", ".r", ".R",
    ".lua", ".vim", ".el", ".lisp", ".clj", ".ex", ".exs",
    ".tf", ".hcl", ".makefile", ".mk",
    "Makefile", "Dockerfile", "Vagrantfile",
}


def read_file(
    path: str,
    from_line: int | None = None,
    to_line: int | None = None,
) -> str:
    """Read a file by absolute or relative path.

    Optional from_line/to_line (0-based, inclusive) limit output to a line
    range.  For .gz transcript files this maps directly to JSONL line indices.

    Returns file contents as text, a directory listing, or an error message.
    Never raises — all errors are returned as strings.
    """
    try:
        target = Path(path).resolve()
    except (ValueError, OSError) as exc:
        return f"Invalid path: {exc}"

    if not target.exists():
        return f"File not found: {path}"

    # Directory — return a listing
    if target.is_dir():
        return _list_directory(target)

    # Gzip — decompress transparently
    if target.suffix.lower() == ".gz":
        return _read_gzip(target, path, from_line, to_line)

    # Binary check
    if _is_binary(target):
        return "Cannot read binary file."

    # Read file contents
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as exc:
        logger.warning("Error reading %s: %s", target, exc)
        return f"Error reading file: {exc}"

    text = _apply_line_range(text, from_line, to_line)

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[Content truncated]"

    return text


def _read_gzip(
    target: Path,
    original_path: str,
    from_line: int | None = None,
    to_line: int | None = None,
) -> str:
    """Decompress a .gz file and return its text contents."""
    try:
        with gzip.open(target, "rt", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except PermissionError:
        return f"Permission denied: {original_path}"
    except Exception as exc:
        logger.warning("Error reading gzip %s: %s", target, exc)
        return f"Error reading gzip file: {exc}"

    text = _apply_line_range(text, from_line, to_line)

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[Content truncated]"

    return text


def _apply_line_range(
    text: str,
    from_line: int | None,
    to_line: int | None,
) -> str:
    """Return only lines from_line..to_line (0-based, inclusive)."""
    if from_line is None and to_line is None:
        return text
    lines = text.splitlines(keepends=True)
    start = max(from_line or 0, 0)
    end = min((to_line or len(lines) - 1) + 1, len(lines))
    return "".join(lines[start:end])


def _is_binary(path: Path) -> bool:
    """Check if a file is binary by extension or content sniffing."""
    if path.suffix.lower() in TEXT_EXTENSIONS or path.name in TEXT_EXTENSIONS:
        return False

    # Sniff first 8KB for null bytes
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except Exception:
        return True


def _list_directory(target: Path) -> str:
    """Return a directory listing similar to ls -la."""
    lines = [f"Directory: {target}", ""]

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return f"Permission denied listing directory: {target}"

    for entry in entries:
        try:
            st = entry.stat()
            size = st.st_size
            kind = "d" if entry.is_dir() else "-"
            if size >= 1_048_576:
                size_str = f"{size / 1_048_576:.1f}M"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f}K"
            else:
                size_str = f"{size}B"
            name = entry.name + ("/" if entry.is_dir() else "")
            lines.append(f"  {kind} {size_str:>8}  {name}")
        except OSError:
            lines.append(f"  ? {'?':>8}  {entry.name}")

    if len(entries) == 0:
        lines.append("  (empty)")

    return "\n".join(lines)
