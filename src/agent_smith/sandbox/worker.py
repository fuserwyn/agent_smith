"""Sandbox worker: executes untrusted Python code in a separate process.

This file is intentionally **self-contained and stdlib-only**: it is launched
as ``python -I worker.py`` with an empty environment, so it must not import
anything from the project or from third-party packages.

Protocol (JSON lines over private file descriptors)::

    host -> worker : {"type": "init", "config": {...}, "tools": [...], "extras": [...]}
                     {"type": "exec", "code": "..."}
                     {"type": "tool_result", "ok": true, "result": "..."}
    worker -> host : {"type": "ready", "rss_kb": 12345}
                     {"type": "out", "data": "..."}            (streamed stdout)
                     {"type": "tool_call", "name": "...", "args": {...}}
                     {"type": "done", "status": "...", ...}

Security layers (defence in depth):
  1. AST policy       - rejects dunder/frame attribute access and forbidden imports
                        before anything runs.
  2. Restricted builtins - no eval/exec/compile/input/globals/...; safe getattr family;
                        guarded ``__import__`` with an allowlist.
  3. Module proxies   - imported modules are wrapped so ``_private`` names and
                        non-allowlisted sub-modules (``random._os``, ``typing.sys``) are hidden.
  4. Audit hook       - ``sys.addaudithook`` (cannot be removed) blocks file access
                        outside ``allowed_directories``, sockets, subprocesses, ctypes...
  5. OS limits        - SIGALRM timeout (paused during tool calls), RLIMIT_AS on Linux;
                        the host additionally hard-kills on timeout / RSS overflow.
"""

import ast
import builtins
import io
import json
import linecache
import os
import signal
import sys
import traceback
import types

SANDBOX_FILENAME = "<sandbox>"

# Dunder attributes that are harmless and commonly needed (class definitions,
# super().__init__(), introspection of names). Everything else dunder is denied.
SAFE_DUNDER_ATTRS = {
    "__init__", "__name__", "__doc__", "__qualname__", "__len__", "__str__", "__repr__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__", "__iter__",
    "__next__", "__contains__", "__getitem__", "__setitem__", "__delitem__", "__call__",
    "__bool__", "__add__", "__sub__", "__mul__", "__truediv__", "__floordiv__", "__mod__",
    "__enter__", "__exit__", "__post_init__", "__neg__", "__abs__", "__radd__", "__rmul__",
}
SAFE_DUNDER_NAMES = {"__name__", "__doc__"}
# Non-dunder attributes that expose interpreter internals (frames / code objects).
FORBIDDEN_ATTRS = {
    "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "tb_frame", "tb_next",
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "co_code",
    "func_globals", "mro",
}
REMOVED_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint", "globals", "locals", "vars",
    "help", "exit", "quit", "copyright", "credits", "license", "__loader__", "__spec__",
}
# Modules that must never be imported, even indirectly, unless explicitly allowlisted.
HARD_DENY_MODULES = {
    "os", "posix", "nt", "sys", "subprocess", "socket", "_socket", "ssl", "ctypes", "_ctypes",
    "multiprocessing", "_multiprocessing", "threading", "_thread", "signal", "importlib",
    "builtins", "shutil", "pathlib", "io", "_io", "pty", "fcntl", "resource", "gc", "inspect",
    "code", "codeop", "pickle", "marshal", "urllib", "http", "ftplib", "smtplib", "asyncio",
    "selectors", "select", "mmap", "tempfile", "glob", "webbrowser", "sysconfig", "site",
}
# Audit events that are always denied.
DENIED_EVENT_PREFIXES = (
    "socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.kill", "os.killpg", "os.startfile", "os.putenv", "os.unsetenv",
    "ctypes.", "urllib.", "ftplib.", "smtplib.", "imaplib.", "poplib.", "nntplib.", "telnetlib.",
    "webbrowser.", "sys.settrace", "sys.setprofile", "sys._getframe", "sys._current_frames",
    "sys.remote_exec", "code.__new__", "function.__new__", "pty.", "mmap.", "os.add_dll_directory",
)
# Audit events carrying filesystem paths: event -> indices of path arguments.
PATH_EVENTS = {
    "os.listdir": (0,), "os.scandir": (0,), "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,),
    "os.rename": (0, 1), "os.chmod": (0,), "os.chown": (0,), "os.truncate": (0,), "os.utime": (0,),
    "os.link": (0, 1), "os.symlink": (0, 1), "os.chdir": (0,), "os.chflags": (0,), "os.lchown": (0,),
    "os.mkfifo": (0,), "os.mknod": (0,), "os.listxattr": (0,), "os.getxattr": (0,),
    "os.setxattr": (0,), "os.removexattr": (0,), "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1),
    "shutil.rmtree": (0,), "shutil.move": (0, 1), "glob.glob": (0,), "os.walk": (0,),
}


