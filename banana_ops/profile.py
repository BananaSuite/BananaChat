"""Application-specific deployment modes for the shared lifecycle manager."""

from pathlib import Path
import os
import re
import secrets
from urllib.parse import urlsplit

from .product import PRODUCT

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
    values.update(BANANA_MAINTENANCE_FILE=str(data / ".banana-maintenance"), PYTHONDONTWRITEBYTECODE="1")
    if product == "BananaWiki" and mode == "wiki":
        defaults = {"BW_INSTANCE_DIR": str(data), "BW_DATABASE_PATH": str(data / "bananawiki.db"),
                    "BW_HOST": "127.0.0.1", "BW_PORT": str(settings["port"]), "BW_PROXY_MODE": "1" if settings.get("domain") else "0",
                    "BW_PREFERRED_URL_SCHEME": "https" if settings.get("domain") else "http", "BW_ENV": "production",
                    "BW_SETUP_TOKEN": secrets.token_hex(32), "BW_SOURCE_URL": source_link(settings["source_url"])}
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
                    "BC_COMPUTE_UPSTREAM": "http://127.0.0.1:11434", "BC_COMPUTE_TOKEN_FILE": str(data / ".compute-api-token"),
                    "BC_SOURCE_URL": source_link(settings["source_url"])}
    else:
        defaults = {"BC_HOST": "127.0.0.1", "BC_PORT": str(settings["port"]), "BC_INSTANCE_DIR": str(data),
                    "BC_DATABASE_PATH": str(data / "bananachat.db"), "BC_LOG_FILE": str(data / "logs" / "bananachat.log"),
                    "BC_OLLAMA_URL": settings.get("backend_url") or "http://127.0.0.1:11434",
                    "BC_PROXY_MODE": "1" if settings.get("domain") else "0", "BC_PROXY_HOPS": "1",
                    "BC_SECURE_COOKIES": "1" if settings.get("domain") else "0", "BC_ENV": "production",
                    "BC_SETUP_TOKEN": secrets.token_hex(32), "BC_SOURCE_URL": source_link(settings["source_url"])}
    for key, value in defaults.items():
        values.setdefault(key, value)
    if product == "BananaChat" and mode in {"single", "compute"}:
        values.update(OLLAMA_HOST="127.0.0.1:11434", OLLAMA_MODELS=str(data / "models"))
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
    if settings["mode"] in {"single", "compute"}:
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
        urls.append("http://127.0.0.1:11434/api/version")
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
