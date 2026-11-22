"""Search Internet Archive collections linked by the r/ROMs Megathread."""
import hashlib
import json
import os
import re
import tempfile
import threading
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
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


def request(url, service, archive_cookie=''):
    if not trusted(url, service):
        raise ValueError("URL is outside the approved hosts.")
    cookie = archive_cookie if service == "archive" else ""
    opener = build_opener(Redirects(service, cookie))
    req = Request(url, headers={"User-Agent": "Megathread ROM Browser/0.1"})
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


def download(rom, destination, progress, archive_cookie=''):
    written = 0
    digest = hashlib.sha1()
    service = "blue-file" if rom.source == "BlueRoms" else "archive"
    response = request(rom.url, service, archive_cookie) if archive_cookie else request(rom.url, service)
    with response as source:
        if source.headers.get_content_type() in {"text/html", "application/json", "application/xhtml+xml"}:
            raise ValueError("The host returned a page instead of a file.")
        content_length = source.headers.get("Content-Length")
        expected = rom.size or (int(content_length) if content_length and content_length.isdecimal() else 0)
        if destination.exists():
            raise FileExistsError(f"A file already exists at {destination}")
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
            partial = Path(output.name)
            try:
                while chunk := source.read(256 * 1024):
                    written += len(chunk)
                    if expected and written > expected:
                        raise ValueError("The download exceeds the listed file size.")
                    digest.update(chunk)
                    output.write(chunk)
                    progress(written)
                if not written or (expected and written != expected):
                    raise ValueError("The download is incomplete.")
                if rom.sha1 and digest.hexdigest() != rom.sha1:
                    raise ValueError("The file did not match the published SHA-1.")
                output.close()
                os.rename(partial, destination)
            finally:
                output.close()
                partial.unlink(missing_ok=True)


