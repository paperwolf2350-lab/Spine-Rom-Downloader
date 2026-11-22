"""Search Internet Archive collections linked by the r/ROMs Megathread."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import tkinter as tk
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from http.client import HTTPException
from itertools import chain
from operator import itemgetter
from http.cookies import CookieError, SimpleCookie
from pathlib import Path, PurePosixPath
from queue import Empty, SimpleQueue
from time import monotonic
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = "https://r-roms.gitlab.io/"
ALL_PLATFORMS = "All indexed platforms"
STATE_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Megathread ROM Browser"
ARCHIVE = "https://archive.org"
ZIP_ITEM = "ni-roms"
ZIP_SCOPE = "roms/Nintendo - Game Boy.zip"
ZIP_CATEGORY = "Game Boy ZIP (Internet Archive)"
BLUE = "https://www.blueroms.ws"
BLUE_SUFFIX = " (BlueRoms)"
BLUE_PLATFORMS = {
    "Arcade", "Dreamcast", "Game Boy Advance", "Mega Drive - Genesis", "Neo Geo",
    "Nintendo 3DS", "Nintendo 64", "Nintendo DS", "Nintendo GameCube",
    "Nintendo Switch", "Nintendo Switch 2", "Nintendo Wii", "Nintendo Wii U",
    "PlayStation", "PlayStation 2", "PlayStation 3", "PlayStation 4", "PlayStation 5",
    "PlayStation Portable", "PlayStation Vita", "Saturn", "Super Nintendo",
    "Xbox", "Xbox 360",
}
EXTENSIONS = set(
    ".zip .7z .rar .iso .chd .rvz .wbfs .wua .cia .3ds .nds .gba .gbc .gb "
    ".nes .sfc .smc .n64 .z64 .v64 .bin .cue .gdi .cso .pbp .pce .a26 .a52 "
    ".a78 .pkg .vpk .cdi .ciso .nrg .cci .nsz .xcz .wud .wux .sms .gg .md .gen .smd .32x .j64 .jag .xiso .wad .nsp .xci".split()
)


def trusted(url, service):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.netloc.lower() != host:
        return False
    if service == "guide":
        return host == "r-roms.gitlab.io"
    if service == "blue":
        return host == "www.blueroms.ws"
    if service == "blue-file":
        return host == "files.blueroms.ws"
    return host == "archive.org" or bool(re.fullmatch(
        r"(?:ia|dn)[0-9]+\.(?:[a-z0-9-]+\.)?archive\.org", host
    ))


class Redirects(HTTPRedirectHandler):
    def __init__(self, service, archive_cookie=''):
        self.service = service
        self.archive_cookie = archive_cookie

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.service == "archive-login" or not trusted(newurl, self.service):
            raise ValueError(f"The server redirected to an unapproved host ({urlsplit(newurl).hostname or 'unknown'}).")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and self.archive_cookie:
            redirected.add_unredirected_header("Cookie", self.archive_cookie)
        return redirected


def request(url, service, archive_cookie='', start=0):
    if not trusted(url, service):
        raise ValueError("URL is outside the approved hosts.")
    cookie = archive_cookie if service == "archive" else ""
    opener = build_opener(Redirects(service, cookie))
    req = Request(url, headers={"User-Agent": "Megathread ROM Browser/0.1"})
    if start:
        req.add_header("Range", f"bytes={start}-")
    if cookie:
        req.add_unredirected_header("Cookie", cookie)
    return opener.open(req, timeout=20)


def archive_sign_in(email, password):
    body = urlencode({"email": email, "password": password}).encode("ascii")
    req = Request(f"{ARCHIVE}/services/xauthn/?op=login", data=body)
    opener = build_opener(Redirects("archive-login"))
    status = 200
    try:
        try:
            response = opener.open(req, timeout=20)
        except HTTPError as exc:
            response = exc
            status = exc.code
        with response:
            result = json.loads(response.read(256 * 1024))
            headers = getattr(response, "headers", None)
            set_cookies = (headers.get_all("Set-Cookie") or []) if headers else []
    except (OSError, ValueError) as exc:
        raise ValueError(f"Internet Archive sign-in returned no usable response (HTTP {status}).") from exc

    if isinstance(result, dict):
        values = result.get("values")
        values = values if isinstance(values, dict) else {}
        if result.get("success") is True:
            names = ("logged-in-user", "logged-in-sig")
            cookies = values.get("cookies")
            cookies = cookies if isinstance(cookies, dict) else {}
            header_cookies = SimpleCookie()
            for header in set_cookies:
                try:
                    header_cookies.load(header)
                except CookieError:
                    continue

            session = {}
            for name in names:
                header_cookie = header_cookies.get(name)
                for value in (cookies.get(name), header_cookie.value if header_cookie else None):
                    if isinstance(value, str):
                        value = value.strip()
                        if re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+", value):
                            session[name] = value
                            break

            missing = [name for name in names if name not in session]
            if not missing:
                return "; ".join(f"{name}={session[name]}" for name in names)
            raise ValueError(
                "Internet Archive accepted the sign-in, but its response did not include "
                f"usable session cookies ({', '.join(missing)}). "
                "You can use Open on Archive.org to try the item in your signed-in browser."
            )

        reason = values.get("reason") or result.get("error")
        if reason == "account_not_found":
            raise ValueError("Internet Archive’s password endpoint could not find this account email.")
        if reason == "account_bad_password":
            raise ValueError("Internet Archive’s password endpoint rejected the password.")
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,64}", reason):
            raise ValueError(f"Internet Archive declined this sign-in (HTTP {status}, {reason}).")

    if status == 429:
        raise ValueError("Internet Archive is limiting sign-in attempts. Try again later.")
    raise ValueError(f"Internet Archive declined this sign-in (HTTP {status}).")


def read(url, service):
    limit = 64 * 1024 * 1024
    with request(url, service) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The collection listing exceeds the size limit.")
    return data


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.href = None
        self.parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(" ".join(self.parts).split())))
            self.href = None


def categories():
    result = {}
    for href, label in Links(read(ROOT, "guide").decode("utf-8")).links:
        url = urljoin(ROOT, href)
        parsed = urlsplit(url)
        if label and trusted(url, "guide") and re.fullmatch(r"/megathread/[^/]+/?", parsed.path):
            clean_url = ROOT.rstrip("/") + parsed.path.rstrip("/") + "/"
            if clean_url not in result.values():
                result[label] = clean_url
    return result


def collections(url):
    items = set()
    for href, _ in Links(read(url, "guide").decode("utf-8")).links:
        candidate = urljoin(url, href)
        if not trusted(candidate, "archive"):
            continue
        parsed = urlsplit(candidate)
        match = re.fullmatch(r"/(?:details|download)/([\w.-]+)(?:/(.*))?", parsed.path, re.ASCII)
        if match and match[1] not in {".", ".."}:
            items.add((match[1], unquote(match[2] or "").rstrip("/")))
    return sorted(items)


@dataclass(frozen=True)
class Rom:
    name: str
    collection: str
    url: str
    size: int
    sha1: str
    locked: bool
    source: str = "Internet Archive"
    archive_member: bool = False


def rom_from(entry):
    """Build a Rom from saved data, or return None if the data is unusable."""
    if not isinstance(entry, dict):
        return None
    try:
        rom = Rom(**{key: entry[key] for key in Rom.__dataclass_fields__ if key in entry})
    except TypeError:
        return None
    texts = (rom.name, rom.collection, rom.url, rom.sha1, rom.source)
    if not all(isinstance(text, str) for text in texts) or not rom.name or not rom.url:
        return None
    if not isinstance(rom.size, int) or isinstance(rom.size, bool):
        return None
    return rom


@lru_cache(maxsize=16)
def metadata(identifier):
    return json.loads(read(f"{ARCHIVE}/metadata/{quote(identifier)}", "archive"))


def listed_files(identifier, scope):
    item = metadata(identifier)
    if not item.get("metadata"):
        raise ValueError("Archive item unavailable.")
    title = item["metadata"].get("title") or identifier
    locked = item["metadata"].get("access-restricted-item") in (True, "true", "1")
    results = []
    for entry in item.get("files", []):
        name = entry.get("name", "")
        path = PurePosixPath(name)
        if scope and name != scope and not name.startswith(scope + "/"):
            continue
        if path.suffix.lower() not in EXTENSIONS or path.is_absolute() or ".." in path.parts:
            continue
        if entry.get("source") not in (None, "original"):
            continue
        size = entry.get("size", "")
        sha1 = entry.get("sha1", "")
        results.append(Rom(
            name, title, f"{ARCHIVE}/download/{quote(identifier)}/{quote(name)}",
            int(size) if str(size).isdigit() else 0,
            sha1.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", sha1) else "",
            locked or entry.get("private") in (True, "true", "1"),
        ))
    return results


def archive_members(archive, cookie="", extensions=EXTENSIONS):
    prefix = urlsplit(archive.url).path.rstrip("/") + "/"
    url = ARCHIVE + prefix
    response = request(url, "archive", cookie) if cookie else request(url, "archive")
    with response:
        limit = 8 * 1024 * 1024
        page = response.read(limit + 1)
        if len(page) > limit:
            raise ValueError("The ZIP member listing exceeds the size limit.")
        base = response.url
    members = {}
    for href, label in Links(page.decode("utf-8", errors="replace")).links:
        target = urlsplit(urljoin(base, href))
        if target.netloc != "archive.org" or target.scheme != "https" or target.query:
            continue
        if not target.path.startswith(prefix):
            continue
        member = unquote(target.path[len(prefix):])
        path = PurePosixPath(member)
        if (path.is_absolute() or ".." in path.parts or "\\" in member
                or path.suffix.lower() not in extensions or label != member):
            continue
        direct = url + quote(member, safe="")
        members[direct] = asdict(Rom(
            path.name, archive.name, direct, 0, "", archive.locked,
            archive_member=True,
        ))
    if not members:
        raise ValueError("Internet Archive returned no supported individual file links.")
    return members


def blue_links(url):
    for href, label in Links(read(url, "blue").decode("utf-8")).links:
        candidate = urljoin(url, href)
        yield candidate, urlsplit(candidate).path, label


def blue_platforms():
    platforms = {}
    for url, path, label in blue_links(BLUE + "/"):
        if label in BLUE_PLATFORMS and trusted(url, "blue") and re.fullmatch(r"/[a-z0-9_-]+/?", path):
            platforms[label] = BLUE + path.rstrip("/")
    if not platforms:
        raise ValueError("BlueRoms returned no supported platform links.")
    return dict(sorted(platforms.items()))


def blue_catalog(platform, platform_url, progress):
    platform_path = urlsplit(platform_url).path.rstrip("/")
    next_page = platform_url
    visited = set()
    games = {}
    while next_page and next_page not in visited and len(visited) < 500:
        visited.add(next_page)
        following = None
        page_games = set()
        for url, path, label in blue_links(next_page):
            if not trusted(url, "blue"):
                continue
            if label and re.fullmatch(r"/game" + re.escape(platform_path) + r"/[^/]+/?", path):
                game = BLUE + path.rstrip("/")
                page_games.add(game)
                games.setdefault(game, asdict(Rom(
                    label, platform, game, 0, "", False, "BlueRoms",
                )))
            if label in {"»", "Next", "Next »"} and path.rstrip("/") == platform_path and urlsplit(url).query and url.split("#", 1)[0] != next_page:
                following = url.split("#", 1)[0]
        if not page_games:
            raise ValueError(f"BlueRoms returned a catalog page with no {platform} titles.")
        next_page = following
        progress(len(visited), len(games))
    if next_page:
        raise ValueError("BlueRoms pagination did not reach the end of the catalog.")
    return {platform_path[1:]: games}


def resolve_blue(rom):
    pages = {
        url for url, path, label in blue_links(rom.url)
        if trusted(url, "blue") and path.startswith("/download/") and label
    }
    if len(pages) != 1:
        raise ValueError("BlueRoms did not expose a single download page for this game.")
    files = {}
    for url, path, label in blue_links(pages.pop()):
        name = PurePosixPath(unquote(path)).name
        if trusted(url, "blue-file") and PurePosixPath(name).suffix.lower() in EXTENSIONS:
            files[url] = name
        elif trusted(url, "blue-file") and PurePosixPath(label).suffix.lower() in EXTENSIONS:
            files[url] = label
    if len(files) != 1:
        raise ValueError("BlueRoms did not expose a single supported direct file.")
    url, name = files.popitem()
    return replace(rom, name=name, url=url)


class DownloadCanceled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCanceled("Canceled by user.")


class ConnectionDropped(Exception):
    """The network failed part-way. Disk problems are never reported as this."""


def free_space(folder):
    """Free bytes on the drive holding folder, or None if it cannot be told."""
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


def check_space(folder, size):
    free = free_space(folder)
    if size and free is not None and free < size:
        raise ValueError(
            f"Not enough free space: the file needs {size / 1073741824:,.2f} GB "
            f"and the drive has {free / 1073741824:,.2f} GB free."
        )


def download(rom, destination, progress, archive_cookie='', cancel=None, note=None):
    """Save rom to destination.

    If the connection drops part-way, reconnect and ask the host for the rest
    of the file instead of starting over. The finished file is still checked
    against the listed size and SHA-1 as a whole.
    """
    check_cancel(cancel)
    if destination.exists():
        raise FileExistsError(f"A file already exists at {destination}")
    service = "blue-file" if rom.source == "BlueRoms" else "archive"
    written = 0
    expected = rom.size
    digest = hashlib.sha1()
    stalls = 0
    check_space(destination.parent, expected)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
        partial = Path(output.name)
        try:
            while True:
                before = written
                try:
                    try:
                        source = request(rom.url, service, archive_cookie, written)
                    except HTTPError:
                        raise
                    except (OSError, HTTPException) as exc:
                        raise ConnectionDropped(str(exc)) from exc
                    with source:
                        if source.headers.get_content_type() in {"text/html", "application/json", "application/xhtml+xml"}:
                            raise ValueError("The host returned a page instead of a file.")
                        resumed = (source.status == 206 and (source.headers.get("Content-Range") or "")
                                   .startswith(f"bytes {written}-"))
                        if written and not resumed:
                            if source.status == 206:
                                raise ValueError("The host resumed from the wrong place.")
                            # The host sent the whole file again: start over.
                            output.seek(0)
                            output.truncate()
                            written = before = 0
                            digest = hashlib.sha1()
                        if not written and not expected:
                            content_length = source.headers.get("Content-Length")
                            if content_length and content_length.isdecimal():
                                expected = int(content_length)
                                check_space(destination.parent, expected)
                        while True:
                            try:
                                chunk = source.read(256 * 1024)
                            except (OSError, HTTPException) as exc:
                                raise ConnectionDropped(str(exc)) from exc
                            if not chunk:
                                break
                            check_cancel(cancel)
                            written += len(chunk)
                            if expected and written > expected:
                                raise ValueError("The download exceeds the listed file size.")
                            digest.update(chunk)
                            output.write(chunk)
                            progress(written)
                    if expected and written < expected:
                        raise ConnectionDropped("the host closed the connection early")
                    break
                except ConnectionDropped as exc:
                    # Keep trying while each attempt makes progress; give up
                    # after three attempts in a row that receive nothing.
                    stalls = 0 if written > before else stalls + 1
                    if not written or stalls >= 3:
                        raise OSError(f"The connection was lost ({exc}).") from exc
                    if note:
                        note(f"Connection lost at {written / 1048576:,.1f} MiB. Reconnecting…")
                    if cancel is not None:
                        cancel.wait(3 * (stalls + 1))
                    check_cancel(cancel)
            if not written or (expected and written != expected):
                raise ValueError("The download is incomplete.")
            if rom.sha1 and digest.hexdigest() != rom.sha1:
                raise ValueError("The file did not match the published SHA-1.")
            output.close()
            check_cancel(cancel)
            os.rename(partial, destination)
        finally:
            output.close()
            partial.unlink(missing_ok=True)


STATE_NOTES = []
JOB_STATUSES = {
    "Queued", "Downloading", "Canceling", "Completed", "Failed", "Canceled", "Interrupted",
}


def log_problem(text):
    """Add a problem report to errors.log. Never raises."""
    try:
        STATE_PATH.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH / "errors.log", "a", encoding="utf-8") as output:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            output.write(f"[{stamp}]\n{text.rstrip()}\n\n")
    except OSError:
        pass


def whole_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def clean_catalog(raw):
    if not isinstance(raw, dict):
        raise ValueError("The saved catalog has an unexpected layout.")
    catalog = {}
    for label, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        items = entry.get("items")
        items = items if isinstance(items, dict) else {}
        errors = entry.get("errors")
        checked = entry.get("checked")
        try:
            datetime.fromisoformat(checked)
        except (TypeError, ValueError):
            checked = ""
        catalog[label] = {
            "items": {
                identifier: {url: rom for url, rom in files.items() if isinstance(rom, dict)}
                for identifier, files in items.items() if isinstance(files, dict)
            },
            "errors": [str(error) for error in errors] if isinstance(errors, list) else [],
            "read": whole_number(entry.get("read")),
            "total": whole_number(entry.get("total")),
            "checked": checked,
        }
    return catalog


def clean_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("The saved settings have an unexpected layout.")
    return {key: value for key, value in raw.items() if isinstance(value, str)}


def clean_jobs(raw):
    if not isinstance(raw, list):
        raise ValueError("The saved download history has an unexpected layout.")
    jobs = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rom = rom_from(item.get("rom"))
        destination = item.get("destination")
        status = item.get("status")
        if rom is None or not isinstance(destination, str) or not destination or status not in JOB_STATUSES:
            continue
        detail, saved_path = item.get("detail"), item.get("saved_path")
        jobs.append(DownloadJob(
            rom, destination, item.get("choose_filename") is True, status,
            detail if isinstance(detail, str) else "",
            saved_path if isinstance(saved_path, str) else "",
        ))
    return jobs


def load_state(filename, default, clean):
    """Read a saved file. A damaged one is set aside and the default is used."""
    path = STATE_PATH / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        STATE_NOTES.append(f"{filename} could not be read ({exc}).")
        log_problem(f"Could not read {path}: {exc}")
        return default
    try:
        return clean(json.loads(text))
    except Exception as exc:
        kept = path.with_name(filename + ".damaged")
        try:
            path.replace(kept)
            note = f"{filename} was damaged or from an incompatible version. A copy was kept as {kept.name}."
        except OSError:
            note = f"{filename} was damaged or from an incompatible version."
        STATE_NOTES.append(note)
        log_problem(f"Could not use {path}: {exc!r}")
        return default


def save_state(filename, value):
    STATE_PATH.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=STATE_PATH, delete=False
    ) as output:
        partial = Path(output.name)
        try:
            json.dump(value, output, ensure_ascii=False)
            output.close()
            partial.replace(STATE_PATH / filename)
        finally:
            output.close()
            partial.unlink(missing_ok=True)


def scan_category(url, progress):
    groups = {}
    for identifier, scope in collections(url):
        groups.setdefault(identifier, []).append(scope)

    def scan_item(identifier, scopes):
        return [asdict(rom) for scope in scopes for rom in listed_files(identifier, scope)]

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {
            pool.submit(scan_item, identifier, scopes): identifier
            for identifier, scopes in groups.items()
        }
        failures = []
        for count, task in enumerate(as_completed(pending), 1):
            identifier = pending[task]
            try:
                results[identifier] = {rom["url"]: rom for rom in task.result()}
            except (OSError, ValueError) as exc:
                failures.append(f"{identifier} • {exc}")
            progress(count, len(groups))
    return results, failures, set(groups)


BACKGROUND = "#212228"
SURFACE = "#282b33"
CONTROL = "#343946"
BORDER = "#454c5e"
TEXT = "#f4f5f8"
MUTED = "#c5cee0"
AMBER = "#ffc66d"


def apply_theme(window):
    window.configure(background=BACKGROUND)
    window.option_add("*TCombobox*Listbox.background", SURFACE)
    window.option_add("*TCombobox*Listbox.foreground", TEXT)
    window.option_add("*TCombobox*Listbox.selectBackground", AMBER)
    window.option_add("*TCombobox*Listbox.selectForeground", BACKGROUND)
    window.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), background=BACKGROUND, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Accent.TLabel", foreground=AMBER)
    style.configure("TButton", padding=(12, 7), background=CONTROL, bordercolor=BORDER,
                    lightcolor=CONTROL, darkcolor=CONTROL, focuscolor=AMBER)
    style.map("TButton", background=[("disabled", SURFACE), ("active", BORDER)],
              foreground=[("disabled", MUTED)])
    style.configure("Primary.TButton", background=AMBER, foreground=BACKGROUND,
                    font=("Segoe UI", 10, "bold"), bordercolor=AMBER,
                    lightcolor=AMBER, darkcolor=AMBER, focuscolor=BACKGROUND)
    style.map("Primary.TButton", background=[("disabled", "#5c4c36"), ("active", "#ffda9f")],
              foreground=[("disabled", "#e4d8c6")])
    style.configure("TEntry", padding=(9, 7), fieldbackground=SURFACE, insertcolor=TEXT,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE)
    style.configure("TCombobox", padding=(7, 6), fieldbackground=SURFACE, background=CONTROL,
                    arrowcolor=MUTED, bordercolor=BORDER, lightcolor=CONTROL, darkcolor=CONTROL)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)],
              selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", TEXT)])
    for widget in ("TEntry", "TCombobox"):
        style.map(widget, bordercolor=[("focus", AMBER)])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    rowheight=30, bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE)
    style.map("Treeview", background=[("selected", "#51412d")],
              foreground=[("selected", "#ffe0ae")])
    style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background=CONTROL,
                    foreground=MUTED, padding=(8, 7), bordercolor=CONTROL,
                    lightcolor=CONTROL, darkcolor=CONTROL)
    style.configure("Vertical.TScrollbar", background=CONTROL, troughcolor=BACKGROUND,
                    arrowcolor=MUTED, bordercolor=BACKGROUND, lightcolor=CONTROL, darkcolor=CONTROL)
    style.map("Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("TSeparator", background=BORDER)


LIBRARY_PLATFORMS = {
    "Arcade": "arcade", "Dreamcast": "dreamcast", "Game Boy Advance": "gba",
    "Mega Drive - Genesis": "megadrive", "Neo Geo": "neogeo", "Nintendo 3DS": "n3ds", "Nintendo 64": "n64",
    "Nintendo DS": "nds", "Nintendo GameCube": "gc", "Nintendo Switch": "switch",
    "Nintendo Wii": "wii", "Nintendo Wii U": "wiiu", "PlayStation": "psx",
    "PlayStation 2": "ps2", "PlayStation 3": "ps3", "PlayStation 4": "ps4",
    "PlayStation 5": "ps5", "PlayStation Portable": "psp", "PlayStation Vita": "psvita",
    "Saturn": "saturn", "Super Nintendo": "snes", "Xbox": "xbox", "Xbox 360": "xbox360",
}
LIBRARY_EXTENSIONS = {
    ".gb": "gb", ".gbc": "gbc", ".gba": "gba", ".nes": "nes", ".sfc": "snes",
    ".smc": "snes", ".n64": "n64", ".z64": "n64", ".v64": "n64", ".nds": "nds",
    ".3ds": "n3ds", ".cia": "n3ds", ".wua": "wiiu", ".wbfs": "wii",
    ".a26": "atari2600", ".a52": "atari5200", ".a78": "atari7800",
    ".sms": "mastersystem", ".gg": "gamegear", ".32x": "sega32x",
}


# Other folder names people commonly use for the same console.
LIBRARY_FOLDER_NAMES = {
    "psx": ("psx", "ps1", "playstation"), "psvita": ("psvita", "vita"),
    "gc": ("gc", "gamecube", "ngc"), "n3ds": ("n3ds", "3ds"), "nds": ("nds", "ds"),
    "snes": ("snes", "sfc"), "nes": ("nes", "famicom"),
    "megadrive": ("megadrive", "genesis"), "mastersystem": ("mastersystem", "sms"),
    "gamegear": ("gamegear", "gg"), "sega32x": ("sega32x", "32x"),
    "segacd": ("segacd", "megacd"), "pcengine": ("pcengine", "tg16", "pce"),
    "arcade": ("arcade", "mame", "fbneo"),
}
# Console names as they appear in Internet Archive folder and collection names.
# Order matters: the more specific name comes first.
LIBRARY_KEYWORDS = [(re.compile(pattern), console) for pattern, console in (
    (r"playstation ?portable|\bpsp\b", "psp"),
    (r"playstation ?vita|\bps ?vita\b", "psvita"),
    (r"playstation ?2\b|\bps2\b", "ps2"), (r"playstation ?3\b|\bps3\b", "ps3"),
    (r"playstation ?4\b|\bps4\b", "ps4"), (r"playstation ?5\b|\bps5\b", "ps5"),
    (r"playstation|\bpsx\b|\bps1\b", "psx"),
    (r"xbox ?360|\bx360\b", "xbox360"), (r"\bxbox\b", "xbox"),
    (r"game ?boy ?advance|\bgba\b", "gba"), (r"game ?boy ?color|\bgbc\b", "gbc"),
    (r"game ?boy\b", "gb"),
    (r"game ?cube|\bngc\b", "gc"), (r"wii ?u\b", "wiiu"), (r"\bwii\b", "wii"),
    (r"nintendo ?64|\bn64\b", "n64"), (r"nintendo ?3ds|\b3ds\b", "n3ds"),
    (r"nintendo ?dsi?\b|\bnds\b", "nds"), (r"nintendo ?switch", "switch"),
    (r"super ?nintendo|super ?famicom|\bsnes\b", "snes"),
    (r"nintendo ?entertainment ?system|famicom|\bnes\b", "nes"),
    (r"dreamcast", "dreamcast"), (r"saturn", "saturn"),
    (r"mega ?cd|sega ?cd", "segacd"), (r"\b32x\b", "sega32x"),
    (r"mega ?drive|genesis", "megadrive"), (r"master ?system", "mastersystem"),
    (r"game ?gear", "gamegear"), (r"neo ?geo", "neogeo"),
    (r"pc ?engine|turbo ?grafx", "pcengine"),
    (r"atari ?2600", "atari2600"), (r"atari ?5200", "atari5200"), (r"atari ?7800", "atari7800"),
    (r"\bmame\b|\bfbneo\b|final ?burn|arcade", "arcade"),
)]


def archive_console(rom):
    """Work out the console from where a file sits on Internet Archive.

    The file's own name is left out, because game titles often contain
    console words ("Arcade Classics", "Genesis Collection").
    """
    match = re.match(r"/download/([^/]+)/(.*)", unquote(urlsplit(rom.url).path))
    if not match:
        return None
    path = match[2]
    marker = path.lower().find(".zip/") if rom.archive_member else -1
    folders = path[:marker + 4] if marker >= 0 else path.rpartition("/")[0]
    for text in (folders, match[1], rom.collection):
        text = re.sub(r"[^a-z0-9]+", " ", text.lower())
        for pattern, console in LIBRARY_KEYWORDS:
            if pattern.search(text):
                return console
    return None


def library_destination(root, rom):
    if rom.source == "BlueRoms":
        console = LIBRARY_PLATFORMS.get(rom.collection)
    else:
        console = (LIBRARY_EXTENSIONS.get(PurePosixPath(rom.name).suffix.lower())
                   or archive_console(rom))
    if console:
        try:
            base = root.resolve()
            found = [
                folder for folder in (root / name for name in LIBRARY_FOLDER_NAMES.get(console, (console,)))
                if folder.is_dir() and folder.resolve().is_relative_to(base)
            ]
        except OSError:
            found = []
        if len(found) == 1:
            return found[0]
    return root


@dataclass
class DownloadJob:
    rom: Rom
    destination: str
    choose_filename: bool
    status: str = "Queued"
    detail: str = ""
    saved_path: str = ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_theme(self)
        self.title("Spine ROM Browser")
        self.geometry("1000x670")
        self.minsize(690, 440)
        self.events = SimpleQueue()
        self.catalog = load_state("catalog.json", {}, clean_catalog)
        self.settings = load_state("settings.json", {}, clean_settings)
        self.index = []
        self.category_rows = {}
        self.rebuild_timer = None
        self.coverage = ""
        self.rows = {}
        self.jobs = load_state("downloads.json", [], clean_jobs)
        for job in self.jobs:
            if job.status in {"Queued", "Downloading", "Canceling"}:
                job.status = "Interrupted"
                job.detail = "The app closed before this download finished."
        self.active_job = None
        self.cancel_transfer = None
        self.download_window = None
        self.archive_cookie = ''
        self.search = tk.StringVar()
        self.category = tk.StringVar(value=ALL_PLATFORMS)
        self.status = tk.StringVar()
        self.download_status = tk.StringVar()
        bottom = ttk.Frame(self, padding=(26, 12))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        frame = ttk.Frame(self, padding=(26, 22, 26, 10))
        frame.pack(fill="both", expand=True)
        title = ttk.Frame(frame)
        title.pack(anchor="w")
        title_font = ("Segoe UI", 22, "bold")
        ttk.Label(title, text="Spine", style="Accent.TLabel", font=title_font).pack(side="left")
        ttk.Label(title, text=" ROM Browser", font=title_font).pack(side="left")
        ttk.Label(frame, text="Search by filename, region, or collection.",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 14))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        self.combo = ttk.Combobox(bar, textvariable=self.category, state="readonly", width=29)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", lambda _: self.select_category())
        ttk.Label(bar, text="Search", style="Muted.TLabel").pack(side="left", padx=(14, 7))
        entry = ttk.Entry(bar, textvariable=self.search)
        entry.pack(side="left", fill="x", expand=True)
        self.refresh = ttk.Button(bar, text="Refresh all", command=self.reload)
        self.refresh.pack(side="left", padx=(10, 0))
        account_bar = ttk.Frame(frame)
        account_bar.pack(fill="x", pady=(10, 0))
        self.account_button = ttk.Button(account_bar, text="Connect Internet Archive…",
                                         command=self.toggle_archive_account)
        self.account_button.pack(side="left")
        self.account_status = tk.StringVar(value="Internet Archive · Signed out")
        ttk.Label(account_bar, textvariable=self.account_status,
                  style="Muted.TLabel").pack(side="left", padx=10)
        self.library_label = tk.StringVar()
        self.update_library_label()
        library_bar = ttk.Frame(frame)
        library_bar.pack(fill="x", pady=(8, 0))
        ttk.Button(library_bar, text="ROM library…", command=self.choose_library).pack(side="left")
        ttk.Label(library_bar, textvariable=self.library_label,
                  style="Muted.TLabel").pack(side="left", padx=10)
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=(14, 7))
        self.tree = ttk.Treeview(table, columns=("file", "collection", "source", "size", "access"), show="headings", selectmode="browse")
        for col, label, width in [
            ("file", "File or title", 380), ("collection", "Collection", 195),
            ("source", "Source", 130),
            ("size", "Size", 90), ("access", "Access", 95),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=col in {"file", "collection"})
        self.tree.tag_configure("even", background=SURFACE)
        self.tree.tag_configure("odd", background="#2d3039")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.enable_download())
        self.search_timer = None
        self.search.trace_add("write", lambda *_: self.schedule_search())
        ttk.Label(frame, textvariable=self.status, style="Muted.TLabel").pack(anchor="w")
        self.sync_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.sync_status, style="Muted.TLabel",
                  wraplength=620).pack(anchor="w")
        self.download_button = ttk.Button(bottom, text="Queue download…",
                                          style="Primary.TButton", command=self.save)
        self.download_button.pack(side="right")
        self.archive_open_button = ttk.Button(bottom, text="Open on Archive.org",
                                             command=self.open_archive_item)
        self.archive_open_button.pack(side="right", padx=(8, 0))
        ttk.Button(bottom, text="Downloads…", command=self.show_downloads).pack(side="right", padx=8)
        ttk.Button(frame, text="Catalog details…", command=self.show_catalog).pack(anchor="w", pady=(5, 0))
        ttk.Label(bottom, textvariable=self.download_status, style="Muted.TLabel",
                  wraplength=380).pack(side="left", fill="x", expand=True)
        self.enable_download()
        self.event_timer = self.after(80, self.process_events)
        self.select_category()
        if STATE_NOTES:
            self.after(300, self.show_state_notes)
        if not self.catalog:
            self.reload()

    def show_state_notes(self):
        notes = "\n\n".join(STATE_NOTES)
        STATE_NOTES.clear()
        messagebox.showwarning(
            "Saved data", f"Spine started fresh where it could not use its saved data.\n\n{notes}",
            parent=self,
        )

    def report_callback_exception(self, exc, value, trace):
        log_problem("".join(traceback.format_exception(exc, value, trace)))
        try:
            self.sync_status.set(f"Something went wrong • details saved to {STATE_PATH / 'errors.log'}")
        except Exception:
            pass

    def destroy(self):
        if self.cancel_transfer:
            self.cancel_transfer.set()
        self.after_cancel(self.event_timer)
        if self.rebuild_timer is not None:
            self.after_cancel(self.rebuild_timer)
        if self.search_timer is not None:
            self.after_cancel(self.search_timer)
        super().destroy()

    def background(self, job, success, error, *, daemon=True):
        def run():
            try:
                result = job()
                self.events.put((success, result))
            except Exception as exc:
                if not isinstance(exc, (OSError, ValueError, DownloadCanceled)):
                    log_problem(traceback.format_exc())
                self.events.put((error, str(exc)))
        threading.Thread(target=run, daemon=daemon).start()

    def process_events(self):
        try:
            for _ in range(100):
                callback, value = self.events.get_nowait()
                callback(value)
        except Empty:
            pass
        finally:
            self.event_timer = self.after(80, self.process_events)

    def reload(self):
        """Refresh the chosen category, or everything when all platforms are shown."""
        only = self.category.get()
        only = None if only == ALL_PLATFORMS else only
        self.refresh.configure(state="disabled")
        self.sync_status.set(f"Refreshing {only}…" if only else "Reading the Megathread…")
        previous = self.catalog.copy()
        archive_cookie = self.archive_cookie

        def post(text):
            self.events.put((self.sync_status.set, text))

        def store(label, entry):
            entry["checked"] = datetime.now().astimezone().isoformat(timespec="minutes")
            previous[label] = entry
            self.events.put((self.update_catalog, (label, entry)))
            return bool(entry["errors"])

        def scan_megathread():
            urls = categories()
            if not urls:
                raise ValueError("No platform categories found.")
            if only and only not in urls:
                raise ValueError("The Megathread no longer lists this category.")
            wanted = {only: urls[only]} if only else urls
            failures = 0
            for number, (label, url) in enumerate(wanted.items(), 1):
                def progress(done, total):
                    post(f"{number}/{len(wanted)} • {label} • {done}/{total} Archive items read")

                try:
                    fresh, errors, expected = scan_category(url, progress)
                    if not expected:
                        raise ValueError("No supported Archive items found.")
                    cached = previous.get(label, {}).get("items", {})
                    items = {key: cached[key] for key in expected - fresh.keys() if key in cached}
                    items.update(fresh)
                    entry = {"items": items, "errors": errors, "read": len(fresh), "total": len(expected)}
                except (OSError, ValueError) as exc:
                    entry = previous.get(label, {}).copy()
                    entry.update(errors=[str(exc)], read=0, total=0)
                failures += store(label, entry)
            return set(urls), failures

        def scan_zip():
            post("Reading Internet Archive Game Boy ZIP…")
            try:
                archive, = listed_files(ZIP_ITEM, ZIP_SCOPE)
                members = archive_members(archive, archive_cookie)
                entry = {"items": {ZIP_ITEM: members}, "errors": [], "read": 1, "total": 1}
            except (OSError, ValueError) as exc:
                detail = str(exc)
                if isinstance(exc, HTTPError) and exc.code in (401, 403) and not archive_cookie:
                    detail = f"Internet Archive returned HTTP {exc.code}. Connect your account and refresh to try the ZIP listing."
                entry = previous.get(ZIP_CATEGORY, {}).copy()
                entry.update(errors=[detail], read=0, total=1)
            return store(ZIP_CATEGORY, entry)

        def scan_blue():
            platforms = blue_platforms()
            listed = {platform + BLUE_SUFFIX for platform in platforms}
            if only:
                if only not in listed:
                    raise ValueError("BlueRoms no longer lists this platform.")
                platform = only[:-len(BLUE_SUFFIX)]
                platforms = {platform: platforms[platform]}
            failures = 0
            for number, (platform, url) in enumerate(platforms.items(), 1):
                label = platform + BLUE_SUFFIX

                def progress(pages, titles):
                    post(f"BlueRoms {number}/{len(platforms)} • {platform} • {pages} pages • {titles} titles")

                try:
                    items = blue_catalog(platform, url, progress)
                    entry = {"items": items, "errors": [], "read": 1, "total": 1}
                except (OSError, ValueError) as exc:
                    entry = previous.get(label, {}).copy()
                    entry.update(errors=[str(exc)], read=0, total=1)
                failures += store(label, entry)
            return listed, failures

        def scan():
            metadata.cache_clear()
            if only:
                try:
                    if only == ZIP_CATEGORY:
                        failures = scan_zip()
                    elif only.endswith(BLUE_SUFFIX):
                        failures = scan_blue()[1]
                    else:
                        failures = scan_megathread()[1]
                except (OSError, ValueError) as exc:
                    return f"Could not refresh {only} • {exc}"
                save_state("catalog.json", previous)
                gaps = " • some listings could not be read (see Catalog details)" if failures else ""
                return f"{only} refreshed{gaps}"

            issues = ""
            failures = 0
            megathread = blue = None
            try:
                megathread, failures = scan_megathread()
            except (OSError, ValueError) as exc:
                issues += f" • Megathread refresh failed: {exc}"
            save_state("catalog.json", previous)
            failures += scan_zip()
            save_state("catalog.json", previous)
            try:
                blue, blue_failures = scan_blue()
                failures += blue_failures
            except (OSError, ValueError) as exc:
                issues += f" • BlueRoms discovery failed: {exc}"
            # Drop categories a source no longer lists, but only if that source answered.
            for label in list(previous):
                if label == ZIP_CATEGORY:
                    continue
                listed = blue if label.endswith(BLUE_SUFFIX) else megathread
                if listed is not None and label not in listed:
                    del previous[label]
                    self.events.put((self.update_catalog, (label, None)))
            save_state("catalog.json", previous)
            return f"Refresh complete • {len(previous)} categories checked • {failures} with gaps{issues}"

        def finish(status):
            self.refresh.configure(state="normal")
            self.sync_status.set(status)
            if self.rebuild_timer is not None:
                self.rebuild_index(keep_view=True)

        self.background(scan, finish, lambda error: finish(f"Refresh failed • {error}"))

    def update_catalog(self, update):
        """Take in one refreshed category without rebuilding the whole list."""
        label, entry = update
        self.category_rows.pop(label, None)
        if entry is None:
            self.catalog.pop(label, None)
        else:
            self.catalog[label] = entry
        selected = self.category.get()
        self.list_categories()
        if selected == label or self.category.get() != selected:
            self.rebuild_index(keep_view=True)
        elif selected == ALL_PLATFORMS and self.rebuild_timer is None:
            # Many categories arrive in a row during a refresh: update the
            # combined list every few seconds instead of once per category.
            self.rebuild_timer = self.after(4000, lambda: self.rebuild_index(keep_view=True))

    def list_categories(self):
        values = [ALL_PLATFORMS, *sorted(self.catalog)]
        self.combo.configure(values=values)
        if self.category.get() not in values:
            self.category.set(ALL_PLATFORMS)

    def select_category(self):
        self.list_categories()
        self.rebuild_index()

    def rows_for(self, label):
        """One category's files, sorted and ready to search. Kept until it changes."""
        rows = self.category_rows.get(label)
        if rows is None:
            unique = {}
            for files in self.catalog[label].get("items", {}).values():
                unique.update(files)
            rows = []
            for entry in unique.values():
                rom = rom_from(entry)
                if rom is not None:
                    rows.append((rom.name.casefold(), rom.url, rom,
                                 f"{rom.name} {rom.collection} {rom.source}".casefold()))
            rows.sort(key=itemgetter(0, 1))
            self.category_rows[label] = rows
        return rows

    def rebuild_index(self, keep_view=False):
        if self.rebuild_timer is not None:
            self.after_cancel(self.rebuild_timer)
            self.rebuild_timer = None
        selected = self.category.get()
        self.refresh.configure(text="Refresh all" if selected == ALL_PLATFORMS else "Refresh this category")
        if selected == ALL_PLATFORMS:
            merged = sorted(chain.from_iterable(self.rows_for(label) for label in self.catalog),
                            key=itemgetter(0, 1))
            self.index = []
            previous = None
            for row in merged:
                if row[1] != previous:
                    self.index.append(row)
                    previous = row[1]
            self.coverage = f"{len(self.catalog)} categories checked"
        else:
            self.index = self.rows_for(selected)
            entry = self.catalog[selected]
            self.coverage = f"{entry['read']}/{entry['total']} listings read on last refresh"
        self.render(keep_view)

    def show_catalog(self):
        window = tk.Toplevel(self)
        window.configure(background=BACKGROUND)
        window.title("Catalog details")
        window.geometry("780x430")
        details = tk.Text(window, wrap="word", padx=16, pady=14, relief="flat",
                          background=SURFACE, foreground=TEXT, font=("Segoe UI", 10),
                          selectbackground=AMBER, selectforeground=BACKGROUND)
        details.pack(fill="both", expand=True)
        if not self.catalog:
            details.insert("end", "No categories have been checked yet.")
        for label, entry in sorted(self.catalog.items()):
            try:
                checked = datetime.fromisoformat(entry["checked"]).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                checked = "not checked yet"
            details.insert("end", f"{label} • {checked} • {entry['read']}/{entry['total']} items read\n")
            for error in entry["errors"]:
                details.insert("end", f"  {error}\n")
            if entry["errors"] and entry.get("items"):
                details.insert("end", "  Search includes available cached listings.\n")
            details.insert("end", "\n")
        details.configure(state="disabled")

    def schedule_search(self):
        """Search shortly after typing pauses, not on every key."""
        if self.search_timer is not None:
            self.after_cancel(self.search_timer)
        self.search_timer = self.after(200, self.render)

    def render(self, keep_view=False):
        """Fill the results list. keep_view keeps the selection and scroll position."""
        if self.search_timer is not None:
            self.after_cancel(self.search_timer)
            self.search_timer = None
        chosen = self.selected() if keep_view else None
        top = self.tree.yview()[0] if keep_view else 0
        self.tree.delete(*self.tree.get_children())
        terms = self.search.get().casefold().split()
        self.rows.clear()
        count = 0
        reselect = None
        for _, _, rom, searchable in self.index:
            if not all(term in searchable for term in terms):
                continue
            count += 1
            if count <= 500:
                size = f"{rom.size / 1048576:,.1f} MB" if rom.size else "Unknown"
                row = self.tree.insert("", "end", tags=("odd" if count % 2 else "even",), values=(
                    rom.name, rom.collection, rom.source, size,
                    ("ZIP member" if rom.archive_member else "Check access") if rom.locked and self.archive_cookie
                    else "Sign in" if rom.locked else "ZIP member" if rom.archive_member
                    else "Check link" if rom.source == "BlueRoms" else "Public"
                ))
                self.rows[row] = rom
                if chosen is not None and rom.url == chosen.url:
                    reselect = row
        if keep_view:
            if reselect is not None:
                self.tree.selection_set(reselect)
            self.tree.yview_moveto(top)
        if self.combo.cget("state") == "readonly":
            caption = f"{count:,} matches • {self.coverage}"
            if count > 500:
                caption += " • first 500 shown"
            self.status.set(caption)
        self.enable_download()

    def selected(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def toggle_archive_account(self):
        if self.archive_cookie:
            self.archive_cookie = ""
            self.account_button.configure(text="Connect Internet Archive…")
            self.account_status.set("Internet Archive · Signed out")
            self.render()
            return
        email = simpledialog.askstring(
            "Connect Internet Archive", "Internet Archive account email:", parent=self,
        )
        if not email or not email.strip():
            return
        password = simpledialog.askstring(
            "Connect Internet Archive", "Internet Archive password:", show="*", parent=self,
        )
        if not password:
            return
        self.account_status.set("Internet Archive · Signing in…")
        self.account_button.configure(state="disabled")

        def ready(cookie):
            self.archive_cookie = cookie
            self.account_button.configure(text="Disconnect Internet Archive", state="normal")
            self.account_status.set("Internet Archive · Signed in for this session")
            self.render()

        def failed(error):
            self.account_button.configure(state="normal")
            self.account_status.set("Internet Archive · Sign-in failed")
            messagebox.showerror("Internet Archive sign-in", error, parent=self)

        self.background(lambda: archive_sign_in(email.strip(), password), ready, failed)

    def open_archive_item(self):
        rom = self.selected()
        if rom and rom.source == "Internet Archive" and trusted(rom.url, "archive"):
            match = re.match(r"/download/([a-zA-Z0-9_.-]+)/", urlsplit(rom.url).path)
            if match:
                webbrowser.open(f"{ARCHIVE}/details/{match[1]}")

    def enable_download(self):
        rom = self.selected()
        self.archive_open_button.configure(
            state="normal" if rom and rom.source == "Internet Archive" else "disabled",
        )
        self.download_button.configure(state="normal" if rom and (not rom.locked or self.archive_cookie) else "disabled")

    def save(self):
        rom = self.selected()
        if not rom or (rom.locked and not self.archive_cookie):
            return
        if rom.source == "BlueRoms":
            folder = filedialog.askdirectory(
                parent=self, title="Choose where to save the ROM",
                initialdir=self.download_folder(rom), mustexist=True,
            )
            if not folder:
                return
            self.start_download(rom, Path(folder), choose_filename=True)
        else:
            chosen = filedialog.asksaveasfilename(
                parent=self, title="Save ROM", initialfile=self.filename(rom),
                confirmoverwrite=False, initialdir=self.download_folder(rom),
            )
            if chosen:
                self.start_download(rom, Path(chosen))

    def update_library_label(self):
        root = self.settings.get("library_root")
        self.library_label.set(f"ROM library · {root}" if root else "ROM library · Choose a folder to enable")

    def choose_library(self):
        root = self.settings.get("library_root")
        if root:
            choice = messagebox.askyesnocancel(
                "ROM library", "Choose Yes to change the library or No to disconnect it.",
                parent=self,
            )
            if choice is None:
                return
            if not choice:
                self.settings.pop("library_root", None)
                self.save_library()
                return
        suggested = Path("D:/Roms")
        initial = root if root and Path(root).is_dir() else (
            str(suggested) if suggested.is_dir() else self.download_folder()
        )
        chosen = filedialog.askdirectory(
            parent=self, title="Choose the ROM library root", initialdir=initial, mustexist=True,
        )
        if chosen:
            self.settings["library_root"] = chosen
            self.save_library()

    def save_library(self):
        self.update_library_label()
        try:
            save_state("settings.json", self.settings)
        except OSError as exc:
            self.sync_status.set(f"Could not save ROM library setting • {exc}")

    def download_folder(self, rom=None):
        root = self.settings.get("library_root")
        if root and Path(root).is_dir():
            return str(library_destination(Path(root), rom)) if rom else root
        folder = self.settings.get("download_folder")
        return folder if folder and Path(folder).is_dir() else str(Path.home())

    @staticmethod
    def filename(rom):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PurePosixPath(rom.name).name).rstrip(". ")
        stem = name.partition(".")[0].rstrip().upper()
        reserved = re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³]", stem)
        if reserved:
            name = "_" + name
        return name

    def start_download(self, rom, destination, choose_filename=False):
        folder = destination if choose_filename else destination.parent
        if any(job.rom.url == rom.url and job.status in {"Queued", "Downloading", "Canceling"}
               for job in self.jobs):
            messagebox.showinfo("Already queued", f"{rom.name} is already in the download queue.", parent=self)
            return
        if not choose_filename and destination.exists():
            messagebox.showerror(
                "File already exists",
                f"{destination} already exists. Spine does not replace files. "
                "Choose another name or folder.", parent=self,
            )
            return
        free = free_space(folder)
        if rom.size and free is not None and free < rom.size:
            messagebox.showerror(
                "Not enough space",
                f"{rom.name} needs {rom.size / 1073741824:,.2f} GB, but that drive has "
                f"{free / 1073741824:,.2f} GB free.", parent=self,
            )
            return
        unverified = "" if rom.sha1 else (
            "\n\nThis source publishes no checksum for this file, so Spine cannot confirm "
            "it is the file that was listed."
        )
        if not messagebox.askokcancel(
            "Confirm download",
            f"Download {rom.name} to {destination}?{unverified}"
            "\n\nContinue if you have permission to download this file.",
            parent=self,
        ):
            self.download_status.set("Download canceled")
            return
        self.settings["download_folder"] = str(folder)
        try:
            save_state("settings.json", self.settings)
        except OSError as exc:
            self.sync_status.set(f"Could not remember the download folder • {exc}")
        self.jobs.append(DownloadJob(rom, str(destination), choose_filename))
        self.update_downloads()
        self.run_queue()

    def update_downloads(self, persist=True):
        if persist:
            try:
                retained = [job for job in self.jobs if job.status not in {"Queued", "Downloading", "Canceling"}][-100:]
                pending = [job for job in self.jobs if job.status in {"Queued", "Downloading", "Canceling"}]
                save_state("downloads.json", [asdict(job) for job in pending + retained])
            except OSError as exc:
                self.sync_status.set(f"Could not save download history • {exc}")
        if self.download_window is None or not self.download_window.winfo_exists():
            return
        tree = self.download_tree
        selected = tree.selection()
        for row in tree.get_children():
            tree.delete(row)
        for index in range(len(self.jobs) - 1, -1, -1):
            job = self.jobs[index]
            tree.insert("", "end", iid=str(index), values=(job.rom.name, job.status, job.rom.source))
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])
        self.update_download_details()

    def show_downloads(self):
        if self.download_window is not None and self.download_window.winfo_exists():
            self.download_window.lift()
            return
        window = self.download_window = tk.Toplevel(self)
        window.title("Downloads")
        window.geometry("780x390")
        window.minsize(520, 285)
        window.configure(background=BACKGROUND)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        tree = self.download_tree = ttk.Treeview(frame, columns=("file", "status", "source"),
                                                 show="headings", selectmode="browse")
        for column, label, width in (("file", "File or title", 430), ("status", "Status", 115), ("source", "Source", 125)):
            tree.heading(column, text=label)
            tree.column(column, width=width, stretch=column == "file")
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        tree.bind("<<TreeviewSelect>>", lambda _: self.update_download_details())
        self.job_detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.job_detail, style="Muted.TLabel",
                  wraplength=465).pack(anchor="w", pady=8)
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.cancel_button = ttk.Button(controls, text="Cancel selected", command=self.cancel_job)
        self.cancel_button.pack(side="left")
        self.retry_button = ttk.Button(controls, text="Retry selected", command=self.retry_job)
        self.retry_button.pack(side="left", padx=8)
        self.update_downloads(persist=False)

    def selected_job(self):
        selected = self.download_tree.selection()
        return self.jobs[int(selected[0])] if selected else None

    def update_download_details(self):
        job = self.selected_job()
        self.job_detail.set((job.detail or job.destination) if job else "Select a download to see its details.")
        self.cancel_button.configure(state="normal" if job and job.status in {"Queued", "Downloading"} else "disabled")
        self.retry_button.configure(state="normal" if job and job.status in {"Failed", "Canceled", "Interrupted"} else "disabled")

    def cancel_job(self):
        job = self.selected_job()
        if job and job.status == "Queued":
            job.status, job.detail = "Canceled", "Canceled before the transfer began."
        elif job is not None and job is self.active_job and job.status == "Downloading":
            self.cancel_transfer.set()
            job.status, job.detail = "Canceling", "Waiting for the current network read to finish."
            self.download_status.set(job.detail)
        self.update_downloads()

    def retry_job(self):
        job = self.selected_job()
        if job and job.status in {"Failed", "Canceled", "Interrupted"}:
            if job.rom.locked and not self.archive_cookie:
                messagebox.showinfo("Internet Archive", "Connect Internet Archive before retrying this file.",
                                    parent=self.download_window)
                return
            job.status, job.detail = "Queued", ""
            self.update_downloads()
            self.run_queue()

    def run_queue(self):
        if self.active_job is not None:
            return
        job = next((item for item in self.jobs if item.status == "Queued"), None)
        if job is None:
            return
        rom, destination, choose_filename = job.rom, Path(job.destination), job.choose_filename
        folder = destination if choose_filename else destination.parent
        self.active_job = job
        cancel = self.cancel_transfer = threading.Event()
        job.status = "Downloading"
        job.detail = "Looking up BlueRoms file…" if choose_filename else "Connecting to Internet Archive…"
        self.download_status.set(job.detail)
        self.update_downloads()
        archive_cookie = self.archive_cookie if rom.source == "Internet Archive" else ""
        last_update = float("-inf")

        def progress(text):
            if job.status == "Downloading":
                job.detail = text
                self.download_status.set(text)
                if self.download_window is not None and self.download_window.winfo_exists():
                    self.update_download_details()

        def report(written):
            nonlocal last_update
            now = monotonic()
            if now - last_update >= 0.25:
                last_update = now
                self.events.put((progress, f"{rom.name}: {written / 1048576:,.2f} MiB received"))

        def finish(status, detail, saved_path=""):
            job.status, job.detail, job.saved_path = status, detail, saved_path
            self.active_job = self.cancel_transfer = None
            self.download_status.set(f"{status} • {rom.name}")
            self.update_downloads()
            self.run_queue()

        def saved(result):
            status, detail, saved_path = result
            finish(status, detail, saved_path)

        def failed(error):
            if cancel.is_set():
                finish("Canceled", "Canceled by user.")
            else:
                finish("Failed", f"{error}\nDestination folder: {folder}")

        def transfer():
            check_cancel(cancel)
            if rom.locked and not archive_cookie:
                raise ValueError("Connect Internet Archive and retry this file.")
            file = rom
            target = destination
            if choose_filename:
                try:
                    file = resolve_blue(rom)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"BlueRoms link lookup failed: {exc}") from exc
                target = folder / self.filename(file)
                self.events.put((progress, f"Connecting to save {target.name}…"))
            try:
                download(file, target, report, archive_cookie, cancel,
                         lambda text: self.events.put((progress, text)))
            except HTTPError as exc:
                if archive_cookie and exc.code in (401, 403):
                    raise ValueError(
                        f"Internet Archive denied access (HTTP {exc.code}). "
                        "Reconnect if the session expired. This item may still be restricted."
                    ) from exc
                raise ValueError(f"Could not download {target.name}: HTTP {exc.code}") from exc
            except (OSError, ValueError) as exc:
                raise ValueError(f"Could not save {target.name}: {exc}") from exc
            checksum = "SHA-1 matched" if file.sha1 else "not verified (the source publishes no checksum)"
            return "Completed", f"Saved to {target} • {checksum}", str(target)

        self.background(transfer, saved, failed, daemon=False)


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        # A .pyw has no console, so without this a start-up crash shows nothing at all.
        log_problem(traceback.format_exc())
        try:
            messagebox.showerror(
                "Spine ROM Browser",
                f"Spine ROM Browser could not start.\n\nDetails were saved to:\n{STATE_PATH / 'errors.log'}",
            )
        except Exception:
            pass

