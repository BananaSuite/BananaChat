"""Application-specific deployment modes for the shared lifecycle manager."""

from pathlib import Path
import os
import re
import secrets
from urllib.parse import urlsplit

from .files import read_environment
from .product import PRODUCT

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
HOSTING_STORAGE_NAMES = ("uploads", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files")


def hosting_storage_link(path, data):
    """Recognize only the relative compatibility aliases created by hosting."""
    relative = path.relative_to(data)
    if len(relative.parts) != 3 or relative.parts[0] != "instances" or path.name not in HOSTING_STORAGE_NAMES:
        return False
    if not path.is_symlink() or os.readlink(path) != "storage/" + path.name:
        return False
    tenant = path.parent
    return all(folder.is_dir() and not folder.is_symlink()
               for folder in (data, data / "instances", tenant, tenant / "storage", tenant / "storage" / path.name))


def prepare_hosting_storage(data):
    """Recreate internal aliases after restoring regular-file-only packages."""
    instances = data / "instances"
    if instances.is_symlink():
        raise ValueError("The managed instances directory must not be a symlink.")
    for tenant in instances.iterdir():
        if tenant.is_symlink():
            raise ValueError("Managed tenant directories must not be symlinks.")
        if not tenant.is_dir():
            continue
        storage = tenant / "storage"
        if storage.is_symlink():
            raise ValueError("Managed tenant storage must not be a symlink.")
        storage.mkdir(mode=0o700, exist_ok=True)
        for name in HOSTING_STORAGE_NAMES:
            target, alias = storage / name, tenant / name
            if target.is_symlink():
                raise ValueError("Managed tenant storage targets must not be symlinks.")
            target.mkdir(mode=0o700, exist_ok=True)
            if not alias.exists() and not alias.is_symlink():
                alias.symlink_to("storage/" + name, target_is_directory=True)


def modes(product=PRODUCT):
    return ("wiki", "hosting") if product == "BananaWiki" else ("single", "web", "compute")


def managed_ollama(settings):
    """BananaChat single/compute servers run their own Ollama unless an existing one was chosen."""
    return (settings["product"] == "BananaChat" and settings["mode"] in {"single", "compute"}
            and not settings.get("ollama_url"))


def ollama_upstream(settings, environment=None):
    """The loopback Ollama a BananaChat single/compute server uses, as configured now.

    The gateway reads BC_COMPUTE_UPSTREAM and the chat service BC_OLLAMA_URL;
    operators may edit either. Anything that is not a loopback address falls
    back to the address chosen at installation.
    """
    if environment is None:
        try:
            environment = read_environment(Path(settings["root"]) / "config/app.env")
        except (OSError, ValueError):
            environment = {}
    value = (environment.get("BC_COMPUTE_UPSTREAM" if settings["mode"] == "compute" else "BC_OLLAMA_URL") or "").rstrip("/")
    return value if loopback_http(value) else (settings.get("ollama_url") or DEFAULT_OLLAMA_URL)


def loopback_http(url):
    """A plain-HTTP address on this machine without credentials, path or query."""
    try:
        parts = urlsplit(url or "")
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return False
    return (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS and not parts.username and not parts.password
            and parts.path in ("", "/") and not parts.query and not parts.fragment)


def validate_domain(domain):
    if not domain:
        return ""
    domain = domain.encode("idna").decode().lower().rstrip(".")
    if len(domain) > 253 or "." not in domain or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in domain.split(".")):
        raise ValueError("Use a DNS hostname without a scheme, port, or path.")
    return domain


def source_link(url):
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
        return "https://" + host + "/" + path.removesuffix(".git")
    if url.startswith("ssh://"):
        parsed = urlsplit(url)
        return "https://" + parsed.hostname + parsed.path.removesuffix(".git")
    return url.removesuffix(".git") if url.startswith("https://") else "https://github.com/BananaSuite/" + PRODUCT


def wiki_paths(data):
    folders = {"BW_UPLOAD_FOLDER": "uploads", "BW_FAVICON_UPLOAD_FOLDER": "favicons", "BW_ATTACHMENT_FOLDER": "attachments",
               "BW_CHAT_ATTACHMENT_FOLDER": "chat_attachments", "BW_KANBAN_ATTACHMENT_FOLDER": "kanban_attachments",
               "BW_CUSTOM_PAGE_FILES_FOLDER": "custom_page_files", "BW_TTS_FOLDER": "tts", "BW_EXTERNAL_PLUGINS_DIR": "plugins",
               "BW_SITE_EXPORT_TEMP_DIR": "tmp_exports"}
    return {"BW_INSTANCE_DIR": str(data), "BW_DATABASE_PATH": str(data / "bananawiki.db"),
            "BW_LOG_FILE": str(data / "logs" / "bananawiki.log"),
            **{key: str(data / folder) for key, folder in folders.items()}}


