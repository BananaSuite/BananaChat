"""OS service installer for the BananaChat Worker daemon.

Usage
-----
  # Install for the current account (use --system only for Linux system services)
  python bananachat_worker.py install

  # Uninstall
  python bananachat_worker.py uninstall

  # Start / Stop / Status (after installation)
  python bananachat_worker.py start
  python bananachat_worker.py stop
  python bananachat_worker.py status

Platform support
----------------
  Linux: installs a systemd user service (~/.config/systemd/user/) that
            starts on login.  Pass --system to install as a system service
            (/etc/systemd/system/) which survives logouts and starts at boot.

  Windows: installs a Task Scheduler job for the current user at login.
            It uses the interactive user token with least privilege.

  macOS:   installs a LaunchAgent in ~/Library/LaunchAgents that starts at
            login.  Pass --system to install a LaunchDaemon in
            /Library/LaunchDaemons that starts at boot and still runs as the
            installing account, never as root.
"""

import os
import platform
import plistlib
import subprocess
import sys
import tempfile

_OS = platform.system()


# Linux systemd

_SYSTEMD_UNIT = """\
[Unit]
Description=BananaChat Worker Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} {script} run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
EnvironmentFile=-{env_file}
NoNewPrivileges=true
{identity}

[Install]
WantedBy={target}
"""

_ENV_TEMPLATE = """\
BC_SERVER_URL=https://your-bananachat-server.example.com
BC_WORKER_TOKEN=paste-your-token-here
BC_WORKER_NAME={hostname}
# BC_OLLAMA_BINARY=ollama
# BC_OLLAMA_IDLE_TIMEOUT=600
# BC_IDLE_THRESHOLD=300
# BC_GPU_GAMING_THRESHOLD=70
# BC_WORKER_LOG_LEVEL=INFO
"""


def _systemd_unit_path(system: bool) -> str:
    if system:
        return "/etc/systemd/system/bananachat-worker.service"
    user_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, "bananachat-worker.service")


def _env_file_path() -> str:
    """Mirror of config.default_env_file(); see the note there."""
    if _OS == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BananaChat", "bananachat-worker.env")
    return os.path.expanduser("~/.config/bananachat-worker.env")


def _write_env_template(env_file: str, extra: str = "") -> bool:
    """Create the settings file if it is not already there. Returns whether
    it was created, so each installer can print the same guidance once."""
    import socket

    os.makedirs(os.path.dirname(env_file), mode=0o700, exist_ok=True)
    if os.path.exists(env_file):
        return False
    with open(env_file, "w") as handle:
        handle.write(_ENV_TEMPLATE.format(hostname=socket.gethostname()) + extra)
    os.chmod(env_file, 0o600)
    print(f"Created settings file: {env_file}")
    print("Edit it to set BC_SERVER_URL and BC_WORKER_TOKEN before starting.")
    return True


def _system_prefix(system):
    return ["sudo"] if system and os.geteuid() != 0 else []


