"""Mariana sandbox HTTP server.

Runs inside the `mariana-sandbox` Docker container and exposes a small HTTP
surface the orchestrator uses to execute code and manipulate files.

Endpoints
---------
POST /exec
    Execute Python / TypeScript / Rust code.  Returns stdout, stderr,
    exit_code, and any files produced inside a per-execution tempdir.

POST /fs/read
    Read a file from /workspace/{user_id}/... with path-traversal protection.

POST /fs/write
    Create / overwrite a file under /workspace/{user_id}/...

POST /fs/list
    Recursive listing of /workspace/{user_id}/...

POST /fs/delete
    Delete a file or an empty directory under /workspace/{user_id}/...

GET /health
    Liveness probe.

Threat model
------------
Untrusted code runs inside this container.  The container itself is the
security boundary:

* `cap_drop: ALL`, `no-new-privileges`, `read_only: true`
* no docker.sock, no route to other services except via the orchestrator
* the FastAPI process runs as root (necessary to `setuid` into the
  `sandbox` user for each exec) but the user code ALWAYS runs as uid 1000
* per-request CPU / memory / file-descriptor rlimits enforced via preexec
* wall-clock timeout via asyncio subprocess with hard-kill on SIGKILL
* no outbound network attached — docker-compose puts this container on an
  isolated internal-only bridge network
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import pathlib
import re
import resource
import secrets
import shutil
import signal
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sandbox")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Shared-secret auth: the orchestrator forwards this header with every call.
# Set via env; sandbox rejects requests without it.
SANDBOX_SHARED_SECRET = os.getenv("SANDBOX_SHARED_SECRET", "")

WORKSPACE_ROOT = pathlib.Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

# Per-execution caps
DEFAULT_WALL_TIMEOUT_SEC = 60
MAX_WALL_TIMEOUT_SEC = 1800           # 30 minutes
DEFAULT_MEM_MB = 1024                 # 1 GB RAM
MAX_MEM_MB = 4096                     # 4 GB RAM (some data tasks need it)
DEFAULT_CPU_SEC = 60
MAX_CPU_SEC = 1800
MAX_STDOUT_BYTES = 2 * 1024 * 1024    # 2 MB — truncated if exceeded
MAX_STDERR_BYTES = 512 * 1024
MAX_CODE_BYTES = 1 * 1024 * 1024      # 1 MB source file

SANDBOX_UID = 1000
SANDBOX_GID = 100  # `users` group

# Identifier validation (user_id, path components)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\- ]{0,254}$")


def _valid_user_id(uid: str) -> bool:
    return bool(_SAFE_ID_RE.match(uid))


def _safe_workspace_path(user_id: str, rel_path: str) -> pathlib.Path:
    """Return absolute path under WORKSPACE_ROOT/{user_id}, refusing traversal."""
    if not _valid_user_id(user_id):
        raise HTTPException(400, f"invalid user_id: {user_id!r}")
    rel = rel_path.lstrip("/").replace("\\", "/")
    if not rel:
        raise HTTPException(400, "empty path")
    # Reject absolute paths, traversal, null bytes
    if "\x00" in rel or ".." in rel.split("/"):
        raise HTTPException(400, f"invalid path: {rel_path!r}")
    # Every component must match the whitelist (tight but permissive enough).
    for comp in rel.split("/"):
        if comp in ("", ".") or not _PATH_COMPONENT_RE.match(comp):
            raise HTTPException(400, f"invalid path component: {comp!r}")
    user_root = (WORKSPACE_ROOT / user_id).resolve()
    user_root.mkdir(parents=True, exist_ok=True)
    # Ensure ownership is correct every time (cheap, idempotent).
    with suppress(PermissionError):
        os.chown(user_root, SANDBOX_UID, SANDBOX_GID)
    candidate = (user_root / rel).resolve()
    # Final guard: resolved path must stay inside user root.
    try:
        candidate.relative_to(user_root)
    except ValueError as exc:
        raise HTTPException(400, f"path escapes workspace: {rel_path!r}") from exc
    return candidate


# -----------------------------------------------------------------------------
# FastAPI app + auth middleware
# -----------------------------------------------------------------------------

app = FastAPI(
    title="Mariana Sandbox",
    version="1.0.0",
    docs_url=None,       # no public docs; internal only
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):  # type: ignore[override]
    if request.url.path == "/health":
        return await call_next(request)
    if not SANDBOX_SHARED_SECRET:
        # Fail closed: if the server was started without a secret, refuse
        # every request except /health.  Deploy script must set it.
        return JSONResponse({"detail": "sandbox misconfigured"}, status_code=503)
    provided = request.headers.get("x-sandbox-secret", "")
    if not secrets.compare_digest(provided, SANDBOX_SHARED_SECRET):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "ts": time.time(),
    }


# -----------------------------------------------------------------------------
# /exec  — run code
# -----------------------------------------------------------------------------

Language = Literal["python", "bash", "typescript", "javascript", "rust"]


class ExecRequest(BaseModel):
    user_id: str
    language: Language = "python"
    code: str = Field(..., max_length=MAX_CODE_BYTES)
    # Optional stdin fed to the child process.
    stdin: str = Field(default="", max_length=1024 * 1024)
    # Working directory, relative to the user workspace.  Defaults to "/".
    cwd: str = Field(default="")
    # Hard caps (clamped by the server to MAX_*).
    wall_timeout_sec: int = Field(default=DEFAULT_WALL_TIMEOUT_SEC, ge=1)
    mem_mb: int = Field(default=DEFAULT_MEM_MB, ge=64)
    cpu_sec: int = Field(default=DEFAULT_CPU_SEC, ge=1)
    # Arbitrary environment variables the agent wants to pass to the child.
    # Sandbox clears PATH etc. and only forwards what's here (plus a minimal
    # base PATH).  Key/value sizes capped.
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("user_id")
    @classmethod
    def _check_uid(cls, v: str) -> str:
        if not _valid_user_id(v):
            raise ValueError(f"invalid user_id: {v!r}")
        return v

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 32:
            raise ValueError("too many env vars (max 32)")
        for k, val in v.items():
            if not re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", k):
                raise ValueError(f"invalid env var name: {k!r}")
            if len(val) > 4096:
                raise ValueError(f"env var {k!r} value too long")
        return v


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    killed: bool
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: list[dict[str, Any]]


def _preexec(mem_mb: int, cpu_sec: int) -> Callable[[], None]:
    """Return a `preexec_fn` that applies rlimits and drops to sandbox user."""

    def _inner() -> None:  # runs in the child after fork()
        # Resource limits
        mem_bytes = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_DATA, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        # 128 MB core dump cap is irrelevant since core dumps disabled below
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # File size cap 256 MB so a runaway write can't fill /tmp
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
        # Process cap — prevents fork bombs.  256 is generous for pytest/matplotlib.
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        # New session so the whole process group can be killed together.
        os.setsid()
        # Drop privileges to sandbox user (server runs as root so it can setgid/setuid).
        if os.geteuid() == 0:
            os.setgid(SANDBOX_GID)
            os.setuid(SANDBOX_UID)

    return _inner


def _pick_runner(language: Language, src_path: pathlib.Path) -> tuple[list[str], str | None]:
    """Map language to (argv, optional compile step marker)."""
    if language == "python":
        return (["python3", "-I", "-u", str(src_path)], None)
    if language == "bash":
        return (["bash", "--noprofile", "--norc", str(src_path)], None)
    if language in ("typescript", "javascript"):
        # Bun runs both TS and JS directly; faster than tsx.
        return (["/usr/local/bin/bun", "run", str(src_path)], None)
    if language == "rust":
        # Compile, then exec the binary.  `_compile_rust` handles this path;
        # this return value is not directly used for Rust.
        return ([], "rust")
    raise HTTPException(400, f"unsupported language: {language}")


def _source_filename(language: Language) -> str:
    return {
        "python": "main.py",
        "bash": "main.sh",
        "typescript": "main.ts",
        "javascript": "main.js",
        "rust": "main.rs",
    }[language]


async def _run_subprocess(
    argv: list[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    stdin: bytes,
    wall_timeout_sec: int,
    mem_mb: int,
    cpu_sec: int,
) -> tuple[bytes, bytes, int, bool, bool, int]:
    """Run a subprocess with a wall-clock timeout.

    Returns (stdout, stderr, rc, timed_out, killed, duration_ms).
    """
    start = time.monotonic()
    # Build base environment.
    base_env = {
        "PATH": "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        # HOME must point at a writable dir: the container rootfs is
        # read-only so /home/sandbox can't be used by tools that try to
        # create cache dirs on first use (e.g. rustup, matplotlib).
        "HOME": "/tmp/sandbox",
        "RUSTUP_HOME": "/usr/local/rustup",
        "CARGO_HOME": "/usr/local/cargo",
        "RUSTUP_TOOLCHAIN": "stable",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": str(cwd),
    }
    base_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=base_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_preexec(mem_mb, cpu_sec),
        close_fds=True,
    )
    timed_out = False
    killed = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=wall_timeout_sec
        )
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        timed_out = True
        killed = True
        # Kill the entire process group (preexec called setsid).
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        # Drain what we have.
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            stdout_bytes, stderr_bytes = b"", b""
        rc = -9
    elapsed_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "exec finished argv=%s rc=%s timed_out=%s elapsed_ms=%s",
        argv[0], rc, timed_out, elapsed_ms,
    )
    return stdout_bytes, stderr_bytes, rc, timed_out, killed, elapsed_ms


async def _compile_rust(src_path: pathlib.Path, *, wall_timeout_sec: int) -> tuple[pathlib.Path | None, str]:
    """Compile Rust source with rustc.  Returns (binary_path or None, stderr).

    v3.6: the sandbox rootfs is read-only, so rustup's default behaviour of
    creating ``~/.rustup`` on first use fails.  We set RUSTUP_HOME /
    CARGO_HOME explicitly to the image-baked paths and point HOME at the
    writable tmpfs so any stray tool cache has somewhere to go.
    """
    binary_path = src_path.with_suffix("")
    proc = await asyncio.create_subprocess_exec(
        "rustc", "-O", "--edition=2021", "-o", str(binary_path), str(src_path),
        cwd=str(src_path.parent),
        env={
            "PATH": "/usr/local/cargo/bin:/usr/bin:/bin",
            "HOME": "/tmp/sandbox",
            "RUSTUP_HOME": "/usr/local/rustup",
            "CARGO_HOME": "/usr/local/cargo",
            "RUSTUP_TOOLCHAIN": "stable",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_preexec(2048, min(wall_timeout_sec, 120)),
        close_fds=True,
    )
    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=wall_timeout_sec)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return None, "rustc: compilation timed out"
    if proc.returncode != 0:
        return None, stderr_bytes.decode("utf-8", "replace")
    return binary_path, ""


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    if len(raw) <= limit:
        return raw.decode("utf-8", "replace"), False
    head = raw[: limit]
    return head.decode("utf-8", "replace") + f"\n\n…[truncated at {limit} bytes]", True


def _snapshot_workspace(user_workspace: pathlib.Path) -> dict[str, float]:
    """Record every file under user_workspace and its mtime.

    Used to detect files the user's code created or modified during a run
    when the code wrote directly into the workspace (outside run_dir).
    """
    snap: dict[str, float] = {}
    if not user_workspace.exists():
        return snap
    try:
        for p in user_workspace.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(user_workspace))
            except ValueError:
                continue
            # Skip anything under _runs/ since those are our own artifact copies.
            if rel.startswith("_runs/") or rel.startswith("_runs\\"):
                continue
            try:
                snap[rel] = p.stat().st_mtime
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace snapshot failed err=%s", exc)
    return snap


MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MB cap per artifact
MAX_ARTIFACT_COUNT = 200


def _collect_artifacts(
    run_dir: pathlib.Path,
    user_workspace: pathlib.Path,
    pre_snapshot: dict[str, float] | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Collect non-source files the user's code produced in run_dir OR in
    user_workspace during the run.

    Two sources are considered:
      (a) files left in the ephemeral run_dir (code wrote relative paths)
      (b) files in user_workspace newer than pre_snapshot (code wrote
          absolute paths like /workspace/<uid>/foo.xlsx)

    For (a) we copy into ``_runs/<ts>_<rand>/`` so they persist.  For (b) the
    file already lives in the workspace, so we surface its workspace_path
    directly without copying.
    Returns a list of {name, workspace_path, size, sha256} dicts, deduped.
    """
    artifacts: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    skip_names = {"main.py", "main.sh", "main.ts", "main.js", "main.rs", "main"}

    # --- (a) files in run_dir -------------------------------------------------
    if run_dir.exists():
        ts = int(time.time())
        rand = secrets.token_hex(3)
        dest_dir = user_workspace / "_runs" / f"{ts}_{rand}"
        files = [p for p in run_dir.rglob("*") if p.is_file()]
        interesting = [p for p in files if p.name not in skip_names and not p.name.endswith(".pyc")]
        if interesting:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for src in interesting:
                if len(artifacts) >= MAX_ARTIFACT_COUNT:
                    break
                try:
                    size = src.stat().st_size
                    if size > MAX_ARTIFACT_BYTES:
                        continue
                    rel = src.relative_to(run_dir)
                    dst = dest_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    with suppress(PermissionError):
                        os.chown(dst, SANDBOX_UID, SANDBOX_GID)
                    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
                    wp = str(dst.relative_to(WORKSPACE_ROOT)).split("/", 1)[1]
                    if wp in seen_paths:
                        continue
                    seen_paths.add(wp)
                    artifacts.append({
                        "name": str(rel),
                        "workspace_path": wp,
                        "size": size,
                        "sha256": digest,
                    })
                except Exception as exc:  # noqa: BLE001
                    log.warning("artifact copy failed name=%s err=%s", src.name, exc)

    # --- (b) files newly written in user_workspace ---------------------------
    if pre_snapshot is not None and user_workspace.exists():
        try:
            post = _snapshot_workspace(user_workspace)
        except Exception as exc:  # noqa: BLE001
            log.warning("post snapshot failed err=%s", exc)
            post = {}
        for rel, mtime in post.items():
            if len(artifacts) >= MAX_ARTIFACT_COUNT:
                break
            old = pre_snapshot.get(rel)
            if old is not None and mtime <= old + 1e-6:
                continue  # unchanged
            full = user_workspace / rel
            if not full.is_file():
                continue
            if full.name in skip_names or full.name.endswith(".pyc"):
                continue
            try:
                size = full.stat().st_size
                if size > MAX_ARTIFACT_BYTES:
                    continue
                data = full.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
            except Exception as exc:  # noqa: BLE001
                log.warning("artifact read failed path=%s err=%s", rel, exc)
                continue
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            artifacts.append({
                "name": pathlib.PurePosixPath(rel).name,
                "workspace_path": rel,
                "size": size,
                "sha256": digest,
            })
    return artifacts


