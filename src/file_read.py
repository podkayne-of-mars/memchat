"""Read local files for the read_file tool."""

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


def read_file(path: str) -> str:
    """Read a file by absolute or relative path.

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

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[Content truncated]"

    return text


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