def install_linux(system: bool = False):
    python = sys.executable
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "bananachat_worker.py"))
    env_file = _env_file_path()
    unit_path = _systemd_unit_path(system)

    identity = ""
    if system:
        if os.geteuid() == 0:
            raise ValueError("Run installation from the non-root worker account with --system; the installer uses sudo where needed")
        import pwd
        account = pwd.getpwnam(os.environ["SUDO_USER"]) if os.environ.get("SUDO_USER") else pwd.getpwuid(os.getuid())
        if account.pw_uid == 0:
            raise ValueError("Install the worker from its non-root account with --system; sudo is used only to install the service")
        env_file = os.path.join(account.pw_dir, ".config", "bananachat-worker.env")
        identity = f"User={account.pw_name}\nGroup={account.pw_gid}"
    for path in (python, script, env_file):
        if any(char in path for char in '\n\r%"'):
            raise ValueError("Service paths cannot contain control characters, quotes or systemd specifiers")
    unit_content = _SYSTEMD_UNIT.format(
        python='"' + python + '"', script='"' + script + '"', env_file='"' + env_file + '"',
        identity=identity, target="multi-user.target" if system else "default.target",
    )
    if system:
        with tempfile.NamedTemporaryFile(mode="w", prefix="bananachat-worker-", suffix=".service") as temporary:
            temporary.write(unit_content)
            temporary.flush()
            subprocess.run([*_system_prefix(system), "install", "-m", "0644", temporary.name, unit_path], check=True)
    else:
        with open(unit_path, "w") as f:
            f.write(unit_content)

    _write_env_template(env_file)

    scope = "--system" if system else "--user"
    subprocess.run([*_system_prefix(system), "systemctl", scope, "daemon-reload"], check=True)
    subprocess.run([*_system_prefix(system), "systemctl", scope, "enable", "bananachat-worker"], check=True)

    print(f"\nService installed at: {unit_path}")
    print(f"\nTo start now:     systemctl {scope} start bananachat-worker")
    print(f"To check status:  systemctl {scope} status bananachat-worker")
    print(f"To view logs:     journalctl {scope} -u bananachat-worker -f")


def uninstall_linux(system: bool = False):
    scope = "--system" if system else "--user"
    subprocess.run([*_system_prefix(system), "systemctl", scope, "stop",    "bananachat-worker"], check=False)
    subprocess.run([*_system_prefix(system), "systemctl", scope, "disable", "bananachat-worker"], check=False)
    unit_path = _systemd_unit_path(system)
    if system:
        subprocess.run([*_system_prefix(system), "rm", "-f", unit_path], check=False)
    else:
        try:
            os.remove(unit_path)
        except FileNotFoundError:
            pass
    subprocess.run([*_system_prefix(system), "systemctl", scope, "daemon-reload"], check=False)
    print("Service uninstalled.")


def status_linux(system: bool = False):
    scope = "--system" if system else "--user"
    subprocess.run([*_system_prefix(system), "systemctl", scope, "status", "bananachat-worker"])


def start_linux(system: bool = False):
    scope = "--system" if system else "--user"
    subprocess.run([*_system_prefix(system), "systemctl", scope, "start", "bananachat-worker"], check=True)


def stop_linux(system: bool = False):
    scope = "--system" if system else "--user"
    subprocess.run([*_system_prefix(system), "systemctl", scope, "stop", "bananachat-worker"], check=True)


# macOS launchd

_LAUNCHD_LABEL = "com.bananasuite.bananachat.worker"


def _launchd_plist_path(system: bool) -> str:
    if system:
        return f"/Library/LaunchDaemons/{_LAUNCHD_LABEL}.plist"
    agents = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(agents, exist_ok=True)
    return os.path.join(agents, f"{_LAUNCHD_LABEL}.plist")


def _launchd_domain(system: bool) -> str:
    return "system" if system else f"gui/{os.getuid()}"


def _launchctl(system: bool, *arguments, check: bool = False):
    return subprocess.run([*_system_prefix(system), "launchctl", *arguments],
                          capture_output=True, text=True, timeout=30, check=check)


