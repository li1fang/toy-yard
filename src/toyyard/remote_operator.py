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
REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA = "remote_export_handoff_result_v0"
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


def _join_remote_path(profile: dict[str, Any], base_dir: str, leaf: str) -> str:
    base = str(base_dir or "").rstrip("/\\")
    tail = str(leaf or "").strip("/\\")
    path_style = str(profile.get("node", {}).get("path_style") or "posix")
    if "/" in base and "\\" not in base:
        sep = "/"
    elif "\\" in base and "/" not in base:
        sep = "\\"
    else:
        sep = "\\" if path_style == "windows" else "/"
    if not base:
        return tail
    if not tail:
        return base
    return base + sep + tail


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


def _scp_command_from_source(
    *,
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
    source_path: str,
    target_path: str,
    recursive: bool = False,
) -> str:
    source_shell = str(source_profile.get("node", {}).get("shell_family") or "")
    peer_key_path = _peer_credential_path_for_source(target_profile, source_shell)
    if not peer_key_path:
        raise ValueError(
            f"Target host profile `{target_profile.get('profile_name')}` is missing source-side credential path for {source_shell} scp."
        )

    target_host_name = str(target_profile.get("network", {}).get("primary_ipv4") or target_profile.get("network", {}).get("primary_hostname") or "")
    target_user = str(target_profile.get("ssh", {}).get("username") or "")
    target_port = str(target_profile.get("ssh", {}).get("port") or 22)
    destination = f"{target_user}@{target_host_name}:{target_path}"
    host_verification = dict(target_profile.get("ssh", {}).get("host_key_verification") or {})
    source_known_hosts = host_verification.get("source_known_hosts_path") or host_verification.get("known_hosts_path")

    if source_shell == "bash":
        parts = ["scp"]
        if recursive:
            parts.append("-r")
        parts.extend(
            [
                "-P",
                target_port,
                "-i",
                shlex.quote(str(peer_key_path)),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
        )
        if source_known_hosts:
            parts.extend(["-o", f"UserKnownHostsFile={source_known_hosts}"])
        parts.extend([shlex.quote(source_path), shlex.quote(destination)])
        return " ".join(parts)

    if source_shell == "powershell":
        parts = ["scp"]
        if recursive:
            parts.append("-r")
        parts.extend(
            [
                "-P",
                target_port,
                "-i",
                _quote_ps(str(peer_key_path)),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
        )
        if source_known_hosts:
            parts.extend(["-o", _quote_ps(f"UserKnownHostsFile={source_known_hosts}")])
        parts.extend([_quote_ps(source_path), _quote_ps(destination)])
        return " ".join(parts)

    raise ValueError(f"Unsupported source shell for scp handoff: {source_shell}")


def _rsync_command_from_source(
    *,
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
    source_path: str,
    target_path: str,
) -> str:
    source_shell = str(source_profile.get("node", {}).get("shell_family") or "")
    target_shell = str(target_profile.get("node", {}).get("shell_family") or "")
    if source_shell != "bash" or target_shell != "bash":
        raise ValueError("rsync handoff currently requires bash on both source and target hosts.")

    peer_key_path = _peer_credential_path_for_source(target_profile, source_shell)
    if not peer_key_path:
        raise ValueError(
            f"Target host profile `{target_profile.get('profile_name')}` is missing source-side credential path for bash rsync."
        )

    target_host_name = str(target_profile.get("network", {}).get("primary_ipv4") or target_profile.get("network", {}).get("primary_hostname") or "")
    target_user = str(target_profile.get("ssh", {}).get("username") or "")
    target_port = str(target_profile.get("ssh", {}).get("port") or 22)
    destination = f"{target_user}@{target_host_name}:{target_path}"
    host_verification = dict(target_profile.get("ssh", {}).get("host_key_verification") or {})
    source_known_hosts = host_verification.get("source_known_hosts_path") or host_verification.get("known_hosts_path")

    ssh_parts = [
        "ssh",
        "-p",
        target_port,
        "-i",
        str(peer_key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    if source_known_hosts:
        ssh_parts.extend(["-o", f"UserKnownHostsFile={source_known_hosts}"])
    ssh_transport = " ".join(shlex.quote(part) for part in ssh_parts)
    parts = [
        "rsync",
        "-az",
        "--partial",
        "--append-verify",
        "-e",
        shlex.quote(ssh_transport),
        shlex.quote(source_path),
        shlex.quote(destination),
    ]
    return " ".join(parts)


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


def _ensure_remote_directory(profile: dict[str, Any], remote_dir: str) -> dict[str, Any]:
    shell_family = str(profile.get("node", {}).get("shell_family") or "")
    if shell_family == "bash":
        command = f"mkdir -p {shlex.quote(remote_dir)}"
    elif shell_family == "powershell":
        command = f"New-Item -ItemType Directory -Force -Path {_quote_ps(remote_dir)} | Out-Null"
    else:
        raise ValueError(f"Unsupported shell family: {shell_family}")
    return _run_subprocess(_ssh_argv(profile, command), timeout=120)


def _status_python_code() -> str:
    return """
import json, os, pathlib, shutil, subprocess, sys
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
    'transport_capabilities': {
        'scp_available': bool(shutil.which('scp')),
        'sftp_available': bool(shutil.which('sftp')),
        'rsync_available': bool(shutil.which('rsync')),
    },
}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _verify_bodypaint_export_python_code(export_root: str) -> str:
    return f"""
import json, pathlib
export_root = pathlib.Path({export_root!r})
summary_dir = export_root / 'summary'
paths = {{
    'summary_path': summary_dir / 'bodypaint_suite_summary.json',
    'registry_path': summary_dir / 'bodypaint_packet_registry.json',
    'packet_check_path': summary_dir / 'bodypaint_packet_check.json',
    'communication_signal_path': summary_dir / 'communication_signal.json',
}}
payload = {{
    'export_root': str(export_root),
    'required_files': {{name: path.exists() for name, path in paths.items()}},
    'registry_packet_count': 0,
    'packet_check_status': '',
    'communication_signal_status': '',
}}
if paths['registry_path'].exists():
    registry = json.loads(paths['registry_path'].read_text(encoding='utf-8-sig'))
    payload['registry_packet_count'] = len(registry.get('packets') or [])
if paths['packet_check_path'].exists():
    packet_check = json.loads(paths['packet_check_path'].read_text(encoding='utf-8-sig'))
    payload['packet_check_status'] = str(packet_check.get('status') or '')
if paths['communication_signal_path'].exists():
    signal = json.loads(paths['communication_signal_path'].read_text(encoding='utf-8-sig'))
    payload['communication_signal_status'] = str(signal.get('status') or '')
payload['acceptance'] = {{
    'summary_exists': payload['required_files']['summary_path'],
    'registry_exists': payload['required_files']['registry_path'],
    'packet_check_exists': payload['required_files']['packet_check_path'],
    'communication_signal_exists': payload['required_files']['communication_signal_path'],
    'has_packets': payload['registry_packet_count'] > 0,
}}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _directory_tree_manifest_python_code(root_path: str) -> str:
    return f"""
import hashlib, json, pathlib
root = pathlib.Path({root_path!r})
files = []
total_bytes = 0
if root.exists():
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        data_hash = hashlib.sha256()
        size = 0
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                size += len(chunk)
                data_hash.update(chunk)
        rel = path.relative_to(root).as_posix()
        total_bytes += size
        files.append({{
            'path': rel,
            'size_bytes': size,
            'sha256': data_hash.hexdigest(),
        }})
tree_material = json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
payload = {{
    'root': str(root),
    'exists': root.exists(),
    'file_count': len(files),
    'total_bytes': total_bytes,
    'tree_hash': hashlib.sha256(tree_material).hexdigest(),
    'files': files,
}}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _bodypaint_packet_fingerprint_python_code(root_path: str) -> str:
    return f"""
import hashlib, json, pathlib

VOLATILE_KEYS = {{'generated_at_utc'}}

def normalize_json(value):
    if isinstance(value, dict):
        return {{
            key: normalize_json(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_KEYS
        }}
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    return value

root = pathlib.Path({root_path!r})
files = []
if root.exists():
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() == '.json':
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
            normalized = normalize_json(payload)
            material = json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
            files.append({{
                'path': rel,
                'kind': 'json',
                'fingerprint_sha256': hashlib.sha256(material).hexdigest(),
            }})
        else:
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            files.append({{
                'path': rel,
                'kind': 'binary',
                'fingerprint_sha256': digest.hexdigest(),
            }})
material = json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
payload = {{
    'root': str(root),
    'exists': root.exists(),
    'file_count': len(files),
    'stable_fingerprint': hashlib.sha256(material).hexdigest(),
    'volatile_keys_ignored': sorted(VOLATILE_KEYS),
    'files': files,
}}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _promote_directory_python_code(staged_root: str, final_root: str, operation_id: str) -> str:
    return f"""
import json, pathlib, shutil
staged = pathlib.Path({staged_root!r})
final = pathlib.Path({final_root!r})
operation_id = {operation_id!r}
payload = {{
    'staged_root': str(staged),
    'final_root': str(final),
    'operation_id': operation_id,
    'staged_exists': staged.exists(),
    'final_existed': final.exists(),
    'backup_path': '',
    'action': '',
    'promoted': False,
}}
if not staged.exists():
    payload['action'] = 'missing_staged_root'
else:
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        previous_root = final.parent / '.previous'
        previous_root.mkdir(parents=True, exist_ok=True)
        backup = previous_root / f"{{final.name}}__{{operation_id}}"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(final), str(backup))
        payload['backup_path'] = str(backup)
    shutil.move(str(staged), str(final))
    payload['action'] = 'promoted'
    payload['promoted'] = final.exists()
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _file_sha256_python_code(file_path: str) -> str:
    return f"""
import hashlib, json, pathlib
path = pathlib.Path({file_path!r})
payload = {{
    'path': str(path),
    'exists': path.exists(),
    'sha256': '',
    'size_bytes': path.stat().st_size if path.exists() and path.is_file() else 0,
}}
if path.exists() and path.is_file():
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    payload['sha256'] = digest.hexdigest()
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _transport_capability_python_code() -> str:
    return """
import json, shutil
payload = {
    'scp_available': bool(shutil.which('scp')),
    'sftp_available': bool(shutil.which('sftp')),
    'rsync_available': bool(shutil.which('rsync')),
}
print(json.dumps(payload, ensure_ascii=True))
""".strip()


def _parse_last_json_stdout(transport: dict[str, Any]) -> dict[str, Any]:
    stdout = str(transport.get("stdout") or "").strip()
    if not stdout:
        return {}
    return json.loads(stdout.splitlines()[-1])


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


def _remote_transport_capabilities(profile: dict[str, Any], project_root: str) -> dict[str, Any]:
    command = _remote_python_command(profile, python_code=_transport_capability_python_code(), project_root=project_root)
    transport = _run_subprocess(_ssh_argv(profile, command), timeout=60)
    payload = _parse_last_json_stdout(transport) if transport["returncode"] == 0 else {}
    capabilities = {
        "scp_available": bool(payload.get("scp_available")),
        "sftp_available": bool(payload.get("sftp_available")),
        "rsync_available": bool(payload.get("rsync_available")),
    }
    return {
        "transport": transport,
        "payload": payload,
        "capabilities": capabilities,
    }


def _select_peer_copy_transport(
    *,
    source_profile: dict[str, Any],
    source_project_root: str,
    target_profile: dict[str, Any],
    target_project_root: str,
    source_path: str,
    target_path: str,
    recursive: bool = False,
) -> dict[str, Any]:
    source_shell = str(source_profile.get("node", {}).get("shell_family") or "")
    target_shell = str(target_profile.get("node", {}).get("shell_family") or "")
    source_caps = _remote_transport_capabilities(source_profile, source_project_root)
    target_caps = _remote_transport_capabilities(target_profile, target_project_root)
    source_rsync = bool(source_caps["capabilities"].get("rsync_available"))
    target_rsync = bool(target_caps["capabilities"].get("rsync_available"))

    if source_shell == "bash" and target_shell == "bash" and source_rsync and target_rsync:
        command = _rsync_command_from_source(
            source_profile=source_profile,
            target_profile=target_profile,
            source_path=source_path,
            target_path=target_path,
        )
        return {
            "selected_transport": "rsync",
            "resume_supported": True,
            "selection_reason": "bash_source_and_target_with_rsync_available",
            "command": command,
            "source_capabilities": source_caps,
            "target_capabilities": target_caps,
        }

    if source_shell != "bash" or target_shell != "bash":
        selection_reason = "rsync_requires_bash_on_both_nodes"
    elif not source_rsync:
        selection_reason = "source_host_lacks_rsync"
    else:
        selection_reason = "target_host_lacks_rsync"

    command = _scp_command_from_source(
        source_profile=source_profile,
        target_profile=target_profile,
        source_path=source_path,
        target_path=target_path,
        recursive=recursive,
    )
    return {
        "selected_transport": "scp",
        "resume_supported": False,
        "selection_reason": selection_reason,
        "command": command,
        "source_capabilities": source_caps,
        "target_capabilities": target_caps,
    }


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

    source_hash_command = _remote_python_command(
        source_profile,
        python_code=_file_sha256_python_code(packet_path),
        project_root=str(source_host["project_root"]),
    )
    source_hash_transport = _run_subprocess(_ssh_argv(source_profile, source_hash_command), timeout=120)
    source_hash_payload = _parse_last_json_stdout(source_hash_transport) if source_hash_transport["returncode"] == 0 else {}
    source_hash = str(source_hash_payload.get("sha256") or "")

    target_dir = str(target_transfer.get("remote_directory") or "")
    transfer_plan = _select_peer_copy_transport(
        source_profile=source_profile,
        source_project_root=str(source_host["project_root"]),
        target_profile=target_profile,
        target_project_root=str(target_host["project_root"]),
        source_path=packet_path,
        target_path=target_dir.rstrip("/") + "/",
        recursive=False,
    )
    transfer_transport = _run_subprocess(_ssh_argv(source_profile, str(transfer_plan["command"])), timeout=300)
    packet_name = Path(packet_path).name
    target_packet_path = str(target_dir.rstrip("/")) + "/" + packet_name if target_dir else packet_name

    target_hash = ""
    target_import_result: dict[str, Any] | None = None
    verify_result: dict[str, Any] | None = None
    if transfer_transport["returncode"] == 0:
        target_hash_command = _remote_python_command(
            target_profile,
            python_code=_file_sha256_python_code(target_packet_path),
            project_root=str(target_host["project_root"]),
        )
        target_hash_transport = _run_subprocess(_ssh_argv(target_profile, target_hash_command), timeout=120)
        target_hash_payload = _parse_last_json_stdout(target_hash_transport) if target_hash_transport["returncode"] == 0 else {}
        target_hash = str(target_hash_payload.get("sha256") or "")
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
        "source_hash_result": {
            "transport": source_hash_transport,
            "payload": source_hash_payload,
        },
        "transfer": {
            "selected_transport": transfer_plan["selected_transport"],
            "resume_supported": transfer_plan["resume_supported"],
            "selection_reason": transfer_plan["selection_reason"],
            "source_capabilities": transfer_plan["source_capabilities"],
            "target_capabilities": transfer_plan["target_capabilities"],
            "command_transport": transfer_transport,
            "target_packet_path": target_packet_path,
            "target_packet_sha256": target_hash,
        },
        "target_import_result": target_import_result,
        "verify_result": verify_result,
        "failure_stage": failure_stage,
    }
    return _finish_operator_run(conn, paths, operation_id=operation_id, status=status, payload=payload)


def remote_handoff_bodypaint_view(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    source_host_ref: str,
    target_host_ref: str,
    profile: str,
    sample_ref: str | None = None,
    package_refs: list[str] | None = None,
    aiue_pmx_profile: str | None = None,
    target_transfer_profile: str,
) -> dict[str, Any]:
    source_host = get_operator_node(conn, source_host_ref)
    target_host = get_operator_node(conn, target_host_ref)
    source_profile = load_ssh_host_profile(Path(source_host["ssh_info_path"]))
    target_profile = load_ssh_host_profile(Path(target_host["ssh_info_path"]))
    package_refs = [str(ref) for ref in (package_refs or []) if str(ref).strip()]
    if not sample_ref and not package_refs:
        raise ValueError("BodyPaint handoff requires --sample or at least one --package.")

    operation_id, started_at = _begin_operator_run(
        conn,
        paths,
        node_id=str(source_host["node_id"]),
        operation_kind="remote_handoff_bodypaint_view",
        metadata={
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "sample_ref": sample_ref or "",
            "package_refs": package_refs,
            "aiue_pmx_profile": aiue_pmx_profile or "",
        },
    )

    export_args = ["export", "bodypaint-view", "--profile", profile, "--json"]
    if sample_ref:
        export_args.extend(["--sample", sample_ref])
    for package_ref in package_refs:
        export_args.extend(["--package", package_ref])
    if aiue_pmx_profile:
        export_args.extend(["--aiue-pmx-profile", aiue_pmx_profile])

    export_result = remote_toyyard(
        conn,
        paths,
        host_ref=source_host_ref,
        toyyard_args=export_args,
    )
    export_json = dict(export_result.get("parsed_json") or {})
    export_root = str(export_json.get("export_root") or "").strip()
    if export_result["status"] != "pass" or not export_root:
        payload = {
            "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "bodypaint_view_handoff",
            "lane": "bodypaint",
            "status": "fail",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "export_result": export_result,
            "failure_stage": "export",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)

    source_tree_command = _remote_python_command(
        source_profile,
        python_code=_directory_tree_manifest_python_code(export_root),
        project_root=str(source_host["project_root"]),
    )
    source_tree_transport = _run_subprocess(_ssh_argv(source_profile, source_tree_command), timeout=300)
    source_tree_manifest = _parse_last_json_stdout(source_tree_transport) if source_tree_transport["returncode"] == 0 else {}
    if source_tree_transport["returncode"] != 0 or not source_tree_manifest.get("exists"):
        payload = {
            "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "bodypaint_view_handoff",
            "lane": "bodypaint",
            "status": "fail",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "export_result": export_result,
            "source_export_root": export_root,
            "source_tree_manifest": {
                "transport": source_tree_transport,
                "payload": source_tree_manifest,
            },
            "failure_stage": "source_manifest",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)
    source_packet_fingerprint_command = _remote_python_command(
        source_profile,
        python_code=_bodypaint_packet_fingerprint_python_code(export_root),
        project_root=str(source_host["project_root"]),
    )
    source_packet_fingerprint_transport = _run_subprocess(_ssh_argv(source_profile, source_packet_fingerprint_command), timeout=300)
    source_packet_fingerprint = (
        _parse_last_json_stdout(source_packet_fingerprint_transport)
        if source_packet_fingerprint_transport["returncode"] == 0
        else {}
    )
    if source_packet_fingerprint_transport["returncode"] != 0 or not source_packet_fingerprint.get("exists"):
        payload = {
            "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "bodypaint_view_handoff",
            "lane": "bodypaint",
            "status": "fail",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "export_result": export_result,
            "source_export_root": export_root,
            "source_tree_manifest": {
                "transport": source_tree_transport,
                "payload": source_tree_manifest,
            },
            "source_packet_fingerprint": {
                "transport": source_packet_fingerprint_transport,
                "payload": source_packet_fingerprint,
            },
            "failure_stage": "source_fingerprint",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)

    target_transfer = _transfer_profile(target_profile, target_transfer_profile)
    target_dir = str(target_transfer.get("remote_directory") or "").strip()
    if not target_dir:
        raise ValueError(f"Target transfer profile `{target_transfer_profile}` is missing remote_directory.")
    profile_leaf = Path(export_root).name
    target_export_root = _join_remote_path(target_profile, target_dir, profile_leaf)
    staging_operation_root = _join_remote_path(target_profile, _join_remote_path(target_profile, target_dir, ".incoming"), operation_id)
    target_staged_export_root = _join_remote_path(target_profile, staging_operation_root, profile_leaf)
    transfer_identity_material = json.dumps(
        {
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "lane": "bodypaint",
            "profile": profile,
            "stable_fingerprint": source_packet_fingerprint.get("stable_fingerprint") or "",
            "target_transfer_profile": target_transfer_profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    transfer_id = "xfer_bodypaint_" + hashlib.sha256(transfer_identity_material.encode("utf-8")).hexdigest()[:12]

    prepare_transport: dict[str, Any] | None = None
    if bool(target_transfer.get("create_if_missing")):
        prepare_transport = _ensure_remote_directory(target_profile, target_dir)
        if prepare_transport["returncode"] != 0:
            payload = {
                "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
                "operation_id": operation_id,
                "operation_kind": "bodypaint_view_handoff",
                "lane": "bodypaint",
                "status": "fail",
                "started_at": started_at,
                "ended_at": now_iso(),
                "source_host": source_host["profile_name"],
                "target_host": target_host["profile_name"],
                "profile": profile,
                "export_result": export_result,
                "target_transfer_profile": target_transfer_profile,
                "prepare_transport": prepare_transport,
                "failure_stage": "target_prepare",
            }
            return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)

    existing_verify_command = _remote_python_command(
        target_profile,
        python_code=_verify_bodypaint_export_python_code(target_export_root),
        project_root=str(target_host["project_root"]),
    )
    existing_verify_transport = _run_subprocess(_ssh_argv(target_profile, existing_verify_command), timeout=120)
    existing_verify_payload: dict[str, Any] = _parse_last_json_stdout(existing_verify_transport) if existing_verify_transport["returncode"] == 0 else {}
    existing_verify_result = {
        "transport": existing_verify_transport,
        "payload": existing_verify_payload,
        "acceptance": dict(existing_verify_payload.get("acceptance") or {}),
    }
    existing_tree_command = _remote_python_command(
        target_profile,
        python_code=_directory_tree_manifest_python_code(target_export_root),
        project_root=str(target_host["project_root"]),
    )
    existing_tree_transport = _run_subprocess(_ssh_argv(target_profile, existing_tree_command), timeout=300)
    existing_tree_payload = _parse_last_json_stdout(existing_tree_transport) if existing_tree_transport["returncode"] == 0 else {}
    existing_tree_manifest = {
        "transport": existing_tree_transport,
        "payload": existing_tree_payload,
    }
    existing_fingerprint_command = _remote_python_command(
        target_profile,
        python_code=_bodypaint_packet_fingerprint_python_code(target_export_root),
        project_root=str(target_host["project_root"]),
    )
    existing_fingerprint_transport = _run_subprocess(_ssh_argv(target_profile, existing_fingerprint_command), timeout=300)
    existing_fingerprint_payload = _parse_last_json_stdout(existing_fingerprint_transport) if existing_fingerprint_transport["returncode"] == 0 else {}
    existing_target_packet_fingerprint = {
        "transport": existing_fingerprint_transport,
        "payload": existing_fingerprint_payload,
    }
    existing_target_hash = str(existing_tree_payload.get("tree_hash") or "")
    source_stable_fingerprint = str(source_packet_fingerprint.get("stable_fingerprint") or "")
    existing_stable_fingerprint = str(existing_fingerprint_payload.get("stable_fingerprint") or "")
    existing_acceptance_ok = all(bool(value) for value in existing_verify_result.get("acceptance", {}).values())
    skip_existing_verified = bool(
        existing_verify_transport["returncode"] == 0
        and existing_tree_transport["returncode"] == 0
        and existing_fingerprint_transport["returncode"] == 0
        and existing_acceptance_ok
        and source_stable_fingerprint
        and existing_stable_fingerprint == source_stable_fingerprint
    )

    if skip_existing_verified:
        payload = {
            "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "bodypaint_view_handoff",
            "lane": "bodypaint",
            "status": "pass",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "sample_ref": sample_ref or "",
            "package_refs": package_refs,
            "aiue_pmx_profile": aiue_pmx_profile or "",
            "target_transfer_profile": target_transfer_profile,
            "transfer_identity": {
                "transfer_id": transfer_id,
                "identity_material": json.loads(transfer_identity_material),
            },
            "export_result": export_result,
            "source_export_root": export_root,
            "source_tree_manifest": {
                "transport": source_tree_transport,
                "payload": source_tree_manifest,
            },
            "source_packet_fingerprint": {
                "transport": source_packet_fingerprint_transport,
                "payload": source_packet_fingerprint,
            },
            "prepare_transport": prepare_transport,
            "existing_target_verify_result": existing_verify_result,
            "existing_target_tree_manifest": existing_tree_manifest,
            "existing_target_packet_fingerprint": existing_target_packet_fingerprint,
            "transfer": {
                "command_transport": None,
                "selected_transport": "noop_verified_existing",
                "resume_supported": False,
                "selection_reason": "verified_target_packet_fingerprint_already_matches_source",
                "source_capabilities": None,
                "target_capabilities": None,
                "target_export_root": target_export_root,
                "skipped_existing_verified": True,
                "skip_reason": "verified_target_packet_fingerprint_already_matches_source",
                "source_tree_hash": str(source_tree_manifest.get("tree_hash") or ""),
                "target_tree_hash": existing_target_hash,
                "tree_hash_match": existing_target_hash == str(source_tree_manifest.get("tree_hash") or ""),
                "source_stable_fingerprint": source_stable_fingerprint,
                "target_stable_fingerprint": existing_stable_fingerprint,
                "stable_fingerprint_match": True,
            },
            "verify_result": existing_verify_result,
            "target_tree_manifest": existing_tree_manifest,
            "failure_stage": "",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="pass", payload=payload)

    staging_prepare_transport = _ensure_remote_directory(target_profile, staging_operation_root)
    if staging_prepare_transport["returncode"] != 0:
        payload = {
            "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
            "operation_id": operation_id,
            "operation_kind": "bodypaint_view_handoff",
            "lane": "bodypaint",
            "status": "fail",
            "started_at": started_at,
            "ended_at": now_iso(),
            "source_host": source_host["profile_name"],
            "target_host": target_host["profile_name"],
            "profile": profile,
            "export_result": export_result,
            "target_transfer_profile": target_transfer_profile,
            "source_tree_manifest": {
                "transport": source_tree_transport,
                "payload": source_tree_manifest,
            },
            "prepare_transport": prepare_transport,
            "staging_prepare_transport": staging_prepare_transport,
            "failure_stage": "target_staging_prepare",
        }
        return _finish_operator_run(conn, paths, operation_id=operation_id, status="fail", payload=payload)

    transfer_plan = _select_peer_copy_transport(
        source_profile=source_profile,
        source_project_root=str(source_host["project_root"]),
        target_profile=target_profile,
        target_project_root=str(target_host["project_root"]),
        source_path=export_root,
        target_path=str(staging_operation_root.rstrip("/\\")) + ("/" if "/" in staging_operation_root or "\\" not in staging_operation_root else "\\"),
        recursive=True,
    )
    transfer_transport = _run_subprocess(_ssh_argv(source_profile, str(transfer_plan["command"])), timeout=600)

    staged_verify_result: dict[str, Any] | None = None
    staged_tree_manifest_result: dict[str, Any] | None = None
    promote_result: dict[str, Any] | None = None
    verify_result: dict[str, Any] | None = None
    target_tree_manifest_result: dict[str, Any] | None = None
    if transfer_transport["returncode"] == 0:
        staged_verify_command = _remote_python_command(
            target_profile,
            python_code=_verify_bodypaint_export_python_code(target_staged_export_root),
            project_root=str(target_host["project_root"]),
        )
        staged_verify_transport = _run_subprocess(_ssh_argv(target_profile, staged_verify_command), timeout=120)
        staged_verify_payload: dict[str, Any] = _parse_last_json_stdout(staged_verify_transport) if staged_verify_transport["returncode"] == 0 else {}
        staged_verify_result = {
            "transport": staged_verify_transport,
            "payload": staged_verify_payload,
            "acceptance": dict(staged_verify_payload.get("acceptance") or {}),
        }
        staged_tree_command = _remote_python_command(
            target_profile,
            python_code=_directory_tree_manifest_python_code(target_staged_export_root),
            project_root=str(target_host["project_root"]),
        )
        staged_tree_transport = _run_subprocess(_ssh_argv(target_profile, staged_tree_command), timeout=300)
        staged_tree_payload = _parse_last_json_stdout(staged_tree_transport) if staged_tree_transport["returncode"] == 0 else {}
        staged_tree_manifest_result = {
            "transport": staged_tree_transport,
            "payload": staged_tree_payload,
        }
    staged_packet_fingerprint_result: dict[str, Any] | None = None
    if transfer_transport["returncode"] == 0:
        staged_fingerprint_command = _remote_python_command(
            target_profile,
            python_code=_bodypaint_packet_fingerprint_python_code(target_staged_export_root),
            project_root=str(target_host["project_root"]),
        )
        staged_fingerprint_transport = _run_subprocess(_ssh_argv(target_profile, staged_fingerprint_command), timeout=300)
        staged_fingerprint_payload = _parse_last_json_stdout(staged_fingerprint_transport) if staged_fingerprint_transport["returncode"] == 0 else {}
        staged_packet_fingerprint_result = {
            "transport": staged_fingerprint_transport,
            "payload": staged_fingerprint_payload,
        }

    source_tree_hash = str(source_tree_manifest.get("tree_hash") or "")
    staged_tree_hash = ""
    if staged_tree_manifest_result:
        staged_tree_hash = str(staged_tree_manifest_result.get("payload", {}).get("tree_hash") or "")
    staged_tree_hash_match = bool(source_tree_hash and staged_tree_hash and source_tree_hash == staged_tree_hash)
    staged_stable_fingerprint = ""
    if staged_packet_fingerprint_result:
        staged_stable_fingerprint = str(staged_packet_fingerprint_result.get("payload", {}).get("stable_fingerprint") or "")
    staged_stable_fingerprint_match = bool(source_stable_fingerprint and staged_stable_fingerprint and source_stable_fingerprint == staged_stable_fingerprint)
    staged_acceptance_ok = bool(staged_verify_result and all(bool(value) for value in staged_verify_result.get("acceptance", {}).values()))

    if transfer_transport["returncode"] == 0 and staged_acceptance_ok and staged_tree_hash_match:
        promote_command = _remote_python_command(
            target_profile,
            python_code=_promote_directory_python_code(target_staged_export_root, target_export_root, operation_id),
            project_root=str(target_host["project_root"]),
        )
        promote_transport = _run_subprocess(_ssh_argv(target_profile, promote_command), timeout=300)
        promote_payload = _parse_last_json_stdout(promote_transport) if promote_transport["returncode"] == 0 else {}
        promote_result = {
            "transport": promote_transport,
            "payload": promote_payload,
        }

    promoted = bool(promote_result and promote_result.get("payload", {}).get("promoted"))
    if promoted:
        verify_command = _remote_python_command(
            target_profile,
            python_code=_verify_bodypaint_export_python_code(target_export_root),
            project_root=str(target_host["project_root"]),
        )
        verify_transport = _run_subprocess(_ssh_argv(target_profile, verify_command), timeout=120)
        verify_payload: dict[str, Any] = _parse_last_json_stdout(verify_transport) if verify_transport["returncode"] == 0 else {}
        verify_result = {
            "transport": verify_transport,
            "payload": verify_payload,
            "acceptance": dict(verify_payload.get("acceptance") or {}),
        }
        tree_command = _remote_python_command(
            target_profile,
            python_code=_directory_tree_manifest_python_code(target_export_root),
            project_root=str(target_host["project_root"]),
        )
        tree_transport = _run_subprocess(_ssh_argv(target_profile, tree_command), timeout=300)
        tree_payload = _parse_last_json_stdout(tree_transport) if tree_transport["returncode"] == 0 else {}
        target_tree_manifest_result = {
            "transport": tree_transport,
            "payload": tree_payload,
        }
        final_fingerprint_command = _remote_python_command(
            target_profile,
            python_code=_bodypaint_packet_fingerprint_python_code(target_export_root),
            project_root=str(target_host["project_root"]),
        )
        final_fingerprint_transport = _run_subprocess(_ssh_argv(target_profile, final_fingerprint_command), timeout=300)
        final_fingerprint_payload = _parse_last_json_stdout(final_fingerprint_transport) if final_fingerprint_transport["returncode"] == 0 else {}
        target_packet_fingerprint_result = {
            "transport": final_fingerprint_transport,
            "payload": final_fingerprint_payload,
        }
    else:
        target_packet_fingerprint_result = None

    target_tree_hash = ""
    if target_tree_manifest_result:
        target_tree_hash = str(target_tree_manifest_result.get("payload", {}).get("tree_hash") or "")
    tree_hash_match = bool(source_tree_hash and target_tree_hash and source_tree_hash == target_tree_hash)
    target_stable_fingerprint = ""
    if target_packet_fingerprint_result:
        target_stable_fingerprint = str(target_packet_fingerprint_result.get("payload", {}).get("stable_fingerprint") or "")
    stable_fingerprint_match = bool(source_stable_fingerprint and target_stable_fingerprint and source_stable_fingerprint == target_stable_fingerprint)

    status = "pass"
    failure_stage = ""
    if transfer_transport["returncode"] != 0:
        status = "fail"
        failure_stage = "transfer"
    elif not staged_verify_result or not all(bool(value) for value in staged_verify_result.get("acceptance", {}).values()):
        status = "fail"
        failure_stage = "staging_verify"
    elif not staged_tree_hash_match:
        status = "fail"
        failure_stage = "staging_hash_verify"
    elif not promote_result or promote_result.get("transport", {}).get("returncode") != 0 or not promoted:
        status = "fail"
        failure_stage = "target_promote"
    elif not verify_result or not all(bool(value) for value in verify_result.get("acceptance", {}).values()):
        status = "fail"
        failure_stage = "target_verify"
    elif not tree_hash_match:
        status = "fail"
        failure_stage = "target_hash_verify"

    payload = {
        "schema_version": REMOTE_EXPORT_HANDOFF_RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_kind": "bodypaint_view_handoff",
        "lane": "bodypaint",
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "source_host": source_host["profile_name"],
        "target_host": target_host["profile_name"],
        "profile": profile,
        "sample_ref": sample_ref or "",
        "package_refs": package_refs,
        "aiue_pmx_profile": aiue_pmx_profile or "",
        "target_transfer_profile": target_transfer_profile,
        "transfer_identity": {
            "transfer_id": transfer_id,
            "identity_material": json.loads(transfer_identity_material),
        },
        "export_result": export_result,
        "source_export_root": export_root,
        "source_tree_manifest": {
            "transport": source_tree_transport,
            "payload": source_tree_manifest,
        },
        "source_packet_fingerprint": {
            "transport": source_packet_fingerprint_transport,
            "payload": source_packet_fingerprint,
        },
        "prepare_transport": prepare_transport,
        "existing_target_verify_result": existing_verify_result,
        "existing_target_tree_manifest": existing_tree_manifest,
        "existing_target_packet_fingerprint": existing_target_packet_fingerprint,
        "staging_prepare_transport": staging_prepare_transport,
        "transfer": {
            "selected_transport": transfer_plan["selected_transport"],
            "resume_supported": transfer_plan["resume_supported"],
            "selection_reason": transfer_plan["selection_reason"],
            "source_capabilities": transfer_plan["source_capabilities"],
            "target_capabilities": transfer_plan["target_capabilities"],
            "command_transport": transfer_transport,
            "target_export_root": target_export_root,
            "target_staging_root": target_staged_export_root,
            "staging_operation_root": staging_operation_root,
            "skipped_existing_verified": False,
            "staged_tree_hash_match": staged_tree_hash_match,
            "staged_tree_hash": staged_tree_hash,
            "staged_stable_fingerprint": staged_stable_fingerprint,
            "staged_stable_fingerprint_match": staged_stable_fingerprint_match,
            "tree_hash_match": tree_hash_match,
            "source_tree_hash": source_tree_hash,
            "target_tree_hash": target_tree_hash,
            "source_stable_fingerprint": source_stable_fingerprint,
            "target_stable_fingerprint": target_stable_fingerprint,
            "stable_fingerprint_match": stable_fingerprint_match,
        },
        "staged_verify_result": staged_verify_result,
        "staged_tree_manifest": staged_tree_manifest_result,
        "staged_packet_fingerprint": staged_packet_fingerprint_result,
        "promote_result": promote_result,
        "verify_result": verify_result,
        "target_tree_manifest": target_tree_manifest_result,
        "target_packet_fingerprint": target_packet_fingerprint_result,
        "failure_stage": failure_stage,
    }
    return _finish_operator_run(conn, paths, operation_id=operation_id, status=status, payload=payload)
