"""Where a name is defined, referenced or called, from a language server's index (DL-538).

    scripts/refs.py NAME              every definition named exactly NAME, each with its references
    scripts/refs.py PATH:LINE[:COL]   the symbol at that position (1-based)
    options: --callers (callers instead of references), --defs, --root DIR, --timeout SEC

The rule: one `ty server` is started for this call over stdio and stopped after it, so no
server outlives the command and no file or network is needed. The server finds symbols; the
script only formats. Exit 0 with a definition, 1 when there is none, 2 on a usage or server
failure. Navigation only: `mypy` stays the type gate.
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
POSITION = re.compile(r"(?P<path>.+?):(?P<line>[0-9]+)(?::(?P<col>[0-9]+))?")
STOP_GRACE = 3.0
JOIN_GRACE = 1.0  # per reader thread, after the server has ended


class Failure(Exception):
    """A usage, server or protocol failure: exit 2."""


class NotFound(Exception):
    """No definition or no symbol at the position: exit 1."""


def uri_of(path: Path) -> str:
    return "file://" + pathname2url(str(path))


def path_of(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def utf16_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def index_of_utf16(text: str, offset: int) -> int:
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


class Server:
    """One language server process speaking LSP over stdio, bounded by one deadline."""

    def __init__(self, exe: str, root: Path, deadline: float) -> None:
        self.deadline = deadline
        self.next_id = 0
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr_chunks: list[bytes] = []
        try:
            self.proc = subprocess.Popen(
                [exe, "server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
            )
        except OSError as error:
            raise Failure(f"cannot start {exe}: {error}") from error
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.stdin = self.proc.stdin
        self.readers = [
            threading.Thread(target=target, args=(stream,), daemon=True)
            for target, stream in ((self._read, self.proc.stdout), (self._drain, self.proc.stderr))
        ]
        for reader in self.readers:
            reader.start()

    # Only a reader thread touches the stream it reads, and it closes that stream itself;
    # `finish` ends the process and waits for the readers to see end of file (DL-538).
    def _drain(self, stream: Any) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                self.stderr_chunks.append(chunk)
        except Exception:  # a thread must print nothing; the exit code carries the failure
            pass
        finally:
            self._close_quietly(stream)

    @staticmethod
    def _close_quietly(stream: Any) -> None:
        try:
            stream.close()
        except Exception:
            pass

    def _read(self, stream: Any) -> None:
        try:
            while True:
                length = -1
                while True:
                    header = stream.readline()
                    if not header:
                        return
                    if header in (b"\r\n", b"\n"):
                        break
                    name, _, value = header.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value)
                if length < 0:
                    return
                body = stream.read(length)
                if len(body) < length:
                    return
                self.inbox.put(json.loads(body))
        except Exception:  # a thread must print nothing; `request` reports the closed output
            pass
        finally:
            self._close_quietly(stream)
            self.inbox.put(None)

    def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        try:
            self.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.stdin.flush()
        except OSError as error:
            raise Failure(f"server closed its input: {error}") from error

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        wanted = self.next_id
        self.send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise Failure(f"timeout waiting for {method}")
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise Failure(f"timeout waiting for {method}") from None
            if message is None:
                raise Failure("server closed its output before answering")
            if "method" in message and "id" in message:
                self._answer_server_request(message)
            elif message.get("id") == wanted and "method" not in message:
                if "error" in message:
                    raise Failure(f"{method}: {message['error']}")
                return message.get("result")

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        items = (message.get("params") or {}).get("items")
        result = [None] * len(items) if isinstance(items, list) else None
        self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def start(self, root: Path) -> None:
        folders = [{"uri": uri_of(root), "name": root.name}]
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": uri_of(root),
                "workspaceFolders": folders,
                "capabilities": {
                    "workspace": {"configuration": True, "workspaceFolders": True},
                    "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
                },
            },
        )
        self.notify("initialized", {})

    def open_file(self, path: Path, text: str) -> None:
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri_of(path),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def stop(self) -> None:
        """Shut the server down; kill it if it does not leave within the grace period."""
        try:
            self.deadline = max(self.deadline, time.monotonic() + STOP_GRACE)
            self.request("shutdown", {})
            self.notify("exit", {})
            self._close_quietly(self.stdin)
            self.proc.wait(timeout=STOP_GRACE)
        except (Failure, OSError, subprocess.TimeoutExpired):
            self.kill()
        else:
            self.join_readers()

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait()
        self._close_quietly(self.stdin)
        self.join_readers()

    def join_readers(self) -> None:
        """Wait, bounded, for both readers to see end of file; a process the server left
        holding a pipe cannot keep this command waiting."""
        for reader in self.readers:
            reader.join(timeout=JOIN_GRACE)

    def stderr_text(self) -> str:
        return b"".join(self.stderr_chunks).decode(errors="replace").strip()


class Workspace:
    """Reads source text and turns LSP locations into root-relative `path:line:col`."""

    def __init__(self, root: Path, server: Server) -> None:
        self.root = root
        self.server = server
        self.texts: dict[Path, list[str]] = {}

    def lines(self, path: Path) -> list[str]:
        if path not in self.texts:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise NotFound(f"cannot read {path}: {error}") from error
            self.texts[path] = text.split("\n")
            self.server.open_file(path, text)
        return self.texts[path]

    def shown(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def column(self, path: Path, line: int, offset: int) -> int:
        """The 1-based column of a UTF-16 offset on a 0-based line."""
        rows = self.lines(path)
        text = rows[line] if line < len(rows) else ""
        return index_of_utf16(text, offset) + 1

    def locate(self, location: dict[str, Any]) -> tuple[str, int, int]:
        """(path, 1-based line, 1-based column) of a Location or LocationLink."""
        uri = location.get("uri") or location["targetUri"]
        span = location.get("range") or location["targetSelectionRange"]
        path = path_of(uri)
        start = span["start"]
        return (
            self.shown(path),
            start["line"] + 1,
            self.column(path, start["line"], start["character"]),
        )


Position = tuple[int, int]  # 0-based line, UTF-16 offset
Row = tuple[str, int, int, str]  # path, line, column, calling function


class Group:
    def __init__(self, header: str, rows: list[Row]) -> None:
        self.header = header
        self.rows = rows


class Definition:
    def __init__(self, name: str, path: Path, line: int, col: int) -> None:
        self.name = name
        self.path = path
        self.line = line  # 0-based
        self.col = col  # 0-based index into the line


def flatten(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for symbol in symbols:
        flat.append(symbol)
        flat.extend(flatten(symbol.get("children") or []))
    return flat


def name_start(work: Workspace, path: Path, name: str, span: dict[str, Any] | None) -> Position:
    """The server's own position of the name: `selectionRange` of the document symbol whose
    range is the `workspace/symbol` hit's. Decorators and strings never decide it (DL-538)."""
    document = {"textDocument": {"uri": uri_of(path)}}
    symbols = work.server.request("textDocument/documentSymbol", document) or []
    for symbol in flatten(symbols):
        selection = symbol.get("selectionRange")
        if symbol.get("name") == name and selection and (span is None or symbol["range"] == span):
            return selection["start"]["line"], selection["start"]["character"]
    start = (span or {"start": {"line": 0, "character": 0}})["start"]
    return start["line"], start["character"]


