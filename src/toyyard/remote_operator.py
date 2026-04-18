from __future__ import annotations

import base64
import hashlib
import json
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, slugify, write_json
from toyyard.warehouse import json_merge


SSH_HOST_PROFILE_SCHEMA = "ssh_host_profile_v1"
REMOTE_OPERATION_RESULT_SCHEMA = "remote_operation_result_v0"
REMOTE_HANDOFF_RESULT_SCHEMA = "remote_handoff_result_v0"
DEFAULT_TRANSPORT_TOPOLOGY = "peer_to_peer"

ALLOWED_TOYYARD_TOP_LEVEL = {"init", "report", "inspect", "export", "import"}
ALLOWED_TOYYARD_IMPORTS = {
    "audio-index-packet",
    "audio-session",
    "motion-handoff",
    "wallpaper-engine-catalog",
    "aiue-results",
    "aiue-motion-results",
    "bodypaint-results",
}


def _require(payload: dict[str, Any], key: str, *, where: str) -> Any:
    value = payload.get(key)
    if value in (None, "", []):
        raise ValueError(f"Missing required {where} field: {key}")
    return value


def load_ssh_host_profile(profile_path: Path) -> dict[str, Any]:
    resolved = profile_path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Host profile must be a JSON object: {resolved}")
    if payload.get("schema_version") != SSH_HOST_PROFILE_SCHEMA:
        raise ValueError(f"Unsupported host profile schema: {payload.get('schema_version')}")

    node = dict(_require(payload, "node", where="host profile"))
    network = dict(_require(payload, "network", where="host profile"))
    ssh = dict(_require(payload, "ssh", where="host profile"))
    filesystem = dict(_require(payload, "filesystem", where="host profile"))

    _require(payload, "profile_name", where="host profile")
    _require(node, "node_id", where="host profile.node")
    _require(node, "os_family", where="host profile.node")
    _require(node, "shell_family", where="host profile.node")
    _require(node, "path_style", where="host profile.node")
    if not network.get("primary_ipv4") and not network.get("primary_hostname"):
        raise ValueError("Host profile.network requires primary_ipv4 or primary_hostname")
    _require(ssh, "port", where="host profile.ssh")
    _require(ssh, "username", where="host profile.ssh")
    auth = dict(_require(ssh, "auth", where="host profile.ssh"))
    _require(auth, "supported_methods", where="host profile.ssh.auth")
    _require(auth, "preferred_method", where="host profile.ssh.auth")
    _require(auth, "credential_ref", where="host profile.ssh.auth")
    _require(filesystem, "scp_path_style", where="host profile.filesystem")
    transfer_profiles = payload.get("transfer_profiles")
    if not isinstance(transfer_profiles, list) or not transfer_profiles:
        raise ValueError("Host profile.transfer_profiles must be a non-empty array")

    payload["_resolved_profile_path"] = str(resolved)
    return payload