def install_macos(system: bool = False):
    python = sys.executable
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "bananachat_worker.py"))
    env_file = _env_file_path()
    account = None
    if system:
        if os.geteuid() == 0:
            raise ValueError("Run installation from the non-root worker account with --system; the installer uses sudo where needed")
        import pwd
        account = pwd.getpwnam(os.environ["SUDO_USER"]) if os.environ.get("SUDO_USER") else pwd.getpwuid(os.getuid())
        if account.pw_uid == 0:
            raise ValueError("Install the worker from its non-root account with --system; sudo is used only to install the daemon")
        env_file = os.path.join(account.pw_dir, ".config", "bananachat-worker.env")

    log_dir = os.path.join(account.pw_dir if account else os.path.expanduser("~"), "Library", "Logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "bananachat-worker.log")

    plist = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": [python, script, "run"],
        "EnvironmentVariables": {"BC_WORKER_ENV_FILE": env_file},
        "WorkingDirectory": os.path.dirname(script),
        "RunAtLoad": True,
        # Mirror systemd's Restart=on-failure: come back after a crash, stay
        # down after a clean stop.
        "KeepAlive": {"SuccessfulExit": False},
        # Tell macOS this is background work so the scheduler throttles it
        # behind whatever the person is actually doing.
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if account:
        plist["UserName"] = account.pw_name

    plist_path = _launchd_plist_path(system)
    with tempfile.NamedTemporaryFile(prefix="bananachat-worker-", suffix=".plist", delete=False) as temporary:
        temporary_path = temporary.name
        plistlib.dump(plist, temporary)
    try:
        if system:
            subprocess.run([*_system_prefix(system), "install", "-m", "0644", temporary_path, plist_path], check=True)
        else:
            with open(temporary_path, "rb") as source, open(plist_path, "wb") as target:
                target.write(source.read())
            os.chmod(plist_path, 0o644)
    finally:
        os.unlink(temporary_path)

    _write_env_template(env_file)

    domain = _launchd_domain(system)
    # bootstrap is the modern verb; load -w is kept for the older Intel Macs
    # that never got it.
    booted = _launchctl(system, "bootstrap", domain, plist_path)
    if booted.returncode != 0:
        _launchctl(system, "load", "-w", plist_path)

    print(f"\nService installed at: {plist_path}")
    print(f"\nTo start now:     launchctl kickstart -k {domain}/{_LAUNCHD_LABEL}")
    print(f"To check status:  launchctl print {domain}/{_LAUNCHD_LABEL}")
    print(f"To view logs:     tail -f {log_path}")


def uninstall_macos(system: bool = False):
    domain = _launchd_domain(system)
    plist_path = _launchd_plist_path(system)
    if _launchctl(system, "bootout", f"{domain}/{_LAUNCHD_LABEL}").returncode != 0:
        _launchctl(system, "unload", "-w", plist_path)
    if system:
        subprocess.run([*_system_prefix(system), "rm", "-f", plist_path], check=False)
    else:
        try:
            os.remove(plist_path)
        except FileNotFoundError:
            pass
    print("Service uninstalled.")


def status_macos(system: bool = False):
    domain = _launchd_domain(system)
    result = _launchctl(system, "print", f"{domain}/{_LAUNCHD_LABEL}")
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"{_LAUNCHD_LABEL} is not loaded in domain {domain}.")


def start_macos(system: bool = False):
    domain = _launchd_domain(system)
    result = _launchctl(system, "kickstart", "-k", f"{domain}/{_LAUNCHD_LABEL}")
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not start {_LAUNCHD_LABEL}: {result.stderr.strip() or result.stdout.strip()}. "
            "Install it first with the install command."
        )


def stop_macos(system: bool = False):
    domain = _launchd_domain(system)
    result = _launchctl(system, "bootout", f"{domain}/{_LAUNCHD_LABEL}")
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not stop {_LAUNCHD_LABEL}: {result.stderr.strip() or result.stdout.strip()}."
        )


# Windows

def install_windows():
    """Run as the current interactive user, with no elevated service account."""
    from xml.sax.saxutils import escape

    legacy = subprocess.run(['sc', 'query', 'BananaChatWorker'], capture_output=True, timeout=15)
    if legacy.returncode == 0:
        raise RuntimeError(
            'An older BananaChatWorker system service exists. Stop and remove it '
            'with sc.exe stop BananaChatWorker and sc.exe delete BananaChatWorker '
            'from an administrator terminal, then install from your normal account.'
        )
    user = subprocess.run(['whoami'], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    if not user:
        raise RuntimeError('Could not identify the Windows account for the worker task.')
    python = sys.executable
    # pythonw.exe runs the task without flashing a console window at login.
    windowless = os.path.join(os.path.dirname(python), 'pythonw.exe')
    if os.path.exists(windowless):
        python = windowless
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), 'bananachat_worker.py'))
    arguments = subprocess.list2cmdline([script, 'run'])
    env_file = _env_file_path()
    task_xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>BananaChat Worker daemon</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Worker"><UserId>{escape(user)}</UserId>
    <LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
  </Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Worker"><Exec><Command>{escape(python)}</Command>
    <Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(os.path.dirname(script))}</WorkingDirectory>
  </Exec></Actions>