def definitions_by_name(work: Workspace, name: str) -> list[Definition]:
    symbols = work.server.request("workspace/symbol", {"query": name}) or []
    found: dict[tuple[Path, int, int], Definition] = {}
    for symbol in symbols:
        if symbol.get("name") != name:
            continue
        location = symbol["location"]
        path = path_of(location["uri"])
        rows = work.lines(path)
        line, offset = name_start(work, path, name, location.get("range"))
        index = index_of_utf16(rows[line], offset) if line < len(rows) else 0
        found[(path, line, index)] = Definition(name, path, line, index)
    return sorted(found.values(), key=lambda d: (work.shown(d.path), d.line, d.col))


def target_start(target: dict[str, Any]) -> tuple[Path, int]:
    """The file and 0-based line of a Location or LocationLink."""
    span = target.get("range") or target["targetSelectionRange"]
    return path_of(target.get("uri") or target["targetUri"]), span["start"]["line"]


def word_at(text: str, column: int | None) -> re.Match[str] | None:
    """The identifier at a 1-based column; with none, the first one that is no keyword."""
    if column is not None:
        return next(
            (m for m in IDENTIFIER.finditer(text) if m.start() <= column - 1 < m.end()), None
        )
    return next((m for m in IDENTIFIER.finditer(text) if not keyword.iskeyword(m.group())), None)


def position_groups(work: Workspace, spec: str, options: argparse.Namespace) -> list[Group]:
    """Ask at the given position; the definition request only names the header line."""
    matched = POSITION.fullmatch(spec)
    assert matched
    path = Path(matched["path"])
    path = (path if path.is_absolute() else Path.cwd() / path).resolve()
    line = int(matched["line"]) - 1
    rows = work.lines(path)
    if not 0 <= line < len(rows):
        raise NotFound(f"{spec}: no such line")
    word = word_at(rows[line], int(matched["col"]) if matched["col"] else None)
    if word is None:
        raise NotFound(f"{spec}: no symbol at this position")
    params = {
        "textDocument": {"uri": uri_of(path)},
        "position": {"line": line, "character": utf16_offset(rows[line], word.start())},
    }
    targets = work.server.request("textDocument/definition", params) or []
    if isinstance(targets, dict):
        targets = [targets]
    headers = sorted({(work.shown(p), n + 1) for p, n in map(target_start, targets)})
    result: list[Row] = [] if options.defs else query_rows(work, params, options.callers)
    if not headers and not result:
        raise NotFound(f"{spec}: no symbol at this position")
    if not headers:
        headers = [(work.shown(path), line + 1)]
    return [
        Group(f"# {word.group()} {shown}:{number}", result if i == 0 else [])
        for i, (shown, number) in enumerate(headers)
    ]


