#!/usr/bin/env python3
"""Primavera P6 PDS Table / Column Selector & Query Body Builder.

- Dynamic authentication: Accepts PDS credentials directly from the web interface
  (or optional server fallback via .env).
- Hostable: Binds to 0.0.0.0 (or HOST env) for team / server deployment.
- Multi-schema support: Supports ds_p6adminuser (standard admin tables) and
  ds_p6reportuser (reporting views with custom SQL definitions).
- SQL view definitions are strictly restricted to ds_p6reportuser only.
- Caches schema metadata in pds_metadata_cache.json.
- Serves pds_table_selector.html on http://0.0.0.0:8765.

Install dependency: python -m pip install requests
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

try:
    import requests
except ImportError:
    print("Missing dependency: requests\nInstall it with: python -m pip install requests")
    sys.exit(1)


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def load_dotenv_file(dotenv_path: Path) -> None:
    """Load key-value pairs from a .env file into os.environ."""
    if not dotenv_path.is_file():
        return
    try:
        content = dotenv_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key:
                    os.environ[key] = val
    except Exception as exc:
        logging.warning("Could not load .env file %s: %s", dotenv_path, exc)


# ----------------------- Configuration -----------------------
ENV_FILE = Path(__file__).with_name(".env")
load_dotenv_file(ENV_FILE)

PDS_ORIGIN = (
    os.getenv("PDS_ORIGIN")
    or os.getenv("P6_BASE_URL")
    or "https://us2.p6.oraclecloud.com/stackinfra/pds/rest-service/dataservice"
)
DEFAULT_CONFIG_CODE = "ds_p6adminuser"
ALLOWED_CONFIG_CODES = ["ds_p6adminuser", "ds_p6reportuser"]

# Default to 0.0.0.0 for hosting so remote/network browsers can connect.
HOST = os.getenv("HOST") or "0.0.0.0"
PORT = int(os.getenv("PORT") or "8765")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS") or "45")
# Cloud environments (Render, Fly, etc.) inject RENDER or PORT; suppress local GUI browser launch in the cloud
IS_CLOUD_HOSTED = bool(os.getenv("RENDER") or (os.getenv("PORT") and os.getenv("PORT") != "8765"))
AUTO_OPEN_BROWSER = False if IS_CLOUD_HOSTED else is_truthy(os.getenv("AUTO_OPEN_BROWSER", "false"))

CACHE_FILE = Path(__file__).with_name("pds_metadata_cache.json")
HTML_FILE = Path(__file__).with_name("pds_table_selector.html")
VIEW_DEFS_FILE = Path(__file__).with_name("p6_reporting_view_definitions.json")

VIEW_DEFINITIONS: dict[str, dict[str, Any]] = {}
if VIEW_DEFS_FILE.exists():
    try:
        _raw_defs = json.loads(VIEW_DEFS_FILE.read_text(encoding="utf-8"))
        VIEW_DEFINITIONS = _raw_defs.get("views", {})
        logging.info("Loaded %d reporting view definitions from %s", len(VIEW_DEFINITIONS), VIEW_DEFS_FILE.name)
    except Exception as exc:
        logging.warning("Could not load reporting view definitions from %s: %s", VIEW_DEFS_FILE, exc)

# Optional table allow-list from .env or script. Leave empty to use all tables.
_tables_env = os.getenv("TABLES_OVERRIDE", "").strip()
TABLES_OVERRIDE: list[str] = [t.strip() for t in _tables_env.split(",") if t.strip()] if _tables_env else []
HIDE_BLACKLISTED_FROM_DEFAULT_LIST = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("pds-table-selector")


def extract_table_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize a PDS metadata/tables response to [{physicalTableName, ...}]."""
    if isinstance(payload, dict):
        for key in ("data", "tables", "items", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("Expected a JSON array from the metadata tables endpoint.")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = row.get("physicalTableName") or row.get("tableName") or row.get("name")
        if not name:
            continue
        name = str(name).strip()
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "tableName": name,
            "displayName": row.get("displayTableName") or row.get("displayName") or name,
            "tableType": row.get("tableType") or "UNKNOWN",
            "isBlackListed": is_truthy(row.get("isBlackListed", False)),
            "syncConfigCode": row.get("syncConfigCode", DEFAULT_CONFIG_CODE),
        })
    normalized.sort(key=lambda row: row["tableName"].upper())
    return normalized


