"""Small, fail-closed Linux filesystem sandbox for the opt-in native transport.

This module uses only the standard library so the root lifecycle command can
validate profile mounts without loading the web application or its credentials.
"""

import json
import os
from pathlib import Path
import stat


class SandboxError(ValueError):
    """An operator-facing error without profile contents or credentials."""


PUBLIC_RUNTIME_DIRS = ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/local/bin", "/usr/local/lib", "/usr/share")
PUBLIC_SYSTEM_PATHS = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs", "/etc/localtime")


def private_roots(paths):
    """Private app state/source must not be inside a public runtime mount."""
    public = [Path(value).resolve() for value in (*PUBLIC_RUNTIME_DIRS, *PUBLIC_SYSTEM_PATHS, "/bin", "/lib", "/lib64")]
    for value in paths:
        path = Path(value).resolve()
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in public):
            raise SandboxError("Keep application source, storage and connector manifests outside public runtime trees.")


def private_path(value, *, uid, directory=False):
    if not isinstance(value, (str, os.PathLike)):
        raise SandboxError("Use an absolute private profile path.")
    path = Path(value)
    if (not path.is_absolute() or path != path.resolve() or
            any(char.isspace() or char in "%\\\"" or ord(char) < 32 or ord(char) == 127 for char in str(path))):
        raise SandboxError("Use absolute private paths without symlinks, whitespace or systemd specifiers.")
    info = path.stat()
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not wanted or info.st_uid != uid or info.st_mode & 0o077:
        raise SandboxError("Connector files and profile directories must be private and owned by the service user.")
    required = 0o700 if directory else 0o400
    if info.st_mode & required != required:
        raise SandboxError("The service user needs readable connector files and readable, writable, searchable profiles.")
    for parent in path.parents:
        info = parent.stat()
        searchable = 0o100 if info.st_uid == uid else 0o001
        if info.st_mode & searchable != searchable:
            raise SandboxError("The service user must be able to traverse every private path parent.")
    return path


def profile_paths(profiles, *, uid, forbidden=()):
    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 100:
        raise SandboxError("Configure between one and 100 private account profiles.")
    paths = []
    forbidden = [Path(path).resolve() for path in forbidden]
    forbidden += [Path(path) for path in ("/usr", "/etc", "/bin", "/lib", "/lib64", "/proc", "/dev", "/run", "/tmp", "/home", "/root")]
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise SandboxError("Invalid private account profile.")
        current = [private_path(profile.get(key, ""), uid=uid, directory=True)
                   for key in ("home", "config_dir")]
        for path in current:
            if len(path.parts) < 3 or any(path.is_relative_to(other) or other.is_relative_to(path) for other in forbidden):
                raise SandboxError("Keep native profiles outside application storage, source and system directories.")
            if any(previous != name and (path.is_relative_to(other) or other.is_relative_to(path))
                   for previous, other in paths):
                raise SandboxError("Each subscription needs non-overlapping private profile directories.")
            for parent in path.parents:
                if any(parent == own or parent.is_relative_to(own) for own in current):
                    continue
                info = parent.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise SandboxError("Keep private profile roots below root-owned, non-writable parent directories.")
            paths.append((name, path))
    # A configuration directory inside its own home needs no additional mount.
    unique = sorted({path for _, path in paths}, key=lambda path: (len(path.parts), str(path)))
    return [path for path in unique if not any(path != other and path.is_relative_to(other) for other in unique)]


def managed_paths(manifest_path, *, uid, root):
    path = private_path(manifest_path, uid=uid)
    private_roots((root, path))
    with path.open("rb") as source:
        data = source.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise SandboxError("The connector configuration is too large.")
    try:
        manifest = json.loads(data)
    except (ValueError, UnicodeError):
        raise SandboxError("The connector configuration is not valid JSON.") from None
    if not isinstance(manifest, dict):
        raise SandboxError("The connector configuration must be an object.")
    trusted_executable(manifest.get("binary", ""))
    trusted_executable("/usr/bin/bwrap")
    return profile_paths(manifest.get("profiles"), uid=uid, forbidden=(root, path))


def trusted_executable(value):
    if not isinstance(value, (str, os.PathLike)):
        raise SandboxError("Configure an absolute, root-owned native executable.")
    path = Path(value)
    if not path.is_absolute():
        raise SandboxError("Configure an absolute, root-owned native executable.")
    path = path.resolve()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_mode & 0o005 != 0o005:
        raise SandboxError("Production native executables must be root-owned, publicly readable and executable, and not writable by the service user.")
    for parent in path.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o001:
            raise SandboxError("Keep production native executables in root-owned, non-writable, publicly searchable directories.")
    return str(path)


def command(binary, profile, arguments, *, cwd, environment):
    """Expose public system runtime and this profile; retain outbound networking.

    A fresh PID namespace hides the service's processes and /proc descriptors.
    No deployment directory, connector manifest, other profile or host /tmp is
    mounted. The active profile is the only persistent writable filesystem.
    """
    result = [trusted_executable("/usr/bin/bwrap"), "--unshare-user", "--unshare-pid", "--unshare-ipc",
              "--unshare-uts", "--unshare-cgroup", "--disable-userns", "--cap-drop", "ALL",
              "--die-with-parent", "--new-session"]
    for value in PUBLIC_RUNTIME_DIRS:
        if Path(value).exists():
            result += ["--ro-bind", value, value]
    for value in ("/bin", "/lib", "/lib64"):
        path = Path(value)
        if path.is_symlink():
            result += ["--symlink", os.readlink(path), value]
        elif path.exists():
            result += ["--ro-bind", value, value]
    for value in PUBLIC_SYSTEM_PATHS:
        if Path(value).exists():
            result += ["--ro-bind", value, value]
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = environment.get(key)
        if value:
            path = Path(value)
            info = path.stat()
            wanted = stat.S_ISDIR(info.st_mode) and str(path) == "/etc/ssl/certs" \
                if key == "SSL_CERT_DIR" else stat.S_ISREG(info.st_mode)
            if not path.is_absolute() or info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o004 or not wanted:
                raise SandboxError("Extra certificate trust must use root-owned, non-writable public paths.")
            for parent in path.resolve().parents:
                info = parent.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise SandboxError("Keep extra certificate trust below root-owned, non-writable parent directories.")
            result += ["--ro-bind", str(path), str(path)]
    result += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # Recheck each invocation. A nested config remains part of the enclosing
    # home mount even if changed after this check; never resolve and bind it to
    # a different host directory at launch time.
    home = private_path(profile["home"], uid=os.getuid(), directory=True)
    config = private_path(profile["config_dir"], uid=os.getuid(), directory=True)
    result += ["--bind", str(home), str(home)]
    if not config.is_relative_to(home):
        result += ["--bind", str(config), str(config)]
    result += ["--ro-bind", binary, binary, "--chdir", str(cwd), "--", binary, *arguments]
    return result