@app.post("/exec")
async def exec_code(req: ExecRequest) -> dict[str, Any]:
    wall = min(req.wall_timeout_sec, MAX_WALL_TIMEOUT_SEC)
    mem = min(req.mem_mb, MAX_MEM_MB)
    cpu = min(req.cpu_sec, MAX_CPU_SEC)

    user_workspace = (WORKSPACE_ROOT / req.user_id).resolve()
    user_workspace.mkdir(parents=True, exist_ok=True)
    with suppress(PermissionError):
        os.chown(user_workspace, SANDBOX_UID, SANDBOX_GID)

    # Resolve cwd (must stay in the workspace).  Empty → workspace root.
    if req.cwd:
        cwd = _safe_workspace_path(req.user_id, req.cwd)
        cwd.mkdir(parents=True, exist_ok=True)
    else:
        cwd = user_workspace

    # Ephemeral run dir so compile artifacts / matplotlib caches don't pollute.
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="run_", dir="/tmp/sandbox"))
    # Snapshot workspace BEFORE the run so we can detect files the user's code
    # writes directly into it (e.g. open('/workspace/<uid>/report.pdf','wb')).
    try:
        _pre_ws_snapshot = _snapshot_workspace(user_workspace)
    except Exception:
        _pre_ws_snapshot = {}
    try:
        os.chmod(run_dir, 0o770)
        with suppress(PermissionError):
            os.chown(run_dir, SANDBOX_UID, SANDBOX_GID)

        # Write source file.
        src_name = _source_filename(req.language)
        src_path = run_dir / src_name
        src_path.write_text(req.code, encoding="utf-8")
        with suppress(PermissionError):
            os.chown(src_path, SANDBOX_UID, SANDBOX_GID)

        # For bash scripts, ensure +x.
        if req.language == "bash":
            os.chmod(src_path, 0o750)

        # Rust requires a compile step.
        if req.language == "rust":
            compile_start = time.monotonic()
            binary_path, rust_stderr = await _compile_rust(src_path, wall_timeout_sec=min(wall, 120))
            compile_ms = int((time.monotonic() - compile_start) * 1000)
            if binary_path is None:
                return {
                    "stdout": "",
                    "stderr": rust_stderr,
                    "exit_code": 1,
                    "duration_ms": compile_ms,
                    "timed_out": False,
                    "killed": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "artifacts": [],
                    "compile_error": True,
                }
            argv = [str(binary_path)]
        else:
            argv, _ = _pick_runner(req.language, src_path)

        stdout, stderr, rc, timed_out, killed, duration_ms = await _run_subprocess(
            argv,
            cwd=run_dir,             # run in the ephemeral dir so writes to ./ don't pollute workspace
            env=req.env,
            stdin=req.stdin.encode("utf-8"),
            wall_timeout_sec=wall,
            mem_mb=mem,
            cpu_sec=cpu,
        )

        # Collect artifacts from BOTH the ephemeral run_dir AND any new files
        # the user's code wrote directly into its workspace.  Persist run_dir
        # files under _runs/, surface workspace writes by their real path.
        artifacts = _collect_artifacts(
            run_dir,
            user_workspace,
            pre_snapshot=_pre_ws_snapshot,
            user_id=req.user_id,
        )

        stdout_str, stdout_trunc = _truncate(stdout, MAX_STDOUT_BYTES)
        stderr_str, stderr_trunc = _truncate(stderr, MAX_STDERR_BYTES)
        return {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "exit_code": rc,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "killed": killed,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "artifacts": artifacts,
        }
    finally:
        # Best-effort cleanup.  Failures are logged but not fatal.
        with suppress(Exception):
            shutil.rmtree(run_dir, ignore_errors=True)