</Task>'''
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-16', suffix='.xml', delete=False) as output:
            temporary = output.name
            output.write(task_xml)
        subprocess.run(['schtasks', '/Create', '/TN', 'BananaChatWorker', '/XML', temporary, '/F'],
                       capture_output=True, text=True, check=True, timeout=30)
    finally:
        if temporary:
            os.unlink(temporary)
    # A Task Scheduler job has nowhere to send stdout, so point the worker at
    # its own log file next to the settings file.
    log_path = os.path.join(os.path.dirname(env_file), 'bananachat-worker.log')
    _write_env_template(env_file, extra=f'BC_WORKER_LOG_FILE={log_path}\n')

    print('BananaChatWorker task installed for your current account at login.')
    print(f'Settings file: {env_file}')
    print(f'Log file:      {log_path}')
    print('Edit the settings file, then run the start command.')


def uninstall_windows():
    subprocess.run(['schtasks', '/End', '/TN', 'BananaChatWorker'], capture_output=True, timeout=15)
    subprocess.run(['schtasks', '/Delete', '/TN', 'BananaChatWorker', '/F'],
                   capture_output=True, text=True, check=True, timeout=30)
    print('BananaChatWorker task uninstalled.')


def status_windows():
    result = subprocess.run(['schtasks', '/Query', '/TN', 'BananaChatWorker', '/FO', 'LIST'],
                            capture_output=True, text=True, timeout=15)
    print(result.stdout if result.returncode == 0 else 'BananaChatWorker task is not installed.')


# Public dispatch

def _unsupported(action: str):
    """Fail loudly. A service command that quietly does nothing is worse than
    one that refuses: the worker looks installed and never runs."""
    print(f"Cannot {action} the worker service on this platform: {_OS}. "
          "Supported platforms are Linux, macOS and Windows. "
          "Run the worker in the foreground with the run command instead.")
    sys.exit(1)


def install(system: bool = False):
    if _OS == "Windows":
        if system:
            raise ValueError("--system is a Linux and macOS option; on Windows the worker installs as a user task")
        install_windows()
    elif _OS == "Linux":
        install_linux(system=system)
    elif _OS == "Darwin":
        install_macos(system=system)
    else:
        _unsupported("install")


def uninstall(system: bool = False):
    if _OS == "Windows":
        uninstall_windows()
    elif _OS == "Linux":
        uninstall_linux(system=system)
    elif _OS == "Darwin":
        uninstall_macos(system=system)
    else:
        _unsupported("uninstall")


def status(system: bool = False):
    if _OS == "Windows":
        status_windows()
    elif _OS == "Linux":
        status_linux(system=system)
    elif _OS == "Darwin":
        status_macos(system=system)
    else:
        _unsupported("report the status of")


def start(system: bool = False):
    if _OS == "Windows":
        subprocess.run(["schtasks", "/Run", "/TN", "BananaChatWorker"], check=True, timeout=15)
    elif _OS == "Linux":
        start_linux(system=system)
    elif _OS == "Darwin":
        start_macos(system=system)
    else:
        _unsupported("start")


def stop(system: bool = False):
    if _OS == "Windows":
        subprocess.run(["schtasks", "/End", "/TN", "BananaChatWorker"], check=True, timeout=15)
    elif _OS == "Linux":
        stop_linux(system=system)
    elif _OS == "Darwin":
        stop_macos(system=system)
    else:
        _unsupported("stop")