def environment(settings, previous=None):
    root, mode = Path(settings["root"]), settings["mode"]
    data, product = root / "data", settings["product"]
    values = dict(previous or {})
    # A setup token is generated only for a first installation. Updates and
    # restores keep whatever the operator left, including a deleted token.
    first_install = not settings.get("installed")
    values.update(BANANA_MAINTENANCE_FILE=str(data / ".banana-maintenance"), PYTHONDONTWRITEBYTECODE="1")
    if product == "BananaWiki" and mode == "wiki":
        defaults = {"BW_INSTANCE_DIR": str(data), "BW_DATABASE_PATH": str(data / "bananawiki.db"),
                    "BW_HOST": "127.0.0.1", "BW_PORT": str(settings["port"]), "BW_PROXY_MODE": "1" if settings.get("domain") else "0",
                    "BW_PREFERRED_URL_SCHEME": "https" if settings.get("domain") else "http", "BW_ENV": "production",
                    "BW_SOURCE_URL": source_link(settings["source_url"])}
        if first_install:
            defaults["BW_SETUP_TOKEN"] = secrets.token_hex(32)
        defaults.update(wiki_paths(data))
        defaults["BW_SYSTEMD_SERVICE"] = settings["service"] + ".service"
    elif product == "BananaWiki":
        defaults = {"HOSTING_HOST": "127.0.0.1", "HOSTING_PORT": str(settings["port"]), "HOSTING_PROXY_MODE": "1" if settings.get("domain") else "0",
                    "HOSTING_PUBLIC_SCHEME": "https" if settings.get("domain") else "http", "HOSTING_MODE": "subdomain" if settings.get("domain") else "port",
                    "HOSTING_PUBLIC_HOST": "127.0.0.1", "HOSTING_DATABASE_PATH": str(data / "hosting.db"),
                    "HOSTING_SECRET_KEY_PATH": str(data / ".secret_key"), "HOSTING_BACKUP_KEY_PATH": str(data / ".backup_encryption_key"),
                    "INSTANCES_DIR": str(data / "instances"), "STATIC_SITE_DIR": str(root / "site"),
                    "HOSTING_IMPORT_TEMP_DIR": str(data / "imports"), "HOSTING_EXPORT_TEMP_DIR": str(data / "exports"),
                    "HOSTING_BOOTSTRAP_TOKEN": secrets.token_hex(32), "BASE_DOMAIN": settings.get("domain", ""),
                    "PORTAL_DOMAIN": settings.get("portal_domain", ""), "INSTANCE_URL_SUFFIX": "hosting",
                    "HOSTING_INSTANCE_RUNTIME": "docker", "BW_SOURCE_URL": source_link(settings["source_url"])}
        # Portal helpers also import the wiki configuration. Its signing key and
        # temporary files must never be created inside the read-only release.
        defaults.update(wiki_paths(data / "wiki-runtime"))
        values["HOSTING_CONTAINER_IMAGE"] = "bananawiki-tenant:" + settings["revision"]
    elif mode == "compute":
        defaults = {"BC_COMPUTE_HOST": "127.0.0.1", "BC_COMPUTE_PORT": str(settings["port"]),
                    "BC_COMPUTE_UPSTREAM": settings.get("ollama_url") or DEFAULT_OLLAMA_URL,
                    "BC_COMPUTE_TOKEN_FILE": str(data / ".compute-api-token"),
                    "BC_SOURCE_URL": source_link(settings["source_url"])}
    else:
        defaults = {"BC_HOST": "127.0.0.1", "BC_PORT": str(settings["port"]), "BC_INSTANCE_DIR": str(data),
                    "BC_DATABASE_PATH": str(data / "bananachat.db"), "BC_LOG_FILE": str(data / "logs" / "bananachat.log"),
                    "BC_OLLAMA_URL": settings.get("backend_url") or settings.get("ollama_url") or DEFAULT_OLLAMA_URL,
                    "BC_PROXY_MODE": "1" if settings.get("domain") else "0", "BC_PROXY_HOPS": "1",
                    "BC_SECURE_COOKIES": "1" if settings.get("domain") else "0", "BC_ENV": "production",
                    "BC_SOURCE_URL": source_link(settings["source_url"])}
        if first_install:
            defaults["BC_SETUP_TOKEN"] = secrets.token_hex(32)
    if product == "BananaChat" and mode == "web" and not settings.get("backend_url"):
        # A web server never talks to an Ollama of its own by default.
        defaults.pop("BC_OLLAMA_URL")
    for key, value in defaults.items():
        values.setdefault(key, value)
    if product == "BananaChat" and first_install:
        # What the operator chose on the install command line wins over a
        # reused configuration; later updates and restores keep their edits.
        chosen = settings.get("backend_url") if mode == "web" else settings.get("ollama_url")
        if chosen:
            values["BC_COMPUTE_UPSTREAM" if mode == "compute" else "BC_OLLAMA_URL"] = chosen
    if product == "BananaChat" and managed_ollama(settings):
        values.setdefault("OLLAMA_HOST", "127.0.0.1:11434")
        values.setdefault("OLLAMA_MODELS", str(data / "models"))
        managed = values["OLLAMA_HOST"] if "://" in values["OLLAMA_HOST"] else "http://" + values["OLLAMA_HOST"]
        if first_install and mode == "compute" and loopback_http(managed):
            # The gateway protects the Ollama this installation starts.
            values["BC_COMPUTE_UPSTREAM"] = managed.rstrip("/")
    return values