# -----------------------------------------------------------------------------
# /fs/*  — filesystem
# -----------------------------------------------------------------------------


class FsReadRequest(BaseModel):
    user_id: str
    path: str
    # If True, return base64 for binary safety.  Default False → UTF-8 text.
    binary: bool = False
    max_bytes: int = Field(default=1_048_576, ge=1, le=10 * 1024 * 1024)


class FsWriteRequest(BaseModel):
    user_id: str
    path: str
    content: str
    # Whether `content` is base64-encoded binary.
    binary: bool = False
    # Refuse to overwrite an existing file unless explicitly allowed.
    overwrite: bool = True


class FsListRequest(BaseModel):
    user_id: str
    path: str = ""           # relative to workspace root ("" = root)
    recursive: bool = True
    max_entries: int = Field(default=1000, ge=1, le=10_000)


class FsDeleteRequest(BaseModel):
    user_id: str
    path: str


@app.post("/fs/read")
async def fs_read(req: FsReadRequest) -> dict[str, Any]:
    p = _safe_workspace_path(req.user_id, req.path)
    if not p.is_file():
        raise HTTPException(404, f"not a file: {req.path}")
    size = p.stat().st_size
    if size > req.max_bytes:
        raise HTTPException(413, f"file too large: {size} > {req.max_bytes}")
    raw = p.read_bytes()
    if req.binary:
        return {"path": req.path, "size": size, "binary": True,
                "content_b64": base64.b64encode(raw).decode("ascii")}
    try:
        return {"path": req.path, "size": size, "binary": False,
                "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": req.path, "size": size, "binary": True,
                "content_b64": base64.b64encode(raw).decode("ascii"),
                "decoded_as_binary": True}