def query_rows(work: Workspace, params: dict[str, Any], callers: bool) -> list[Row]:
    return caller_rows(work, params) if callers else reference_rows(work, params)


def reference_rows(work: Workspace, params: dict[str, Any]) -> list[Row]:
    result = work.server.request(
        "textDocument/references", {**params, "context": {"includeDeclaration": True}}
    )
    return [(*work.locate(item), "") for item in result or []]


def caller_rows(work: Workspace, params: dict[str, Any]) -> list[Row]:
    items = work.server.request("textDocument/prepareCallHierarchy", params)
    rows: list[Row] = []
    for item in items or []:
        calls = work.server.request("callHierarchy/incomingCalls", {"item": item}) or []
        for call in calls:
            caller = call["from"]
            for span in call.get("fromRanges") or [caller["selectionRange"]]:
                path = path_of(caller["uri"])
                start = span["start"]
                rows.append(
                    (
                        work.shown(path),
                        start["line"] + 1,
                        work.column(path, start["line"], start["character"]),
                        caller["name"],
                    )
                )
    return rows


def name_groups(work: Workspace, name: str, options: argparse.Namespace) -> list[Group]:
    groups: list[Group] = []
    for definition in definitions_by_name(work, name):
        header = f"# {definition.name} {work.shown(definition.path)}:{definition.line + 1}"
        text = work.lines(definition.path)[definition.line]
        params = {
            "textDocument": {"uri": uri_of(definition.path)},
            "position": {"line": definition.line, "character": utf16_offset(text, definition.col)},
        }
        groups.append(
            Group(header, [] if options.defs else query_rows(work, params, options.callers))
        )
    if not groups:
        raise NotFound(f"no definition named {name}")
    return groups


def render(groups: list[Group]) -> None:
    for group in groups:
        print(group.header)
        for path, line, col, caller in sorted(set(group.rows)):
            print(f"{path}:{line}:{col}" + (f" {caller}" if caller else ""))


def find_ty() -> str | None:
    beside = Path(sys.executable).parent / "ty"
    if beside.is_file():
        return str(beside)
    return shutil.which("ty")


def git_top() -> Path:
    done = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise Failure("not in a Git checkout: pass --root DIR")
    return Path(done.stdout.strip())


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        raise Failure(f"refs.py: {message}")


def parse(argv: list[str]) -> argparse.Namespace:
    parser = Parser(prog="refs.py", description="Where a name is defined, referenced or called.")
    parser.add_argument("target", metavar="NAME|PATH:LINE[:COL]")
    parser.add_argument("--callers", action="store_true", help="callers, not all references")
    parser.add_argument("--defs", action="store_true", help="definitions only")
    parser.add_argument("--root", type=Path, help="the workspace (default: the Git top level)")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds (default 20)")
    parser.add_argument("--ty", help=argparse.SUPPRESS)  # a test stand-in for the server
    options = parser.parse_args(argv)
    if options.callers and options.defs:
        parser.error("--callers and --defs exclude each other")
    if options.timeout <= 0:
        parser.error("--timeout must be positive")
    if not IDENTIFIER.fullmatch(options.target) and not POSITION.fullmatch(options.target):
        parser.error(f"{options.target!r} is neither a name nor PATH:LINE[:COL]")
    return options


def run(argv: list[str]) -> int:
    options = parse(argv)
    exe = options.ty or find_ty()
    if exe is None:
        raise Failure("ty not found: uv sync --frozen --extra dev")
    root = (options.root or git_top()).resolve()
    if not root.is_dir():
        raise Failure(f"--root {root} is not a directory")
    server = Server(exe, root, time.monotonic() + options.timeout)
    try:
        server.start(root)
        work = Workspace(root, server)
        if IDENTIFIER.fullmatch(options.target):
            groups = name_groups(work, options.target, options)
        else:
            groups = position_groups(work, options.target, options)
        render(groups)
        sys.stdout.flush()
        server.stop()
        return 0
    except BaseException as error:
        server.kill()
        if isinstance(error, Failure) and server.stderr_text():
            print(f"server stderr: {server.stderr_text()}", file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except NotFound as error:
        print(f"refs.py: {error}", file=sys.stderr)
        return 1
    except Failure as error:
        message = str(error)
        print(message if message.startswith("refs.py:") else f"refs.py: {message}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # no flush error at exit
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"refs.py: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