class SandboxTimeout(BaseException):
    """Raised by SIGALRM; BaseException so user ``except Exception`` cannot swallow it."""


class FinalAnswerSignal(BaseException):
    """Raised by final_answer() to stop the current code block."""


class ToolError(Exception):
    """Raised inside the sandbox when an MCP tool returns an error."""


class SecurityViolation(Exception):
    """Raised when sandboxed code breaks a sandbox rule."""


# --------------------------------------------------------------------------- #
# Protocol channel (private fds, never reachable from user code)
# --------------------------------------------------------------------------- #
class Channel:
    def __init__(self):
        # Duplicate the real stdin/stdout, then point fd 0/1 at /dev/null so
        # sandboxed code can neither read commands nor forge protocol messages.
        self._in = os.fdopen(os.dup(0), "r", encoding="utf-8", newline="\n")
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.close(devnull)

    def send(self, msg):
        # Block SIGALRM while writing so a timeout cannot corrupt a message.
        old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            self._out.write(json.dumps(msg) + "\n")
            self._out.flush()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)

    def recv(self):
        line = self._in.readline()
        if not line:
            raise EOFError("host closed the channel")
        return json.loads(line)


class StreamingStdout(io.TextIOBase):
    """Replacement for sys.stdout/sys.stderr: streams user output to the host."""

    def __init__(self, channel, limit):
        self.channel = channel
        self.limit = limit
        self.sent = 0
        self.truncated = False
        self.buffer_ = ""

    def writable(self):
        return True

    def write(self, s):
        if not isinstance(s, str):
            raise TypeError("write() argument must be str")
        if self.sent >= self.limit:
            self.truncated = True
            return len(s)
        room = self.limit - self.sent
        if len(s) > room:
            s, self.truncated = s[:room], True
        self.sent += len(s)
        self.buffer_ += s
        if "\n" in self.buffer_ or len(self.buffer_) > 4096:
            self.flush()
        return len(s)

    def flush(self):
        if self.buffer_:
            data, self.buffer_ = self.buffer_, ""
            self.channel.send({"type": "out", "data": data})

    def reset(self):
        self.flush()
        self.sent = 0
        self.truncated = False


# --------------------------------------------------------------------------- #
# Policy helpers
# --------------------------------------------------------------------------- #
def import_allowed(name, allowlist):
    """``math`` allows exactly math; ``math.*`` allows math and its sub-modules."""
    for entry in allowlist:
        if entry == name:
            return True
        if entry.endswith(".*"):
            base = entry[:-2]
            if name == base or name.startswith(base + "."):
                return True
    return False


def is_forbidden_attr(name):
    if name in FORBIDDEN_ATTRS:
        return True
    return name.startswith("__") and name.endswith("__") and name not in SAFE_DUNDER_ATTRS


class PolicyChecker(ast.NodeVisitor):
    """Static checks run on the AST before execution."""

    def __init__(self, allowlist):
        self.allowlist = allowlist
        self.errors = []

    def _err(self, node, msg):
        self.errors.append("line %s: %s" % (getattr(node, "lineno", "?"), msg))

    def visit_Attribute(self, node):
        if is_forbidden_attr(node.attr):
            self._err(node, "access to attribute '%s' is not allowed in the sandbox" % node.attr)
        self.generic_visit(node)

    def visit_Name(self, node):
        n = node.id
        if n.startswith("__") and n.endswith("__") and n not in SAFE_DUNDER_NAMES:
            self._err(node, "use of name '%s' is not allowed in the sandbox" % n)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if not import_allowed(alias.name, self.allowlist):
                self._err(node, "import of '%s' is not allowed (allowed: %s)"
                          % (alias.name, ", ".join(self.allowlist)))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.level:
            self._err(node, "relative imports are not allowed")
        elif not import_allowed(node.module or "", self.allowlist):
            self._err(node, "import from '%s' is not allowed (allowed: %s)"
                      % (node.module, ", ".join(self.allowlist)))
        self.generic_visit(node)


