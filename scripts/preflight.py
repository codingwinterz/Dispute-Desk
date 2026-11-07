"""Read-only environment check. NOT part of pytest.

    python scripts/preflight.py

Prints PASS / WARN / FAIL per check; exits 1 if any check FAILs.
Never prints secret values or .env contents: only variable names, lengths, and line numbers.
The only network call is to FalkorDB on localhost:6379. No PayPal calls are made.
"""

import io
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
from dotenv.parser import parse_stream  # noqa: E402

from disputedesk.config import PROJECT_ROOT, SANDBOX_BASE_URL, Settings  # noqa: E402

ENV_FILE = PROJECT_ROOT / ".env"
REQUIRED_VARS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "GEMINI_API_KEY")
FALKOR_HOST, FALKOR_PORT = "localhost", 6379
# PayPal REST API hosts (live and sandbox). Used to spot any non-sandbox API URL.
PAYPAL_API_URL = re.compile(r"https?://api(?:-m)?(?:\.sandbox)?\.paypal\.com", re.IGNORECASE)
CODE_DIRS = ("disputedesk", "scripts")

BOM_UTF8 = b"\xef\xbb\xbf"
BOM_UTF16 = (b"\xff\xfe", b"\xfe\xff")


@dataclass
class Result:
    verdict: str  # PASS | WARN | FAIL
    reason: str


def check_exists(env_file: Path) -> Result:
    if env_file.is_file():
        return Result("PASS", f"{env_file.name} found in repo root")
    return Result("FAIL", f"{env_file.name} not found; copy .env.example to .env and fill it in")


def check_encoding(env_file: Path) -> Result:
    raw = env_file.read_bytes()
    if raw.startswith(BOM_UTF16):
        return Result("FAIL", "UTF-16 BOM; re-save .env as UTF-8 (python-dotenv reads it as UTF-8)")
    if raw.startswith(BOM_UTF8):
        # load_dotenv decodes as plain utf-8, so the BOM becomes part of the first key name.
        return Result("WARN", "UTF-8 with BOM; the first variable's name will include the BOM. "
                              "Re-save as UTF-8 without BOM")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Result("FAIL", f"not valid UTF-8 (bad byte at offset {exc.start})")
    return Result("PASS", "plain UTF-8, no BOM")


def check_parse(env_file: Path) -> Result:
    try:
        text = env_file.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return Result("FAIL", "cannot decode as UTF-8, so python-dotenv cannot parse it")
    # Only line numbers are reported; binding.original.string (the line text) is never used.
    bad = [b.original.line for b in parse_stream(io.StringIO(text)) if b.error]
    if bad:
        return Result("FAIL", f"python-dotenv cannot parse line(s): {', '.join(map(str, bad))}")
    return Result("PASS", "python-dotenv parsed every line")


def read_values(env_file: Path) -> dict[str, str | None]:
    """Values exactly as load_settings() would see them from the file (default utf-8)."""
    try:
        return dotenv_values(env_file)
    except UnicodeDecodeError:
        return {}


def check_vars(values: dict[str, str | None]) -> tuple[Result, list[str]]:
    lines: list[str] = []
    missing: list[str] = []
    warn = False
    for name in REQUIRED_VARS:
        value = values.get(name) or ""
        if value:
            lines.append(f"{name}: set (length {len(value)})")
        else:
            lines.append(f"{name}: missing")
            missing.append(name)
    if values.get("PAYPAL_WEBHOOK_ID") == "placeholder":
        lines.append("PAYPAL_WEBHOOK_ID is 'placeholder'; register the webhook and put its real ID here")
        warn = True
    if missing:
        return Result("FAIL", f"missing: {', '.join(missing)}"), lines
    if warn:
        return Result("WARN", "all set, but PAYPAL_WEBHOOK_ID is a placeholder"), lines
    return Result("PASS", "all required variables set"), lines


def _resp_command(*args: str) -> bytes:
    parts = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        data = arg.encode()
        parts.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(parts)


def check_falkordb(host: str = FALKOR_HOST, port: int = FALKOR_PORT, timeout: float = 3.0) -> Result:
    # Raw RESP over a socket so the check needs no client library. GRAPH.QUERY on a
    # graph that does not exist yet answers RETURN 1 without writing any data.
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(_resp_command("GRAPH.QUERY", "preflight", "RETURN 1"))
            reply = sock.recv(4096)
    except OSError as exc:
        return Result("FAIL", f"cannot reach {host}:{port} ({type(exc).__name__}); is FalkorDB running?")
    if reply.startswith(b"*"):
        return Result("PASS", f"{host}:{port} answered GRAPH.QUERY")
    if reply.startswith(b"-"):
        # Error replies are server messages (e.g. unknown command), not secrets.
        msg = reply[1:].split(b"\r\n", 1)[0].decode(errors="replace")[:120]
        return Result("FAIL", f"{host}:{port} replied with an error: {msg} (Redis without FalkorDB module?)")
    return Result("FAIL", f"{host}:{port} gave an unexpected reply")


def check_sandbox_only(values: dict[str, str | None], root: Path = PROJECT_ROOT) -> Result:
    problems: list[str] = []
    if ".sandbox." not in SANDBOX_BASE_URL:
        problems.append("config.SANDBOX_BASE_URL is not a sandbox URL")
    if Settings.__dataclass_fields__["base_url"].default != SANDBOX_BASE_URL:
        problems.append("Settings.base_url default is not SANDBOX_BASE_URL")
    # .env: report the variable name only, never the value.
    for name, value in values.items():
        for url in PAYPAL_API_URL.findall(value or ""):
            if url.lower() != SANDBOX_BASE_URL:
                problems.append(f".env variable {name} has a non-sandbox PayPal API URL")
    # Code: any PayPal API host other than the sandbox one.
    for dirname in CODE_DIRS:
        for path in sorted((root / dirname).rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for url in PAYPAL_API_URL.findall(text):
                if url.lower() != SANDBOX_BASE_URL:
                    problems.append(f"{path.relative_to(root).as_posix()} has a non-sandbox PayPal API URL")
    if problems:
        return Result("FAIL", "; ".join(dict.fromkeys(problems)))
    return Result("PASS", f"{SANDBOX_BASE_URL} is the only PayPal API URL configured")


def run(env_file: Path = ENV_FILE, root: Path = PROJECT_ROOT) -> int:
    results: list[tuple[str, Result, list[str]]] = []
    exists = check_exists(env_file)
    results.append(("1 .env exists", exists, []))
    if exists.verdict == "PASS":
        results.append(("2 .env encoding", check_encoding(env_file), []))
        results.append(("3 .env parses", check_parse(env_file), []))
        values = read_values(env_file)
        vars_result, var_lines = check_vars(values)
        results.append(("4 required variables", vars_result, var_lines))
    else:
        values = {}
        for name in ("2 .env encoding", "3 .env parses", "4 required variables"):
            results.append((name, Result("FAIL", "skipped: no .env"), []))
    results.append(("5 FalkorDB", check_falkordb(), []))
    results.append(("6 sandbox-only PayPal URL", check_sandbox_only(values, root), []))

    for name, result, detail in results:
        print(f"[{result.verdict}] {name}: {result.reason}")
        for line in detail:
            print(f"       {line}")
    return 1 if any(r.verdict == "FAIL" for _, r, _ in results) else 0


if __name__ == "__main__":
    sys.exit(run())