def load_state(filename, default):
    try:
        return json.loads((STATE_PATH / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_theme(self)
        self.title("Spine ROM Browser")
        self.geometry("1000x670")
        self.minsize(690, 440)
        self.events = SimpleQueue()
        self.catalog = load_state("catalog.json", {})
        self.settings = load_state("settings.json", {})
        self.index = []
        self.coverage = ""
        self.rows = {}
        self.busy = False
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
        self.refresh = ttk.Button(bar, text="Refresh catalog", command=self.reload)
        self.refresh.pack(side="left", padx=(10, 0))
        account_bar = ttk.Frame(frame)
        account_bar.pack(fill="x", pady=(10, 0))
        self.account_button = ttk.Button(account_bar, text="Connect Internet Archive…",
                                         command=self.toggle_archive_account)
        self.account_button.pack(side="left")
        self.account_status = tk.StringVar(value="Internet Archive · Signed out")
        ttk.Label(account_bar, textvariable=self.account_status,
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
        self.search.trace_add("write", lambda *_: self.render())
        ttk.Label(frame, textvariable=self.status, style="Muted.TLabel").pack(anchor="w")
        self.sync_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.sync_status, style="Muted.TLabel",
                  wraplength=620).pack(anchor="w")
        self.download_button = ttk.Button(bottom, text="Download selected…",
                                          style="Primary.TButton", command=self.save)
        self.download_button.pack(side="right")
        self.archive_open_button = ttk.Button(bottom, text="Open on Archive.org",
                                             command=self.open_archive_item)
        self.archive_open_button.pack(side="right", padx=(8, 0))
        ttk.Button(bottom, text="Catalog details…", command=self.show_catalog).pack(side="right", padx=8)
        ttk.Label(bottom, textvariable=self.download_status, style="Muted.TLabel",
                  wraplength=380).pack(side="left", fill="x", expand=True)
        self.enable_download()
        self.event_timer = self.after(80, self.process_events)
        self.select_category()
        if not self.catalog:
            self.reload()

    def destroy(self):
        self.after_cancel(self.event_timer)
        super().destroy()

    def background(self, job, success, error):
        def run():
            try:
                result = job()
                self.events.put((success, result))
            except Exception as exc:
                self.events.put((error, str(exc)))
        threading.Thread(target=run, daemon=True).start()

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
        self.refresh.configure(state="disabled")
        self.sync_status.set("Reading the Megathread…")
        previous = self.catalog.copy()

        def scan():
            failures = 0
            blue_labels = {label for label in previous if label.endswith(BLUE_SUFFIX)}
            blue_issue = ""
            try:
                platforms = blue_platforms()
            except (OSError, ValueError) as exc:
                platforms = {}
                blue_issue = f" • BlueRoms discovery failed: {exc}"
            for number, (platform, url) in enumerate(platforms.items(), 1):
                label = platform + BLUE_SUFFIX
                blue_labels.add(label)

                def blue_progress(pages, titles):
                    caption = f"BlueRoms {number}/{len(platforms)} • {platform} • {pages} pages • {titles} titles"
                    self.events.put((self.sync_status.set, caption))

                try:
                    items = blue_catalog(platform, url, blue_progress)
                    blue = {"items": items, "errors": [], "read": 1, "total": 1}
                except (OSError, ValueError) as exc:
                    blue = previous.get(label, {}).copy()
                    blue.update(errors=[str(exc)], read=0, total=1)
                    failures += 1
                blue["checked"] = datetime.now().astimezone().isoformat(timespec="minutes")
                previous[label] = blue
                self.events.put((self.update_catalog, (label, blue)))
            if platforms:
                save_state("catalog.json", previous)

            try:
                urls = categories()
                if not urls:
                    raise ValueError("No platform categories found.")
            except (OSError, ValueError) as exc:
                save_state("catalog.json", previous)
                return f"Megathread refresh failed • {exc}{blue_issue}"

            metadata.cache_clear()
            for number, (label, url) in enumerate(urls.items(), 1):
                def progress(done, total):
                    caption = f"{number}/{len(urls)} • {label} • {done}/{total} Archive items read"
                    self.events.put((self.sync_status.set, caption))

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
                failures += bool(entry["errors"])
                entry["checked"] = datetime.now().astimezone().isoformat(timespec="minutes")
                previous[label] = entry
                self.events.put((self.update_catalog, (label, entry)))
            current = urls.keys() | blue_labels
            for label in previous.keys() - current:
                self.events.put((self.update_catalog, (label, None)))
            save_state("catalog.json", {label: previous[label] for label in current})
            return f"Refresh complete • {len(current)} categories checked • {failures} with gaps{blue_issue}"

        def finish(status):
            self.refresh.configure(state="normal")
            self.sync_status.set(status)

        self.background(scan, finish, lambda error: finish(f"Refresh failed • {error}"))

    def update_catalog(self, update):
        label, entry = update
        if entry is None:
            self.catalog.pop(label, None)
        else:
            self.catalog[label] = entry
        self.select_category()

    def select_category(self):
        values = [ALL_PLATFORMS, *sorted(self.catalog)]
        self.combo.configure(values=values)
        if self.category.get() not in values:
            self.category.set(ALL_PLATFORMS)
        selected = self.category.get()
        entries = self.catalog.values() if selected == ALL_PLATFORMS else [self.catalog[selected]]
        unique = {}
        for entry in entries:
            for files in entry.get("items", {}).values():
                unique.update(files)
        self.index = sorted(
            (Rom(**entry) for entry in unique.values()),
            key=lambda rom: (rom.name.casefold(), rom.url),
        )
        self.coverage = f"{len(self.catalog)} categories checked" if selected == ALL_PLATFORMS else (
            f"{self.catalog[selected]['read']}/{self.catalog[selected]['total']} listings read on last refresh"
        )
        self.render()

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
            checked = datetime.fromisoformat(entry["checked"]).strftime("%Y-%m-%d %H:%M")
            details.insert("end", f"{label} • {checked} • {entry['read']}/{entry['total']} items read\n")
            for error in entry["errors"]:
                details.insert("end", f"  {error}\n")
            if entry["errors"] and entry.get("items"):
                details.insert("end", "  Search includes available cached listings.\n")
            details.insert("end", "\n")
        details.configure(state="disabled")

    def render(self):
        self.tree.delete(*self.tree.get_children())
        terms = self.search.get().casefold().split()
        self.rows.clear()
        count = 0
        for rom in self.index:
            searchable = f"{rom.name} {rom.collection} {rom.source}".casefold()
            if not all(term in searchable for term in terms):
                continue
            count += 1
            if count <= 500:
                size = f"{rom.size / 1048576:,.1f} MB" if rom.size else "Unknown"
                row = self.tree.insert("", "end", tags=("odd" if count % 2 else "even",), values=(
                    rom.name, rom.collection, rom.source, size,
                    ("Check access" if self.archive_cookie else "Sign in")
                    if rom.locked else ("Check link" if rom.source == "BlueRoms" else "Public")
                ))
                self.rows[row] = rom
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
        self.download_button.configure(state="normal" if rom and (not rom.locked or self.archive_cookie) and not self.busy else "disabled")

    def save(self):
        rom = self.selected()
        if not rom or (rom.locked and not self.archive_cookie) or self.busy:
            return
        if rom.source == "BlueRoms":
            folder = filedialog.askdirectory(
                parent=self, title="Choose where to save the ROM",
                initialdir=self.download_folder(), mustexist=True,
            )
            if not folder:
                return
            self.start_download(rom, Path(folder), choose_filename=True)
        else:
            chosen = filedialog.asksaveasfilename(
                parent=self, title="Save ROM", initialfile=self.filename(rom),
                confirmoverwrite=False, initialdir=self.download_folder(),
            )
            if chosen:
                self.start_download(rom, Path(chosen))

    def download_folder(self):
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
        if not messagebox.askokcancel(
            "Confirm download",
            f"Download {rom.name} to {destination}?\n\nContinue if you have permission to download this file.",
            parent=self,
        ):
            self.download_status.set("Download canceled")
            return
        self.settings["download_folder"] = str(folder)
        try:
            save_state("settings.json", self.settings)
        except OSError as exc:
            self.sync_status.set(f"Could not remember the download folder • {exc}")
        self.busy = True
        self.enable_download()
        status = "Looking up BlueRoms file…" if choose_filename else "Connecting to Internet Archive…"
        self.download_status.set(status)
        archive_cookie = self.archive_cookie if rom.source == "Internet Archive" else ""
        last_update = float("-inf")

        def report(written):
            nonlocal last_update
            now = monotonic()
            if now - last_update >= 0.25:
                last_update = now
                self.events.put((
                    self.download_status.set, f"Received {written / 1048576:,.2f} MiB…",
                ))

        def finish(status):
            self.busy = False
            self.download_status.set(status)
            self.enable_download()

        def saved(status):
            finish("Download complete")
            messagebox.showinfo("Download complete", status, parent=self)

        def failed(error):
            finish("Download failed")
            messagebox.showerror(
                "Download failed",
                f"{error}\n\nDestination folder: {folder}",
                parent=self,
            )

        def transfer():
            file = rom
            target = destination
            if choose_filename:
                try:
                    file = resolve_blue(rom)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"BlueRoms link lookup failed: {exc}") from exc
                target = folder / self.filename(file)
                self.events.put((self.download_status.set, f"Connecting to save {target.name}…"))
            try:
                download(file, target, report, archive_cookie)
            except HTTPError as exc:
                if archive_cookie and exc.code in (401, 403):
                    raise ValueError(
                        f"Internet Archive denied access (HTTP {exc.code}). "
                        "Reconnect if the session expired. This item may still be restricted."
                    ) from exc
                raise ValueError(f"Could not download {target.name}: HTTP {exc.code}") from exc
            except (OSError, ValueError) as exc:
                raise ValueError(f"Could not save {target.name}: {exc}") from exc
            checksum = "SHA-1 matched" if file.sha1 else "no checksum provided"
            return f"Saved to {target} • {checksum}"

        self.background(transfer, saved, failed)


if __name__ == "__main__":
    App().mainloop()