@app.post("/fs/write")
async def fs_write(req: FsWriteRequest) -> dict[str, Any]:
    p = _safe_workspace_path(req.user_id, req.path)
    if p.exists() and not req.overwrite:
        raise HTTPException(409, f"file exists: {req.path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    if req.binary:
        try:
            data = base64.b64decode(req.content, validate=True)
        except Exception as exc:
            raise HTTPException(400, f"invalid base64: {exc}") from exc
        p.write_bytes(data)
    else:
        p.write_text(req.content, encoding="utf-8")
    with suppress(PermissionError):
        os.chown(p, SANDBOX_UID, SANDBOX_GID)
    return {"path": req.path, "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


@app.post("/fs/list")
async def fs_list(req: FsListRequest) -> dict[str, Any]:
    user_root = (WORKSPACE_ROOT / req.user_id).resolve()
    if not _valid_user_id(req.user_id):
        raise HTTPException(400, f"invalid user_id: {req.user_id}")
    user_root.mkdir(parents=True, exist_ok=True)
    base = user_root if not req.path else _safe_workspace_path(req.user_id, req.path)
    if base.is_file():
        raise HTTPException(400, "path is a file, not a directory")
    entries: list[dict[str, Any]] = []
    iterator = base.rglob("*") if req.recursive else base.iterdir()
    for entry in iterator:
        if len(entries) >= req.max_entries:
            break
        try:
            st = entry.stat()
        except FileNotFoundError:
            continue
        entries.append({
            "path": str(entry.relative_to(user_root)),
            "type": "dir" if entry.is_dir() else "file",
            "size": st.st_size if entry.is_file() else 0,
            "mtime": int(st.st_mtime),
        })
    return {"root": req.path or "/", "entries": entries, "truncated": len(entries) >= req.max_entries}


@app.post("/fs/delete")
async def fs_delete(req: FsDeleteRequest) -> dict[str, Any]:
    p = _safe_workspace_path(req.user_id, req.path)
    if not p.exists():
        raise HTTPException(404, f"not found: {req.path}")
    if p.is_dir():
        try:
            p.rmdir()
        except OSError as exc:
            raise HTTPException(400, f"directory not empty: {exc}") from exc
    else:
        p.unlink()
    return {"path": req.path, "deleted": True}