def make_module_proxy(module, allowlist):
    """Return a read-only view of ``module`` hiding private names and forbidden sub-modules.

    The real module lives only in this closure: it is not reachable through any
    attribute of the proxy (closures are behind dunder attributes, which the AST
    policy and the safe ``getattr`` both reject).
    """
    mod_name = module.__name__

    def public_names():
        names = getattr(module, "__all__", None) or [n for n in dir(module) if not n.startswith("_")]
        return [n for n in names if not (isinstance(getattr(module, n, None), types.ModuleType)
                                         and not import_allowed(getattr(module, n).__name__, allowlist))]

    class SandboxedModule:
        __slots__ = ()

        def __getattr__(self, name):
            if name == "__all__":
                return public_names()
            if name.startswith("_") and name not in ("__name__", "__doc__"):
                raise AttributeError("access to private attribute '%s.%s' is not allowed" % (mod_name, name))
            value = getattr(module, name)
            if isinstance(value, types.ModuleType):
                if not import_allowed(value.__name__, allowlist):
                    raise AttributeError("access to module '%s' via '%s.%s' is not allowed"
                                         % (value.__name__, mod_name, name))
                return make_module_proxy(value, allowlist)
            return value

        def __setattr__(self, name, value):
            raise AttributeError("sandboxed modules are read-only")

        def __delattr__(self, name):
            raise AttributeError("sandboxed modules are read-only")

        def __dir__(self):
            return public_names()

        def __repr__(self):
            return "<sandboxed module '%s'>" % mod_name

    SandboxedModule.__name__ = SandboxedModule.__qualname__ = "module"
    return SandboxedModule()


def is_module_proxy(obj):
    return type(obj).__name__ == "module" and type(obj).__module__ == __name__


# --------------------------------------------------------------------------- #
# Execution tracer (line-by-line path of sandboxed code, for the UI)
# --------------------------------------------------------------------------- #
_PLAIN = (int, float, complex, bool, str, bytes, type(None))


def safe_repr(value, depth=0, limit=100):
    """repr() that never runs user code: only builtin values are rendered in full."""
    t = type(value)
    if t in _PLAIN:
        text = repr(value)
    elif t in (list, tuple, set, frozenset) and depth < 2:
        items = list(value)[:8] if t in (list, tuple) else sorted((safe_repr(v, depth + 1) for v in list(value)[:8]))
        inner = ", ".join(safe_repr(v, depth + 1, 40) if t in (list, tuple) else v for v in items)
        if len(value) > 8:
            inner += ", ..."
        text = {list: "[%s]", tuple: "(%s)", set: "{%s}", frozenset: "frozenset({%s})"}[t] % inner
    elif t is dict and depth < 2:
        pairs = list(value.items())[:6]
        text = "{" + ", ".join("%s: %s" % (safe_repr(k, depth + 1, 30), safe_repr(v, depth + 1, 40))
                               for k, v in pairs) + (", ..." if len(value) > 6 else "") + "}"
    elif isinstance(value, types.FunctionType):
        text = "<function %s>" % value.__name__
    elif isinstance(value, type):
        text = "<class %s>" % value.__name__
    else:
        text = "<%s object>" % t.__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