def service_commands(settings):
    root, name = Path(settings["root"]), settings["service"]
    source = root / "current"
    python, gunicorn = source / ".venv/bin/python", source / ".venv/bin/gunicorn"
    if settings["product"] == "BananaWiki":
        if settings["mode"] == "hosting":
            return {name: [str(gunicorn), "-c", "hosting/gunicorn.conf.py", "--timeout", "180", "hosting.wsgi:app"],
                    name + "-maintenance": [str(python), "-m", "hosting.maintenance", "--interval", "300"]}
        return {name: [str(gunicorn), "-c", "gunicorn.conf.py", "wsgi:app"], name + "-tts": [str(python), "scripts/tts_worker.py"]}
    commands = {}
    if managed_ollama(settings):
        commands[name + "-ollama"] = [settings["ollama_binary"], "serve"]
    commands[name] = ([str(python), "-m", "compute.inference_proxy"] if settings["mode"] == "compute"
                      else [str(gunicorn), "-c", "gunicorn.conf.py", "wsgi:app"])
    return commands


def health_urls(settings):
    url = f"http://127.0.0.1:{settings['port']}/health"
    if settings["mode"] == "compute":
        url += "z"
    urls = [url]
    if settings["product"] == "BananaChat" and settings["mode"] in {"single", "compute"}:
        urls.append(ollama_upstream(settings) + "/api/version")
    return urls


def caddyfile(settings):
    domain = settings.get("domain")
    if not domain:
        raise ValueError("Configure a domain before generating an HTTPS proxy configuration.")
    port = settings["port"]
    if settings["mode"] == "hosting":
        portal = settings["portal_domain"]
        site = Path(settings["root"]) / "site"
        return ("# Managed by BananaSuite\n{\n\tservers {\n\t\ttimeouts {\n\t\t\tread_header 10s\n\t\t\tread_body 5m\n\t\t\tidle 2m\n\t\t}\n\t}\n\ton_demand_tls {\n"
                f"\t\task http://127.0.0.1:{port}/internal/domains/authorize\n\t}}\n}}\n\n"
                f"{domain} {{\n\troot * {site}\n\tfile_server {{\n\t\thide .git .env\n\t}}\n}}\n\n"
                f"{portal} {{\n\treverse_proxy 127.0.0.1:{port}\n}}\n\n"
                f":443 {{\n\ttls {{\n\t\ton_demand\n\t}}\n\treverse_proxy 127.0.0.1:{port}\n}}\n")
    return ("# Managed by BananaSuite\n{\n\tservers {\n\t\ttimeouts {\n\t\t\tread_header 10s\n\t\t\tread_body 5m\n\t\t\tidle 2m\n\t\t}\n\t}\n}\n\n"
            f"{domain} {{\n\treverse_proxy 127.0.0.1:{port} {{\n\t\tflush_interval -1\n\t}}\n}}\n")