def ensure_operator_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    profile_name: str,
    ssh_info_path: str,
    project_root: str,
    os_family: str,
    shell_family: str,
    status: str = "registered",
    transport_topology: str = DEFAULT_TRANSPORT_TOPOLOGY,
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM operator_nodes WHERE profile_name = ? OR node_id = ?",
        (profile_name, node_id),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO operator_nodes (
              node_id, profile_name, ssh_info_path, project_root, os_family,
              shell_family, status, transport_topology, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                profile_name,
                ssh_info_path,
                project_root,
                os_family,
                shell_family,
                status,
                transport_topology,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return conn.execute("SELECT * FROM operator_nodes WHERE id = ?", (cursor.lastrowid,)).fetchone()

    conn.execute(
        """
        UPDATE operator_nodes
        SET node_id = ?, ssh_info_path = ?, project_root = ?, os_family = ?, shell_family = ?,
            status = ?, transport_topology = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            node_id,
            ssh_info_path,
            project_root,
            os_family,
            shell_family,
            status,
            transport_topology,
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM operator_nodes WHERE id = ?", (existing["id"],)).fetchone()


def list_operator_nodes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM operator_nodes ORDER BY profile_name").fetchall()


def get_operator_node(conn: sqlite3.Connection, host_ref: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM operator_nodes WHERE profile_name = ? OR node_id = ?",
        (host_ref, host_ref),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown operator host: {host_ref}")
    return row


def _new_operation_id(kind: str, *parts: object) -> str:
    seed = "|".join([kind, now_iso(), *(str(part) for part in parts if str(part))])
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"op_{slugify(kind)}_{digest}"


def _begin_operator_run(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    node_id: str,
    operation_kind: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    started_at = now_iso()
    operation_id = _new_operation_id(operation_kind, node_id, started_at)
    result_path = str(paths.operator_run_result_path(operation_id))
    conn.execute(
        """
        INSERT INTO operator_runs (
          operation_id, node_id, operation_kind, status, started_at, result_path, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            node_id,
            operation_kind,
            "running",
            started_at,
            result_path,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )
    conn.commit()
    return operation_id, started_at


def _finish_operator_run(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    operation_id: str,
    status: str,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_path = paths.operator_run_result_path(operation_id)
    write_json(result_path, payload)
    existing = conn.execute(
        "SELECT * FROM operator_runs WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"Unknown operator run: {operation_id}")
    conn.execute(
        """
        UPDATE operator_runs
        SET status = ?, ended_at = ?, result_path = ?, metadata_json = ?
        WHERE operation_id = ?
        """,
        (
            status,
            payload.get("ended_at", now_iso()),
            str(result_path),
            json_merge(existing["metadata_json"], metadata),
            operation_id,
        ),
    )
    conn.commit()
    return payload


def _profile_runtime_destination(profile: dict[str, Any]) -> str:
    cred = dict(profile.get("ssh", {}).get("auth", {}).get("credential_ref") or {})
    if cred.get("kind") == "ssh_config_host" and cred.get("ssh_config_host"):
        return str(cred["ssh_config_host"])
    network = dict(profile.get("network") or {})
    ssh = dict(profile.get("ssh") or {})
    host = str(network.get("primary_ipv4") or network.get("primary_hostname") or "").strip()
    user = str(ssh.get("username") or "").strip()
    if not host or not user:
        raise ValueError(f"Host profile {profile.get('profile_name')} is missing ssh destination data")
    return f"{user}@{host}"


def _local_credential_path(profile: dict[str, Any]) -> str | None:
    cred = dict(profile.get("ssh", {}).get("auth", {}).get("credential_ref") or {})
    return cred.get("local_path") or cred.get("windows_path") or cred.get("path") or None


def _peer_credential_path_for_source(profile: dict[str, Any], source_shell: str) -> str | None:
    cred = dict(profile.get("ssh", {}).get("auth", {}).get("credential_ref") or {})
    if source_shell == "bash":
        return cred.get("linux_path") or cred.get("posix_path") or cred.get("path")
    if source_shell == "powershell":
        return cred.get("windows_path") or cred.get("path")
    return cred.get("path")


def _known_hosts_path(profile: dict[str, Any]) -> str | None:
    verification = dict(profile.get("ssh", {}).get("host_key_verification") or {})
    return verification.get("known_hosts_path") or None


def _transfer_profile(profile: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for item in profile.get("transfer_profiles") or []:
        if str(item.get("profile_id") or "") == profile_id:
            return dict(item)
    raise ValueError(f"Unknown transfer profile `{profile_id}` for host {profile.get('profile_name')}")


def _quote_ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _python_exec_fragment(python_exe: str, code: str, shell_family: str) -> str:
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    runner = f"import base64; exec(compile(base64.b64decode('{encoded}'), 'remote_exec.py', 'exec'))"
    if shell_family == "bash":
        return f"{shlex.quote(python_exe)} -c {shlex.quote(runner)}"
    if shell_family == "powershell":
        return f"& {_quote_ps(python_exe)} -c {_quote_ps(runner)}"
    raise ValueError(f"Unsupported shell family: {shell_family}")


def _remote_shell_command(profile: dict[str, Any], command: str) -> list[str]:
    shell_family = str(profile.get("node", {}).get("shell_family") or "")
    if shell_family == "bash":
        return [f"bash -lc {shlex.quote(command)}"]
    if shell_family == "powershell":
        return [f"powershell -NoProfile -Command {_quote_ps(command)}"]
    raise ValueError(f"Unsupported shell family: {shell_family}")


def _ssh_argv(profile: dict[str, Any], remote_command: str) -> list[str]:
    ssh = dict(profile.get("ssh") or {})
    argv = ["ssh", "-p", str(ssh.get("port") or 22)]
    key_path = _local_credential_path(profile)
    if key_path:
        argv.extend(["-i", key_path, "-o", "IdentitiesOnly=yes"])
    argv.extend(["-o", "BatchMode=yes"])
    verification = dict(ssh.get("host_key_verification") or {})
    if verification.get("required", True):
        argv.extend(["-o", "StrictHostKeyChecking=yes"])
    known_hosts = _known_hosts_path(profile)
    if known_hosts:
        argv.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
    argv.append(_profile_runtime_destination(profile))
    argv.extend(_remote_shell_command(profile, remote_command))
    return argv


def _run_subprocess(argv: list[str], *, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _remote_python_command(
    profile: dict[str, Any],
    *,
    python_code: str,
    project_root: str | None = None,
    repo_root: str | None = None,
) -> str:
    shell_family = str(profile.get("node", {}).get("shell_family") or "")
    python_exe = "python" if str(profile.get("node", {}).get("os_family")) == "windows" else "python3"
    prefix = ""
    if shell_family == "bash":
        if project_root:
            prefix = f"export TOYYARD_PROJECT_ROOT={shlex.quote(project_root)}; "
        if repo_root:
            prefix += f"export TOYYARD_REPO_ROOT={shlex.quote(repo_root)}; "
    elif shell_family == "powershell":
        parts: list[str] = []
        if project_root:
            parts.append(f"$env:TOYYARD_PROJECT_ROOT = {_quote_ps(project_root)}")
        if repo_root:
            parts.append(f"$env:TOYYARD_REPO_ROOT = {_quote_ps(repo_root)}")
        prefix = "; ".join(parts) + ("; " if parts else "")
    return prefix + _python_exec_fragment(python_exe, python_code, shell_family)


def _status_python_code() -> str:
    return """
import json, os, pathlib, subprocess, sys
project_root = pathlib.Path(os.environ['TOYYARD_PROJECT_ROOT'])
repo_root = pathlib.Path(os.environ.get('TOYYARD_REPO_ROOT') or project_root)
candidate = project_root / 'repo'
if candidate.is_dir() and (candidate / 'toyyard.py').exists():
    repo_root = candidate
python_exe = sys.executable
def run_git(args):
    try:
        completed = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, check=False)
    except Exception as exc:
        return {'ok': False, 'stdout': '', 'stderr': str(exc)}
    return {'ok': completed.returncode == 0, 'stdout': completed.stdout.strip(), 'stderr': completed.stderr.strip()}
branch = run_git(['git', 'branch', '--show-current'])
commit = run_git(['git', 'rev-parse', '--short', 'HEAD'])
origin = run_git(['git', 'remote', 'get-url', 'origin'])
venv_candidates = [str(path) for path in [repo_root / '.venv', project_root / '.venv'] if path.exists()]
payload = {
    'project_root': str(project_root),
    'repo_root': str(repo_root),
    'repo_exists': repo_root.exists(),
    'git_branch': branch['stdout'],
    'git_commit': commit['stdout'],
    'git_origin': origin['stdout'],
    'python_executable': python_exe,
    'python_version': sys.version.split()[0],
    'venv_candidates': venv_candidates,
    'db_path': str(project_root / '_db' / 'toyyard.sqlite'),
    'db_exists': (project_root / '_db' / 'toyyard.sqlite').exists(),
    'exchange_root': str(project_root / '_exchange'),
    'index_packet_root': str(project_root / '_exchange' / 'index_packets'),
    'index_packet_root_exists': (project_root / '_exchange' / 'index_packets').exists(),
    'toyyard_entry': str(repo_root / 'toyyard.py'),
    'toyyard_entry_exists': (repo_root / 'toyyard.py').exists(),
}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _verify_audio_catalog_python_code(canonical_package_id: str) -> str:
    return f"""
import json, os, pathlib, sqlite3
project_root = pathlib.Path(os.environ['TOYYARD_PROJECT_ROOT'])
db_path = project_root / '_db' / 'toyyard.sqlite'
payload = {{'db_path': str(db_path), 'db_exists': db_path.exists(), 'match': None}}
if db_path.exists():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        \"\"\"
        SELECT samples.canonical_sample_id, packages.canonical_package_id, samples.display_name,
               packages.contract_type, packages.warehouse_status, packages.metadata_json
        FROM packages
        JOIN samples ON samples.id = packages.sample_id
        WHERE packages.canonical_package_id = ?
        LIMIT 1
        \"\"\",
        ({canonical_package_id!r},),
    ).fetchone()
    if row is not None:
        metadata = json.loads(row['metadata_json'] or '{{}}')
        payload['match'] = {{
            'canonical_sample_id': row['canonical_sample_id'],
            'canonical_package_id': row['canonical_package_id'],
            'display_name': row['display_name'],
            'contract_type': row['contract_type'],
            'warehouse_status': row['warehouse_status'],
            'availability': metadata.get('availability', 'local'),
            'replicated_from_node': metadata.get('replicated_from_node', ''),
        }}
    conn.close()
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _resolve_project_roots(host_row: sqlite3.Row, status_payload: dict[str, Any] | None = None) -> tuple[str, str]:
    project_root = str(host_row["project_root"])
    repo_root = ""
    if status_payload:
        repo_root = str(status_payload.get("repo_root") or "")
    if not repo_root:
        repo_root = str(Path(project_root) / "repo")
    return project_root, repo_root


def _remote_status_payload(profile: dict[str, Any], project_root: str) -> dict[str, Any]:
    command = _remote_python_command(profile, python_code=_status_python_code(), project_root=project_root)
    result = _run_subprocess(_ssh_argv(profile, command))
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"].strip() or result["stdout"].strip() or "remote status failed")
    stdout = str(result["stdout"]).strip().splitlines()
    if not stdout:
        raise RuntimeError("remote status returned no output")
    payload = json.loads(stdout[-1])
    if not isinstance(payload, dict):
        raise RuntimeError("remote status returned invalid JSON")
    payload["_transport"] = result
    return payload


def _allow_remote_toyyard_args(args: list[str]) -> None:
    if not args:
        raise ValueError("Remote `toyyard` command requires at least one argument.")
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        raise ValueError("Remote `toyyard` command is empty.")
    top = args[0]
    if top not in ALLOWED_TOYYARD_TOP_LEVEL:
        raise ValueError(f"Remote `toyyard` command `{top}` is not allowed in Remote Operator Mode v1.")
    if top == "import" and (len(args) < 2 or args[1] not in ALLOWED_TOYYARD_IMPORTS):
        raise ValueError("Remote `toyyard import` only allows approved subcommands.")


def _toyyard_remote_command(profile: dict[str, Any], project_root: str, repo_root: str, toyyard_args: list[str]) -> str:
    normalized_args = list(toyyard_args)
    if normalized_args and normalized_args[0] == "--":
        normalized_args = normalized_args[1:]
    _allow_remote_toyyard_args(normalized_args)
    shell_family = str(profile.get("node", {}).get("shell_family") or "")
    python_bin = "python" if profile.get("node", {}).get("os_family") == "windows" else "python3"
    argv = [python_bin, "toyyard.py", "--root", project_root, *normalized_args]
    if shell_family == "bash":
        return "cd " + shlex.quote(repo_root) + " && " + " ".join(shlex.quote(part) for part in argv)
    if shell_family == "powershell":
        pieces = " ".join(_quote_ps(part) for part in argv)
        return f"Set-Location {_quote_ps(repo_root)}; & {pieces}"
    raise ValueError(f"Unsupported shell family: {shell_family}")


def register_remote_host(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    profile_path: Path,
    project_root: str,
    transport_topology: str = DEFAULT_TRANSPORT_TOPOLOGY,
) -> dict[str, Any]:
    profile = load_ssh_host_profile(profile_path)
    host_row = ensure_operator_node(
        conn,
        node_id=str(profile["node"]["node_id"]),
        profile_name=str(profile["profile_name"]),
        ssh_info_path=str(Path(profile["_resolved_profile_path"])),
        project_root=project_root,
        os_family=str(profile["node"]["os_family"]),
        shell_family=str(profile["node"]["shell_family"]),
        status="registered",
        transport_topology=transport_topology,
        metadata={
            "primary_ipv4": str(profile.get("network", {}).get("primary_ipv4") or ""),
            "primary_hostname": str(profile.get("network", {}).get("primary_hostname") or ""),
            "path_style": str(profile.get("node", {}).get("path_style") or ""),
        },
    )
    return {
        "profile_name": host_row["profile_name"],
        "node_id": host_row["node_id"],
        "project_root": host_row["project_root"],
        "status": host_row["status"],
        "transport_topology": host_row["transport_topology"],
        "ssh_info_path": host_row["ssh_info_path"],
    }


def remote_probe(conn: sqlite3.Connection, paths: ProjectPaths, *, host_ref: str) -> dict[str, Any]:
    host_row = get_operator_node(conn, host_ref)
    profile = load_ssh_host_profile(Path(host_row["ssh_info_path"]))
    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(host_row["node_id"]),
        operation_kind="remote_probe",
        metadata={"profile_name": host_row["profile_name"]},
    )
    transport = _run_subprocess(_ssh_argv(profile, "hostname"), timeout=30)
    status = "pass" if transport["returncode"] == 0 else "fail"
    payload = {
        "schema_version": REMOTE_OPERATION_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "probe",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "host": {
            "profile_name": host_row["profile_name"],
            "node_id": host_row["node_id"],
        },
        "transport": transport,
        "observations": {
            "hostname_stdout": str(transport["stdout"]).strip(),
        },
    }
    _finish_operator_run(
        conn,
        paths,
        operation_id=operation_id,
        status=status,
        payload=payload,
        metadata={"last_probe_status": status},
    )
    conn.execute("UPDATE operator_nodes SET status = ? WHERE id = ?", (status, host_row["id"]))
    conn.commit()
    return payload


def remote_status(conn: sqlite3.Connection, paths: ProjectPaths, *, host_ref: str) -> dict[str, Any]:
    host_row = get_operator_node(conn, host_ref)
    profile = load_ssh_host_profile(Path(host_row["ssh_info_path"]))
    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(host_row["node_id"]),
        operation_kind="remote_status",
        metadata={"profile_name": host_row["profile_name"]},
    )
    try:
        status_payload = _remote_status_payload(profile, str(host_row["project_root"]))
        status = "pass"
    except Exception as exc:
        status_payload = {"error": str(exc)}
        status = "fail"
    payload = {
        "schema_version": REMOTE_OPERATION_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "status",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "host": {
            "profile_name": host_row["profile_name"],
            "node_id": host_row["node_id"],
            "project_root": host_row["project_root"],
        },
        "status_payload": status_payload,
    }
    metadata = {"last_status": status}
    if status == "pass":
        metadata["repo_root"] = status_payload.get("repo_root", "")
        metadata["git_commit"] = status_payload.get("git_commit", "")
    _finish_operator_run(conn, paths, operation_id=operation_id, status=status, payload=payload, metadata=metadata)
    if status == "pass":
        conn.execute("UPDATE operator_nodes SET status = ? WHERE id = ?", ("reachable", host_row["id"]))
        conn.commit()
    return payload


def remote_sync_code(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    host_ref: str,
    branch: str,
    install_editable: bool = False,
) -> dict[str, Any]:
    host_row = get_operator_node(conn, host_ref)
    profile = load_ssh_host_profile(Path(host_row["ssh_info_path"]))
    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(host_row["node_id"]),
        operation_kind="remote_sync_code",
        metadata={"profile_name": host_row["profile_name"], "branch": branch, "install_editable": install_editable},
    )
    status_before = _remote_status_payload(profile, str(host_row["project_root"]))
    project_root, repo_root = _resolve_project_roots(host_row, status_before)
    shell_family = str(profile.get("node", {}).get("shell_family") or "")
    if shell_family == "bash":
        sync_lines = [
            f"cd {shlex.quote(repo_root)}",
            "git fetch --all --prune",
            f"git checkout {shlex.quote(branch)}",
            f"git pull --ff-only origin {shlex.quote(branch)} || git pull --ff-only",
        ]
        if install_editable:
            sync_lines.extend(
                [
                    "PYTHON_BIN=$(command -v python3 || command -v python)",
                    '[ -d ".venv" ] || "$PYTHON_BIN" -m venv .venv',
                    ". .venv/bin/activate",
                    "python -m pip install -e .",
                ]
            )
        command = "; ".join(sync_lines)
    elif shell_family == "powershell":
        sync_lines = [
            f"Set-Location {_quote_ps(repo_root)}",
            "git fetch --all --prune",
            f"git checkout {_quote_ps(branch)}",
            f"git pull --ff-only origin {_quote_ps(branch)}",
        ]
        if install_editable:
            sync_lines.extend(
                [
                    "$py = 'python'",
                    "if (-not (Test-Path '.venv')) { & $py -m venv .venv }",
                    "& '.venv\\Scripts\\python.exe' -m pip install -e .",
                ]
            )
        command = "; ".join(sync_lines)
    else:
        raise ValueError(f"Unsupported shell family: {shell_family}")
    transport = _run_subprocess(_ssh_argv(profile, command), timeout=300)
    status_after: dict[str, Any]
    if transport["returncode"] == 0:
        try:
            status_after = _remote_status_payload(profile, project_root)
            status = "pass"
        except Exception as exc:
            status_after = {"error": str(exc)}
            status = "fail"
    else:
        status_after = {}
        status = "fail"
    payload = {
        "schema_version": REMOTE_OPERATION_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "sync_code",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "host": {
            "profile_name": host_row["profile_name"],
            "node_id": host_row["node_id"],
        },
        "requested_branch": branch,
        "install_editable": install_editable,
        "status_before": status_before,
        "transport": transport,
        "status_after": status_after,
    }
    _finish_operator_run(
        conn,
        paths,
        operation_id=operation_id,
        status=status,
        payload=payload,
        metadata={"last_sync_status": status, "requested_branch": branch},
    )
    return payload


def remote_toyyard(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    host_ref: str,
    toyyard_args: list[str],
) -> dict[str, Any]:
    host_row = get_operator_node(conn, host_ref)
    profile = load_ssh_host_profile(Path(host_row["ssh_info_path"]))
    normalized_args = list(toyyard_args)
    if normalized_args and normalized_args[0] == "--":
        normalized_args = normalized_args[1:]
    _allow_remote_toyyard_args(normalized_args)
    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(host_row["node_id"]),
        operation_kind="remote_toyyard",
        metadata={"profile_name": host_row["profile_name"], "toyyard_args": normalized_args},
    )
    status_payload = _remote_status_payload(profile, str(host_row["project_root"]))
    project_root, repo_root = _resolve_project_roots(host_row, status_payload)
    command = _toyyard_remote_command(profile, project_root, repo_root, normalized_args)
    transport = _run_subprocess(_ssh_argv(profile, command), timeout=300)
    parsed_json: dict[str, Any] | None = None
    stdout_lines = str(transport["stdout"]).strip().splitlines()
    if stdout_lines:
        try:
            parsed = json.loads(stdout_lines[-1])
            if isinstance(parsed, dict):
                parsed_json = parsed
        except json.JSONDecodeError:
            parsed_json = None
    status = "pass" if transport["returncode"] == 0 else "fail"
    payload = {
        "schema_version": REMOTE_OPERATION_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "remote_toyyard",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "host": {
            "profile_name": host_row["profile_name"],
            "node_id": host_row["node_id"],
        },
        "toyyard_args": normalized_args,
        "transport": transport,
        "parsed_json": parsed_json,
    }
    _finish_operator_run(conn, paths, operation_id=operation_id, status=status, payload=payload)
    return payload


def remote_handoff_audio_index(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    source_host_ref: str,
    target_host_ref: str,
    session_id: str,
    source_node_id: str,
    target_transfer_profile: str,
    import_target: bool = True,
) -> dict[str, Any]:
    source_host = get_operator_node(conn, source_host_ref)
    target_host = get_operator_node(conn, target_host_ref)
    source_profile = load_ssh_host_profile(Path(source_host["ssh_info_path"]))
    target_profile = load_ssh_host_profile(Path(target_host["ssh_info_path"]))
    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(source_host["node_id"]),
        operation_kind="remote_handoff_audio_index",
        metadata={
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "session_id": session_id,
            "source_node_id": source_node_id,
        },
    )

    export_result = remote_toyyard(
        conn,
        paths,
        host_ref=source_host_ref,
        toyyard_args=[
            "export",
            "audio-index-packet",
            "--session-id",
            session_id,
            "--node-id",
            source_node_id,
        ],
    )
    packet_json = dict(export_result.get("parsed_json") or {})
    packet_path = str(packet_json.get("packet_path") or "").strip()
    if export_result["status"] != "pass" or not packet_path:
        payload = {
            "schema_version": REMOTE_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "audio_index_handoff",
            "status": "fail",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "session_id": session_id,
            "export_result": export_result,
            "failure_stage": "export",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)

    target_transfer = _transfer_profile(target_profile, target_transfer_profile)
    source_shell = str(source_profile.get("node", {}).get("shell_family") or "")
    if source_shell != "bash":
        raise ValueError("Remote audio handoff currently requires a bash source host for peer-to-peer scp.")
    peer_key_path = _peer_credential_path_for_source(target_profile, source_shell)
    if not peer_key_path:
        raise ValueError(
            f"Target host profile `{target_profile.get('profile_name')}` is missing source-side credential path for bash scp."
        )

    source_hash_cmd = f"sha256sum {shlex.quote(packet_path)} | awk '{{print $1}}'"
    source_hash_result = _run_subprocess(_ssh_argv(source_profile, source_hash_cmd), timeout=60)
    source_hash = ""
    if source_hash_result["returncode"] == 0 and str(source_hash_result["stdout"]).strip():
        source_hash = str(source_hash_result["stdout"]).strip().splitlines()[-1]

    target_dir = str(target_transfer.get("remote_directory") or "")
    target_host_name = str(target_profile.get("network", {}).get("primary_ipv4") or target_profile.get("network", {}).get("primary_hostname") or "")
    target_user = str(target_profile.get("ssh", {}).get("username") or "")
    target_port = str(target_profile.get("ssh", {}).get("port") or 22)
    scp_parts = [
        "scp",
        "-P",
        target_port,
        "-i",
        shlex.quote(str(peer_key_path)),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        shlex.quote(packet_path),
        f"{shlex.quote(target_user)}@{shlex.quote(target_host_name)}:{shlex.quote(target_dir.rstrip('/') + '/')}",
    ]
    source_known_hosts = dict(target_profile.get("ssh", {}).get("host_key_verification") or {}).get("source_known_hosts_path")
    if source_known_hosts:
        scp_parts[8:8] = ["-o", f"UserKnownHostsFile={source_known_hosts}"]
    scp_command = " ".join(scp_parts)
    transfer_transport = _run_subprocess(_ssh_argv(source_profile, scp_command), timeout=300)
    packet_name = Path(packet_path).name
    target_packet_path = str(target_dir.rstrip("/")) + "/" + packet_name if target_dir else packet_name

    target_hash = ""
    target_import_result: dict[str, Any] | None = None
    verify_result: dict[str, Any] | None = None
    if transfer_transport["returncode"] == 0:
        target_hash_cmd = f"Get-FileHash -Algorithm SHA256 {_quote_ps(target_packet_path.replace('/', '\\'))} | Select-Object -ExpandProperty Hash"
        target_hash_transport = _run_subprocess(_ssh_argv(target_profile, target_hash_cmd), timeout=120)
        if target_hash_transport["returncode"] == 0 and str(target_hash_transport["stdout"]).strip():
            target_hash = str(target_hash_transport["stdout"]).strip().splitlines()[-1]
        if import_target:
            target_import_result = remote_toyyard(
                conn,
                paths,
                host_ref=target_host_ref,
                toyyard_args=[
                    "import",
                    "audio-index-packet",
                    "--packet-path",
                    target_packet_path.replace("/", "\\")
                    if str(target_profile.get("node", {}).get("os_family")) == "windows"
                    else target_packet_path,
                ],
            )
            if target_import_result["status"] == "pass":
                target_verify_command = _remote_python_command(
                    target_profile,
                    python_code=_verify_audio_catalog_python_code(str(packet_json.get("canonical_package_id") or "")),
                    project_root=str(target_host["project_root"]),
                )
                verify_transport = _run_subprocess(_ssh_argv(target_profile, target_verify_command), timeout=120)
                verify_payload: dict[str, Any] = {}
                if verify_transport["returncode"] == 0 and str(verify_transport["stdout"]).strip():
                    verify_payload = json.loads(str(verify_transport["stdout"]).strip().splitlines()[-1])
                verify_result = {
                    "transport": verify_transport,
                    "payload": verify_payload,
                    "acceptance": {
                        "availability_remote_index_only": str(verify_payload.get("match", {}).get("availability") or "") == "remote_index_only",
                        "replicated_from_node_match": str(verify_payload.get("match", {}).get("replicated_from_node") or "") == source_node_id,
                    },
                }

    status = "pass"
    failure_stage = ""
    if transfer_transport["returncode"] != 0:
        status = "fail"
        failure_stage = "transfer"
    elif import_target and (not target_import_result or target_import_result["status"] != "pass"):
        status = "fail"
        failure_stage = "target_import"
    elif import_target and verify_result and not all(verify_result["acceptance"].values()):
        status = "fail"
        failure_stage = "target_verify"

    payload = {
        "schema_version": REMOTE_HANDOFF_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "audio_index_handoff",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "source_host": source_host["profile_name"],
        "target_host": target_host["profile_name"],
        "session_id": session_id,
        "source_node_id": source_node_id,
        "target_transfer_profile": target_transfer_profile,
        "export_result": export_result,
        "source_packet_path": packet_path,
        "source_packet_sha256": source_hash,
        "transfer": {
            "command_transport": transfer_transport,
            "target_packet_path": target_packet_path,
            "target_packet_sha256": target_hash,
        },
        "target_import_result": target_import_result,
        "verify_result": verify_result,
        "failure_stage": failure_stage,
    }
    return _finish_operator_run(conn, paths, operation_id=operation_id, status=status, payload=payload)