def extract_columns(payload: Any) -> list[dict[str, Any]]:
    """Normalize PDS metadata/columns response to a list of column metadata objects."""
    if isinstance(payload, dict):
        for key in ("data", "columns", "items", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("Expected a JSON array from the metadata columns endpoint.")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = row.get("columnName") or row.get("name")
        if not name:
            continue
        name = str(name).strip()
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "columnName": name,
            "displayName": row.get("displayName") or name,
            "dataType": row.get("dataType") or "",
            "physicalDataType": row.get("physicalDataType") or "",
            "isPK": is_truthy(row.get("isPK", False)),
            "isFK": is_truthy(row.get("isFK", False)),
            "fkTableName": row.get("fkTableName") or "",
            "tableType": row.get("tableType") or "",
        })
    return normalized


class PDSClient:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({"Accept": "application/json"})

    def get_json(self, url: str) -> Any:
        response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"PDS returned HTTP {response.status_code}: Invalid credentials or unauthorized access."
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            excerpt = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"PDS returned HTTP {response.status_code}: {excerpt}") from exc
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            excerpt = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"PDS response was not JSON: {excerpt}") from exc

    def fetch_tables(self, config_code: str = DEFAULT_CONFIG_CODE) -> list[dict[str, Any]]:
        url = f"{PDS_ORIGIN}/metadata/tables?configCode={quote(config_code)}"
        return extract_table_list(self.get_json(url))

    def fetch_columns(self, table_name: str, config_code: str = DEFAULT_CONFIG_CODE) -> list[dict[str, Any]]:
        safe_name = quote(table_name, safe="")
        url = f"{PDS_ORIGIN}/metadata/columns/{safe_name}?configCode={quote(config_code)}"
        return extract_columns(self.get_json(url))


