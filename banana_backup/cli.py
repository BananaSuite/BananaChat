"""Common backup commands; applications supply snapshot and restore hooks."""

from pathlib import Path
import tempfile

from . import crypto
from .files import directory
from .git import check_private


def add_commands(commands, *, agent=False):
    parser = commands.add_parser("backups", help="Optional encrypted backups in a private GitHub or Forgejo repository")
    actions = parser.add_subparsers(dest="backup_action", required=True)
    key = actions.add_parser("keygen", help="Create a recovery key to save offline")
    key.add_argument("--output", type=Path, required=True)
    setup = actions.add_parser("configure", help="Select a dedicated private backup repository and series; leave scheduling off")
    setup.add_argument("--repo", required=True)
    setup.add_argument("--forge", choices=("github", "forgejo"), required=True)
    setup.add_argument("--name", required=True, help="Unique series name for this installation, such as wiki-production")
    setup.add_argument("--token-file", type=Path, required=True, help="Private file with a read/write repository token")
    setup.add_argument("--key-file", type=Path, required=True, help="Native age recovery identity saved separately from the repository")
    setup.add_argument("--username", default="git")
    setup.add_argument("--keep", type=int, default=7, help="Retain 2–30 snapshots in this series; default 7")
    setup.add_argument("--max-mib", type=int, default=512, help="Maximum compressed package size, 1–1024 MiB; default 512")
    actions.add_parser("status")
    actions.add_parser("list", help="List snapshot IDs in this series")
    run = actions.add_parser("run", help="Create, encrypt, upload, download-verify, then prune a snapshot")
    run.add_argument("--automatic", action="store_true", help="Skip unless the backup schedule is enabled")
    enable = actions.add_parser("enable", help="Opt into scheduled backups")
    enable.add_argument("--interval", type=int, default=None, help="Minutes between backups (60–10080; default 1440)")
    actions.add_parser("disable", help="Stop scheduling new backups; allow an upload already in progress to finish")
    for action in ("download", "verify", "restore"):
        target = actions.add_parser(action)
        target.add_argument("snapshot", help="Exact snapshot ID from backups list")
        if action == "download" or (action == "restore" and agent):
            target.add_argument("--output", type=Path, required=True,
                                help="New package filename" if action == "download" else "Empty recovery directory")
        elif action == "restore":
            target.add_argument("--domain")
            target.add_argument("--port", type=int)
            target.add_argument("--name", help="Service name when restoring to an empty installation")
    return parser


def handle(args, store, *, create_package, restore_package, schedule=None):
    action = args.backup_action
    if action == "keygen":
        return crypto.keygen(args.output)
    if action == "configure":
        result = store.configure(repo=args.repo, forge=args.forge, name=args.name, token_file=args.token_file,
                                 key_file=args.key_file, username=args.username, keep=args.keep, max_mib=args.max_mib)
        if schedule:
            schedule(store.schedule())
        return result
    if action == "status":
        return store.status()
    if action in {"enable", "disable"}:
        previous = store.schedule()
        policy = store.set_schedule(action == "enable", getattr(args, "interval", None))
        if schedule:
            try:
                schedule(policy)
            except BaseException:
                if action == "enable":
                    store.set_schedule(previous["enabled"], previous["interval_minutes"])
                raise
        return {"schedule": policy}
    if action == "list":
        return store.list()
    if action == "verify":
        return store.verify(args.snapshot)
    if action == "download":
        return store.download(args.snapshot, args.output)
    if action == "run":
        if args.automatic and not store.schedule()["enabled"]:
            return {"outcome": "disabled"}
        config = store.settings()
        crypto.recipient(store.identity)
        check_private(config, store.token)
        # The app is stopped only while preparing a consistent local snapshot;
        # network transfers happen after its normal running state is restored.
        package = create_package()
        try:
            result = store.upload(package, automatic=args.automatic)
        except BaseException as error:
            raise RuntimeError(f"Remote backup failed; the private local package is retained at {package}. {error}") from error
        if result.get("outcome") == "complete":
            Path(package).unlink()
        return result
    if action == "restore":
        directory(store.root)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=store.root) as name:
            package = Path(name) / "package.tar.gz"
            store.download(args.snapshot, package)
            result = restore_package(package, args)
        # Both the updater and the backup schedule remain opt-in on a restore.
        policy = store.set_schedule(False)
        if schedule:
            schedule(policy)
        return result
    raise ValueError("Unknown backup command.")