class ExecutionTracer:
    """Records which lines of ``<sandbox>`` code run, in order, with variable changes.

    Installed with ``sys.settrace`` during setup (before the audit hook forbids it)
    and active only while sandboxed code executes. Entries (compact keys)::

        {"e": "call"|"line"|"return"|"exception"|"tool"|"tool_result"|"final",
         "l": line, "f": function, "d": depth, "v": {name: repr} (changed variables), ...}
    """

    MAX_ENTRIES = 600

    def __init__(self, hidden_names):
        self.hidden = hidden_names
        self.active = False
        self.entries = []
        self.dropped = 0
        self.snapshots = {}
        self.last_index = {}
        self.depth = 0
        self.last_line = 0

    def start(self):
        # The module snapshot ("<module>") persists across executions: variables carry over.
        module_snapshot = self.snapshots.get("<module>", {})
        self.entries, self.dropped, self.depth, self.last_line = [], 0, 0, 0
        self.snapshots, self.last_index = {"<module>": module_snapshot}, {}
        self.active = True

    def stop(self):
        self.active = False

    def add(self, entry):
        if len(self.entries) >= self.MAX_ENTRIES:
            self.dropped += 1
            return None
        self.entries.append(entry)
        return len(self.entries) - 1

    @staticmethod
    def _key(frame):
        return "<module>" if frame.f_code.co_name == "<module>" else id(frame)

    def _vars(self, frame):
        scope = frame.f_globals if frame.f_code.co_name == "<module>" else frame.f_locals
        out = {}
        for name, value in list(scope.items())[:60]:
            if name.startswith("_") or name in self.hidden or is_module_proxy(value):
                continue
            out[name] = safe_repr(value)
        return out

    def _attribute_changes(self, frame):
        """Variables changed since the previous event of this frame are the effect of that
        frame's previous line: attach them to that entry."""
        key = self._key(frame)
        now = self._vars(frame)
        before = self.snapshots.get(key, {})
        self.snapshots[key] = now
        changes = {k: v for k, v in now.items() if before.get(k) != v}
        idx = self.last_index.get(key)
        if changes and idx is not None and idx < len(self.entries):
            self.entries[idx].setdefault("v", {}).update(changes)
            return {}
        return changes

    def global_trace(self, frame, event, arg):
        if not self.active or frame.f_code.co_filename != SANDBOX_FILENAME:
            return None
        if event == "call" and frame.f_code.co_name != "<module>":
            self.depth += 1
            key = self._key(frame)
            self.snapshots[key] = self._vars(frame)
            idx = self.add({"e": "call", "l": frame.f_lineno, "f": frame.f_code.co_name, "d": self.depth,
                            "v": dict(self.snapshots[key])})
            self.last_index[key] = idx
        return self.local_trace

    def local_trace(self, frame, event, arg):
        if not self.active:
            return None
        name, key = frame.f_code.co_name, self._key(frame)
        if event == "line":
            self._attribute_changes(frame)
            self.last_line = frame.f_lineno
            self.last_index[key] = self.add({"e": "line", "l": frame.f_lineno, "f": name, "d": self.depth})
        elif event == "return":
            self._attribute_changes(frame)
            if name != "<module>":
                self.add({"e": "return", "l": frame.f_lineno, "f": name, "d": self.depth, "r": safe_repr(arg)})
                self.depth = max(0, self.depth - 1)
                self.snapshots.pop(key, None)
                self.last_index.pop(key, None)
        elif event == "exception":
            exc_type = arg[0] if arg else None
            if exc_type is not FinalAnswerSignal:
                self.add({"e": "exception", "l": frame.f_lineno, "f": name, "d": self.depth,
                          "x": getattr(exc_type, "__name__", "Exception")})
        return self.local_trace

    def result(self):
        return {"entries": self.entries, "dropped": self.dropped}


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self):
        self.channel = Channel()
        self.config = {}
        self.allowlist = []
        self.allowed_dirs = []
        self.read_only_dirs = []
        self.namespace = {}
        self.sources = []  # every executed code block, for final_answer(func)
        self.final_answer_value = None
        self.final_answer_called = False
        self.stdout = None
        self.exec_counter = 0
        self.tracer = None

    # ---------------- setup ----------------
    def setup(self, init):
        self.config = init["config"]
        self.allowlist = list(self.config.get("authorized_imports", []))
        self.allowed_dirs = [os.path.realpath(d) for d in self.config.get("allowed_directories", [])]
        # The stdlib may be *read* (lazy imports inside allowed modules need it).
        stdlib = os.path.dirname(os.__file__)
        self.read_only_dirs = [os.path.realpath(stdlib)]

        # Work in the first existing allowed directory (or the scratch dir if creatable).
        for d in self.allowed_dirs:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                continue
            if os.path.isdir(d):
                os.chdir(d)
                break

        # Pre-import allowlisted modules before the audit hook locks the filesystem.
        for entry in self.allowlist:
            base = entry[:-2] if entry.endswith(".*") else entry
            try:
                __import__(base)
            except Exception:
                pass

        self.apply_memory_limit()
        self.stdout = StreamingStdout(self.channel, int(self.config.get("max_output_chars", 20000)))
        sys.stdout = self.stdout
        sys.stderr = self.stdout
        self.namespace = self.build_namespace(init.get("tools", []), init.get("extras", []))
        signal.signal(signal.SIGALRM, self._on_alarm)
        if init.get("trace"):
            # Must happen before the audit hook, which forbids sys.settrace afterwards.
            hidden = set(self.namespace) | {"ToolError"}
            self.tracer = ExecutionTracer(hidden)
            sys.settrace(self.tracer.global_trace)
        sys.addaudithook(self.audit_hook)

    def apply_memory_limit(self):
        limit_mb = int(self.config.get("max_memory_mb", 512))
        try:
            import resource
            if sys.platform.startswith("linux"):
                # RLIMIT_AS counts virtual memory: add the interpreter's current footprint.
                with open("/proc/self/status") as fh:
                    vm_kb = next(int(l.split()[1]) for l in fh if l.startswith("VmSize:"))
                limit = vm_kb * 1024 + limit_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass  # host-side RSS watchdog still enforces the limit

    @staticmethod
    def current_rss_kb():
        try:
            with open("/proc/self/status") as fh:
                return next(int(l.split()[1]) for l in fh if l.startswith("VmRSS:"))
        except Exception:
            try:
                import resource
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                return rss // 1024 if sys.platform == "darwin" else rss
            except Exception:
                return 0

    # ---------------- audit hook ----------------
    def _path_ok(self, path, write):
        if isinstance(path, int):
            return False  # raw fds would expose the protocol channel
        if path is None:
            path = "."
        try:
            path = os.fsdecode(path)
        except TypeError:
            return False
        real = os.path.realpath(os.path.abspath(path))
        for d in self.allowed_dirs:
            if real == d or real.startswith(d.rstrip(os.sep) + os.sep):
                return True
        if not write:
            for d in self.read_only_dirs:
                if real == d or real.startswith(d + os.sep):
                    return True
        return False

    def audit_hook(self, event, args):
        if event == "open":
            path, mode = args[0], args[1]
            write = isinstance(mode, str) and any(c in mode for c in "wax+")
            flags = args[2] if len(args) > 2 else 0
            if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                write = True
            if not self._path_ok(path, write):
                raise PermissionError("Sandbox: access to path %r is denied (allowed directories: %s)"
                                      % (path, ", ".join(self.allowed_dirs) or "none"))
            return
        if event in PATH_EVENTS:
            for i in PATH_EVENTS[event]:
                if i < len(args) and not self._path_ok(args[i], write=True if event != "os.listdir" else False):
                    raise PermissionError("Sandbox: filesystem operation %s on %r is denied" % (event, args[i]))
            return
        if event == "import":
            name = args[0] or ""
            root = name.split(".")[0]
            if (root in HARD_DENY_MODULES or name in HARD_DENY_MODULES) and not import_allowed(name, self.allowlist):
                raise ImportError("Sandbox: import of module '%s' is not allowed" % name)
            return
        for prefix in DENIED_EVENT_PREFIXES:
            if event.startswith(prefix):
                if event.startswith("socket."):
                    raise PermissionError("Sandbox: network access is disabled (%s)" % event)
                raise PermissionError("Sandbox: operation '%s' is not allowed" % event)

    # ---------------- namespace ----------------
    def build_namespace(self, tools, extras):
        allow = self.allowlist

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level:
                raise ImportError("relative imports are not allowed in the sandbox")
            if not import_allowed(name, allow):
                raise ImportError("import of '%s' is not allowed. Allowed imports: %s" % (name, ", ".join(allow)))
            module = __import__(name, None, None, fromlist, 0)
            if fromlist:
                for item in fromlist:
                    if item == "*":
                        continue
                    if item.startswith("_"):
                        raise ImportError("cannot import private name '%s' from '%s'" % (item, name))
                    sub = getattr(module, item, None)
                    if isinstance(sub, types.ModuleType) and not import_allowed(sub.__name__, allow):
                        raise ImportError("import of '%s' is not allowed" % sub.__name__)
            return make_module_proxy(module, allow)

        def _check_name(name):
            if not isinstance(name, str):
                raise TypeError("attribute name must be a string")
            if is_forbidden_attr(name):
                raise SecurityViolation("access to attribute '%s' is not allowed in the sandbox" % name)

        def safe_getattr(obj, name, *default):
            _check_name(name)
            return getattr(obj, name, *default)

        def safe_hasattr(obj, name):
            _check_name(name)
            return hasattr(obj, name)

        def safe_setattr(obj, name, value):
            _check_name(name)
            if isinstance(obj, types.ModuleType) or is_module_proxy(obj):
                raise SecurityViolation("modules are read-only in the sandbox")
            return setattr(obj, name, value)

        def safe_delattr(obj, name):
            _check_name(name)
            return delattr(obj, name)

        safe_builtins = {k: v for k, v in vars(builtins).items() if k not in REMOVED_BUILTINS}
        safe_builtins.update(
            __import__=guarded_import, getattr=safe_getattr, hasattr=safe_hasattr,
            setattr=safe_setattr, delattr=safe_delattr, ToolError=ToolError,
        )

        ns = {"__builtins__": safe_builtins, "__name__": "__main__", "__doc__": None}
        ns["final_answer"] = self.make_final_answer()
        for spec in tools:
            ns[spec["name"]] = self.make_tool(spec["name"], spec.get("params", []), spec.get("doc", ""))
        if "resources" in extras:
            ns["list_resources"] = self.make_tool("__list_resources", [], "List MCP resources.")
            ns["read_resource"] = self.make_tool("__read_resource", [{"name": "uri", "required": True}],
                                                 "Read an MCP resource by URI.")
        if "prompts" in extras:
            ns["list_prompts"] = self.make_tool("__list_prompts", [], "List MCP prompts.")
            ns["get_prompt"] = self.make_tool("__get_prompt", [{"name": "name", "required": True},
                                                               {"name": "arguments", "required": False}],
                                              "Render an MCP prompt.")
        return ns

    def make_final_answer(self):
        worker = self

        def final_answer(answer):
            """Submit the final answer and stop the agent loop."""
            if callable(answer) and not isinstance(answer, str):
                answer = worker.source_of(getattr(answer, "__name__", "")) or str(answer)
            worker.final_answer_value = answer if isinstance(answer, str) else str(answer)
            worker.final_answer_called = True
            if worker.tracer and worker.tracer.active:
                worker.tracer.add({"e": "final", "l": worker.tracer.last_line,
                                   "r": safe_repr(worker.final_answer_value, 0, 160)})
            raise FinalAnswerSignal()

        return final_answer

    def source_of(self, name):
        """Recover ``def name(...)`` source (plus module-level imports) from executed code."""
        for src in reversed(self.sources):
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            imports = [ast.get_source_segment(src, n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                    seg = ast.get_source_segment(src, node)
                    return "\n".join([i for i in imports if i] + [seg])
        return None

    def make_tool(self, name, params, doc):
        worker = self
        names = [p["name"] for p in params]
        required = [p["name"] for p in params if p.get("required")]

        def tool(*args, **kwargs):
            if len(args) > len(names):
                raise TypeError("%s() takes at most %d positional arguments (%d given)" % (name, len(names), len(args)))
            call = dict(zip(names, args))
            for k, v in kwargs.items():
                if k in call:
                    raise TypeError("%s() got multiple values for argument '%s'" % (name, k))
                if names and k not in names:
                    raise TypeError("%s() got an unexpected keyword argument '%s'. Parameters: %s"
                                    % (name, k, ", ".join(names)))
                call[k] = v
            missing = [r for r in required if r not in call]
            if missing:
                raise TypeError("%s() missing required argument(s): %s" % (name, ", ".join(missing)))
            written = "%s(%s)" % (name.lstrip("_"), ", ".join(
                [safe_repr(a, 0, 300) for a in args] + ["%s=%s" % (k, safe_repr(v, 0, 300)) for k, v in kwargs.items()]))
            return worker.call_host_tool(name, call, written)

        tool.__name__ = name.lstrip("_")
        tool.__doc__ = doc
        return tool

    def call_host_tool(self, name, args, written=""):
        # Pause the sandbox timer: MCP work does not count toward the code timeout.
        remaining, _ = signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            self.stdout.flush()
            try:
                json.dumps(args)
            except TypeError as exc:
                raise TypeError("tool arguments must be JSON-serialisable: %s" % exc)
            line = self.tracer.last_line if self.tracer else 0
            if self.tracer and self.tracer.active:
                self.tracer.add({"e": "tool", "l": line, "n": name.lstrip("_"),
                                 "a": {k: safe_repr(v, 0, 80) for k, v in args.items()}})
            self.channel.send({"type": "tool_call", "name": name, "args": args, "line": line, "written": written})
            reply = self.channel.recv()
            if self.tracer and self.tracer.active:
                text = reply.get("result") if reply.get("ok") else reply.get("error")
                self.tracer.add({"e": "tool_result", "l": line, "n": name.lstrip("_"), "ok": bool(reply.get("ok")),
                                 "r": safe_repr(str(text or ""), 0, 160)})
        finally:
            if remaining > 0:
                signal.setitimer(signal.ITIMER_REAL, remaining)
        if reply.get("ok"):
            return reply.get("result")
        raise ToolError(reply.get("error", "tool failed"))

    # ---------------- execution ----------------
    def _on_alarm(self, signum, frame):
        raise SandboxTimeout()

    def format_exception(self, exc):
        tb = traceback.extract_tb(exc.__traceback__)
        frames = [f for f in tb if f.filename == SANDBOX_FILENAME]
        lines = []
        if frames:
            lines.append("Traceback (most recent call last):")
            for f in frames:
                lines.append('  Line %s, in %s' % (f.lineno, f.name))
                if f.line:
                    lines.append("    " + f.line.strip())
        lines.extend(traceback.format_exception_only(type(exc), exc))
        return "\n".join(l.rstrip("\n") for l in lines)

    def run(self, code):
        self.exec_counter += 1
        self.final_answer_called = False
        self.final_answer_value = None
        self.stdout.reset()
        result = {"type": "done", "status": "ok", "error": None, "value": None}
        # Register source so tracebacks can show the offending line.
        linecache.cache[SANDBOX_FILENAME] = (len(code), None, code.splitlines(True), SANDBOX_FILENAME)
        try:
            tree = ast.parse(code, SANDBOX_FILENAME, "exec")
        except SyntaxError as exc:
            result.update(status="syntax_error", error="SyntaxError: %s (line %s)\n    %s" % (
                exc.msg, exc.lineno, (exc.text or "").rstrip()))
            return result
        checker = PolicyChecker(self.allowlist)
        checker.visit(tree)
        if checker.errors:
            result.update(status="security_error",
                          error="SecurityError: code rejected before execution:\n  " + "\n  ".join(checker.errors))
            return result
        self.sources.append(code)

        last_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_expr = ast.Expression(tree.body.pop().value)
        timeout = float(self.config.get("max_execution_time_seconds", 30))
        try:
            signal.setitimer(signal.ITIMER_REAL, timeout)
            if self.tracer:
                self.tracer.start()
            try:
                exec(compile(tree, SANDBOX_FILENAME, "exec"), self.namespace)
                if last_expr is not None:
                    value = eval(compile(last_expr, SANDBOX_FILENAME, "eval"), self.namespace)
                    if value is not None:
                        result["value"] = repr(value)[:5000]
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if self.tracer:
                    self.tracer.stop()
        except FinalAnswerSignal:
            pass
        except SandboxTimeout:
            result.update(status="timeout", error="TimeoutError: execution exceeded %ss" % timeout)
        except SystemExit as exc:
            result.update(status="system_exit", error="SystemExit(%r)" % (exc.code,), exit_code=exc.code
                          if isinstance(exc.code, (int, str, type(None))) else str(exc.code))
        except KeyboardInterrupt:
            result.update(status="keyboard_interrupt", error="KeyboardInterrupt")
        except MemoryError:
            result.update(status="memory", error="MemoryError: memory limit of %s MB exceeded"
                          % self.config.get("max_memory_mb"))
        except RecursionError as exc:
            result.update(status="error", error=self.format_exception(exc))
        except BaseException as exc:  # noqa: BLE001 - everything else is reported to the LLM
            result.update(status="error", error=self.format_exception(exc))
        finally:
            try:
                self.stdout.flush()
            except Exception:
                pass
        if self.final_answer_called:
            # final_answer wins even if user code caught the signal.
            result.update(final_answer=self.final_answer_value, status="final_answer"
                          if result["status"] in ("ok",) else result["status"])
        result["truncated"] = self.stdout.truncated
        if self.tracer:
            result["trace"] = self.tracer.result()
        result["rss_kb"] = self.current_rss_kb()
        return result

    def loop(self):
        init = self.channel.recv()
        self.setup(init)
        self.channel.send({"type": "ready", "rss_kb": self.current_rss_kb()})
        while True:
            try:
                msg = self.channel.recv()
            except EOFError:
                return
            if msg.get("type") == "exec":
                self.channel.send(self.run(msg.get("code", "")))
            elif msg.get("type") == "shutdown":
                return


def main():
    # Ctrl+C is delivered to the host only (the host starts us in a new session too).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    Worker().loop()


if __name__ == "__main__":
    main()