class ClientManager:
    """Manages PDSClient instances for different credentials."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: dict[tuple[str, str], PDSClient] = {}

    def get_client(self, username: str, password: str) -> PDSClient:
        key = (username, password)
        with self.lock:
            if key not in self.clients:
                self.clients[key] = PDSClient(username, password)
            return self.clients[key]

    def verify_credentials(self, username: str, password: str) -> tuple[bool, str]:
        """Test credentials against PDS metadata endpoint."""
        try:
            client = self.get_client(username, password)
            client.fetch_tables(config_code=DEFAULT_CONFIG_CODE)
            return True, "Authenticated successfully"
        except Exception as exc:
            return False, str(exc)


class MetadataStore:
    def __init__(self, client_mgr: ClientManager, tables: list[dict[str, Any]]) -> None:
        self.client_mgr = client_mgr
        self.lock = threading.RLock()
        self.cache: dict[str, Any] = {
            "configCode": DEFAULT_CONFIG_CODE,
            "tables": tables,
            "tablesByConfig": {
                DEFAULT_CONFIG_CODE: tables
            },
            "columns": {},
            "errors": {},
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if CACHE_FILE.exists():
            try:
                old = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(old.get("columns"), dict):
                    self.cache["columns"] = old["columns"]
                if isinstance(old.get("tablesByConfig"), dict):
                    self.cache["tablesByConfig"].update(old["tablesByConfig"])
                elif isinstance(old.get("tables"), list):
                    cfg = old.get("configCode") or DEFAULT_CONFIG_CODE
                    self.cache["tablesByConfig"][cfg] = old["tables"]
            except (OSError, json.JSONDecodeError, AttributeError):
                LOG.warning("Ignoring unreadable cache file: %s", CACHE_FILE)
        self.save()

    def save(self) -> None:
        with self.lock:
            self.cache["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
            temp_path = CACHE_FILE.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self.cache, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(CACHE_FILE)

    def tables_for_ui(self, config_code: str = DEFAULT_CONFIG_CODE, client: PDSClient | None = None) -> list[dict[str, Any]]:
        with self.lock:
            tables = self.cache.setdefault("tablesByConfig", {}).get(config_code)
            if not tables and CACHE_FILE.exists():
                try:
                    on_disk = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                    tables = on_disk.get("tablesByConfig", {}).get(config_code)
                    if tables:
                        self.cache["tablesByConfig"][config_code] = tables
                except Exception:
                    pass
        if not tables and client:
            try:
                LOG.info("Fetching tables from PDS endpoint for configCode: %s", config_code)
                tables = client.fetch_tables(config_code=config_code)
                with self.lock:
                    self.cache["tablesByConfig"][config_code] = tables
                    self.save()
            except Exception as exc:
                LOG.error("Failed to fetch tables for %s: %s", config_code, exc)
                tables = []
        if not tables:
            return []

        if HIDE_BLACKLISTED_FROM_DEFAULT_LIST:
            tables = [t for t in tables if not t.get("isBlackListed")]
        if TABLES_OVERRIDE:
            allowed = {x.upper() for x in TABLES_OVERRIDE}
            tables = [t for t in tables if t["tableName"].upper() in allowed]
            found = {t["tableName"].upper() for t in tables}
            for name in TABLES_OVERRIDE:
                if name.upper() not in found:
                    tables.append({"tableName": name, "displayName": name, "tableType": "UNKNOWN", "isBlackListed": False})
        return sorted(tables, key=lambda t: t["tableName"].upper())

    def get_columns(self, table_name: str, config_code: str = DEFAULT_CONFIG_CODE, client: PDSClient | None = None) -> list[dict[str, Any]]:
        tables = self.cache.get("tablesByConfig", {}).get(config_code) or self.cache.get("tables", [])
        matching = next((t for t in tables if t["tableName"].upper() == table_name.upper()), None)
        if matching and matching.get("isBlackListed"):
            raise PermissionError(f"{table_name} is marked blacklisted by the PDS metadata endpoint.")
        cache_key = f"{config_code}:{table_name.upper()}"
        with self.lock:
            existing = self.cache["columns"].get(cache_key) or self.cache["columns"].get(table_name.upper())
            if isinstance(existing, list) and existing:
                return existing
        if not client:
            raise PermissionError("Credentials required to fetch column metadata from PDS.")
        # Fetch outside lock to avoid blocking other requests for the entire network wait.
        columns = client.fetch_columns(table_name, config_code=config_code)
        with self.lock:
            self.cache["columns"][cache_key] = columns
            self.cache["columns"][table_name.upper()] = columns
            self.cache["errors"].pop(cache_key, None)
            self.save()
        return columns


class Handler(BaseHTTPRequestHandler):
    store: MetadataStore
    client_mgr: ClientManager

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-PDS-Username, X-PDS-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-PDS-Username, X-PDS-Password")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def get_credentials(self) -> tuple[str, str] | None:
        """Resolve credentials from request headers or server .env fallback."""
        # 1. Custom headers sent by web client
        user = self.headers.get("X-PDS-Username")
        pwd = self.headers.get("X-PDS-Password")
        if user and pwd:
            return user.strip(), pwd.strip()

        # 2. HTTP Basic Auth
        auth_header = self.headers.get("Authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                import base64
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                if ":" in decoded:
                    u, p = decoded.split(":", 1)
                    if u and p:
                        return u.strip(), p.strip()
            except Exception:
                pass

        # 3. Server fallback from .env (if present)
        env_user = os.getenv("PDS_USERNAME") or os.getenv("P6_USERNAME")
        env_pwd = os.getenv("PDS_PASSWORD") or os.getenv("P6_PASSWORD")
        if env_user and env_pwd and env_user not in ("your_p6_username", "your_username", ""):
            return env_user.strip(), env_pwd.strip()

        return None

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/api/login", "/api/auth"):
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception:
                body = {}

            username = (body.get("username") or self.headers.get("X-PDS-Username") or "").strip()
            password = (body.get("password") or self.headers.get("X-PDS-Password") or "").strip()

            if not username or not password:
                self.send_json({"ok": False, "error": "Username and password cannot be empty."}, 400)
                return

            ok, msg = self.client_mgr.verify_credentials(username, password)
            if ok:
                client = self.client_mgr.get_client(username, password)
                try:
                    tables = self.store.tables_for_ui(DEFAULT_CONFIG_CODE, client=client)
                    table_count = len(tables)
                except Exception:
                    table_count = 0
                self.send_json({
                    "ok": True,
                    "username": username,
                    "message": "Connected to PDS successfully",
                    "tableCount": table_count
                })
            else:
                self.send_json({"ok": False, "error": msg}, 401)
            return

        self.send_error(404, "Not found")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/api/health"):
            self.send_json({"status": "ok", "app": "primavera_p6"})
            return

        if parsed.path in ("/", "/index.html"):
            if not HTML_FILE.exists():
                self.send_error(500, f"HTML file missing: {HTML_FILE.name}")
                return
            data = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/auth-status":
            creds = self.get_credentials()
            env_user = os.getenv("PDS_USERNAME") or os.getenv("P6_USERNAME")
            has_server_fallback = bool(env_user and env_user not in ("your_p6_username", "your_username", ""))
            if creds:
                self.send_json({
                    "authenticated": True,
                    "username": creds[0],
                    "isServerFallback": bool(has_server_fallback and creds[0] == env_user),
                    "hasServerFallback": has_server_fallback
                })
            else:
                self.send_json({
                    "authenticated": False,
                    "username": None,
                    "isServerFallback": False,
                    "hasServerFallback": has_server_fallback
                })
            return

        if parsed.path == "/api/tables":
            creds = self.get_credentials()
            client = self.client_mgr.get_client(*creds) if creds else None

            raw_query = parse_qs(parsed.query)
            query = {k.lower(): v for k, v in raw_query.items()}
            raw_code = (query.get("configcode") or query.get("config_code") or [DEFAULT_CONFIG_CODE])[0].strip()
            config_code = next((c for c in ALLOWED_CONFIG_CODES if c.lower() == raw_code.lower()), DEFAULT_CONFIG_CODE)
            is_reporting = (config_code.lower() == "ds_p6reportuser")

            raw_tables = self.store.tables_for_ui(config_code=config_code, client=client)
            if not raw_tables and not creds:
                self.send_json({"error": "Authentication required. Please enter your PDS username and password."}, 401)
                return

            # Strictly only attach hasDefinition for reporting user (ds_p6reportuser)
            tables = [
                dict(t, hasDefinition=(is_reporting and (t["tableName"].upper() in VIEW_DEFINITIONS)))
                for t in raw_tables
            ]
            LOG.info("API /api/tables serving %d tables for configCode=%s (user=%s)", len(tables), config_code, creds[0] if creds else "anon")
            self.send_json({
                "tables": tables,
                "configCode": config_code,
                "allowedConfigCodes": ALLOWED_CONFIG_CODES,
                "count": len(tables),
                "totalDefinitions": len(VIEW_DEFINITIONS) if is_reporting else 0
            })
            return

        if parsed.path == "/api/columns":
            raw_query = parse_qs(parsed.query)
            query = {k.lower(): v for k, v in raw_query.items()}
            table_name = (query.get("table") or [""])[0].strip()
            raw_code = (query.get("configcode") or query.get("config_code") or [DEFAULT_CONFIG_CODE])[0].strip()
            config_code = next((c for c in ALLOWED_CONFIG_CODES if c.lower() == raw_code.lower()), DEFAULT_CONFIG_CODE)
            is_reporting = (config_code.lower() == "ds_p6reportuser")

            if not table_name:
                self.send_json({"error": "Missing table query parameter."}, 400)
                return

            creds = self.get_credentials()
            client = self.client_mgr.get_client(*creds) if creds else None

            # Strictly only provide view definition for reporting user (ds_p6reportuser)
            if is_reporting:
                view_def_obj = VIEW_DEFINITIONS.get(table_name.upper())
                view_def = view_def_obj.get("definition") if view_def_obj else None
            else:
                view_def_obj = None
                view_def = None

            try:
                columns = self.store.get_columns(table_name, config_code=config_code, client=client)
                LOG.info("API /api/columns serving %d columns for table=%s (%s, hasDef=%s)", len(columns), table_name, config_code, bool(view_def))
                self.send_json({
                    "tableName": table_name,
                    "columns": columns,
                    "configCode": config_code,
                    "viewDefinition": view_def,
                    "hasDefinition": bool(view_def),
                    "viewParsed": view_def_obj.get("parsed", True) if view_def_obj else False
                })
            except Exception as exc:
                message = str(exc)
                LOG.warning("Could not fetch columns for %s: %s", table_name, message)
                status_code = 401 if ("credentials" in message.lower() or "401" in message or "authentication" in message.lower()) else 200
                with self.store.lock:
                    self.store.cache["errors"][table_name.upper()] = message
                    self.store.save()
                self.send_json({
                    "tableName": table_name,
                    "columns": [],
                    "error": message,
                    "configCode": config_code,
                    "viewDefinition": view_def,
                    "hasDefinition": bool(view_def),
                    "viewParsed": view_def_obj.get("parsed", True) if view_def_obj else False
                }, status_code)
            return

        if parsed.path == "/api/view-definition":
            raw_query = parse_qs(parsed.query)
            query = {k.lower(): v for k, v in raw_query.items()}
            raw_code = (query.get("configcode") or query.get("config_code") or [""])[0].strip()
            # If configCode is provided and it is NOT reporting user, view definition is forbidden
            if raw_code and raw_code.lower() != "ds_p6reportuser":
                self.send_json({
                    "viewName": "",
                    "found": False,
                    "definition": None,
                    "error": "SQL view definitions are only available for ds_p6reportuser."
                }, 400)
                return
            view_name = (query.get("view") or query.get("table") or [""])[0].strip()
            vdef_obj = VIEW_DEFINITIONS.get(view_name.upper())
            if not vdef_obj:
                self.send_json({"viewName": view_name, "found": False, "definition": None}, 404)
                return
            self.send_json({
                "viewName": view_name,
                "found": True,
                "definition": vdef_obj.get("definition"),
                "parsed": vdef_obj.get("parsed", False)
            })
            return

        if parsed.path == "/api/cache-status":
            cache = self.store.cache
            self.send_json({
                "cachedTables": len(cache.get("columns", {})),
                "availableTables": len(self.store.tables_for_ui()),
                "cacheFile": CACHE_FILE.name,
                "updatedAt": cache.get("updatedAt"),
            })
            return

        self.send_error(404, "Not found")


def main() -> None:
    print("\n========================================================")
    print("      Primavera P6 PDS Table / Column Selector          ")
    print("========================================================\n")

    client_mgr = ClientManager()

    # Read credentials from .env if available (optional default fallback)
    username = os.getenv("PDS_USERNAME") or os.getenv("P6_USERNAME")
    password = os.getenv("PDS_PASSWORD") or os.getenv("P6_PASSWORD")

    if username in ("your_p6_username", "your_username", ""):
        username = None
    if password in ("your_p6_password", "your_password", ""):
        password = None

    if username and password:
        print(f"Default server credentials loaded from .env for user: '{username}'")
    else:
        print("Note: No default credentials in .env.")
        print("Users will enter their PDS username & password directly in the web UI.")

    tables = []
    if CACHE_FILE.exists():
        try:
            cached_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            tables = (
                cached_data.get("tablesByConfig", {}).get(DEFAULT_CONFIG_CODE)
                or cached_data.get("tables", [])
            )
            if tables:
                print(f"Loaded {len(tables)} tables from local cache: {CACHE_FILE.name}")
        except Exception:
            pass

    if not tables and username and password:
        print("Fetching initial table list from PDS using default credentials...")
        try:
            init_client = client_mgr.get_client(username, password)
            tables = init_client.fetch_tables(config_code=DEFAULT_CONFIG_CODE)
        except Exception as exc:
            print(f"Notice: Initial table fetch failed ({exc}). Server will still start and fetch upon user sign-in.")

    store = MetadataStore(client_mgr, tables)

    if not HTML_FILE.exists():
        print(f"HTML file not found: {HTML_FILE}")
        print("Keep pds_table_selector.html in the same folder as this script.")
        sys.exit(1)

    handler_class = type("PDSRequestHandler", (Handler,), {"store": store, "client_mgr": client_mgr})
    server = ThreadingHTTPServer((HOST, PORT), handler_class)

    local_url = f"http://127.0.0.1:{PORT}"
    network_url = f"http://{HOST}:{PORT}" if HOST not in ("0.0.0.0", "") else f"http://<your-server-ip>:{PORT}"
    print(f"\n========================================================")
    print(f" PDS Query Builder Server is Running!")
    print(f" Local URL:   {local_url}")
    print(f" Network URL: {network_url} (Accessible to hosted network clients)")
    print(f" Metadata cache: {CACHE_FILE}")
    print(f" Web Login:   Users can enter username/password in browser")
    print(f" Press Ctrl+C in this terminal to stop the server.")
    print(f"========================================================\n")

    if AUTO_OPEN_BROWSER:
        def _open_browser() -> None:
            time.sleep(0.6)
            try:
                webbrowser.open(local_url)
            except Exception as e:
                LOG.debug("Could not auto-open browser: %s", e)

        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
