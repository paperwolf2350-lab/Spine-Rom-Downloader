"""Search Internet Archive collections linked by the r/ROMs Megathread."""
import hashlib
import json
import os
import re
import tempfile
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = "https://r-roms.gitlab.io/"
ALL_PLATFORMS = "All indexed platforms"
STATE_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Megathread ROM Browser"
ARCHIVE = "https://archive.org"
EXTENSIONS = set(
    ".zip .7z .rar .iso .chd .rvz .wbfs .wua .cia .3ds .nds .gba .gbc .gb "
    ".nes .sfc .smc .n64 .z64 .v64 .bin .cue .gdi .cso .pbp .pce .a26 .a52 "
    ".a78 .sms .gg .md .gen .smd .32x .j64 .jag .xiso .wad .nsp .xci".split()
)


def trusted(url, service):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.netloc.lower() != host:
        return False
    if service == "guide":
        return host == "r-roms.gitlab.io"
    return host == "archive.org" or bool(re.fullmatch(
        r"ia[0-9]+\.(?:[a-z0-9-]+\.)?archive\.org", host
    ))


class Redirects(HTTPRedirectHandler):
    def __init__(self, service):
        self.service = service

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted(newurl, self.service):
            raise ValueError("The server redirected outside the approved hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url, service):
    if not trusted(url, service):
        raise ValueError("URL is outside the approved hosts.")
    opener = build_opener(Redirects(service))
    return opener.open(Request(url, headers={"User-Agent": "Megathread ROM Browser/0.1"}), timeout=20)


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


def download(rom, destination, progress):
    written = 0
    digest = hashlib.sha1()
    with request(rom.url, "archive") as source:
        if source.headers.get_content_type() in {"text/html", "application/json", "application/xhtml+xml"}:
            raise ValueError("The host returned a page instead of a file.")
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
            partial = Path(output.name)
            try:
                while chunk := source.read(256 * 1024):
                    written += len(chunk)
                    if rom.size and written > rom.size:
                        raise ValueError("The download exceeds the listed file size.")
                    digest.update(chunk)
                    output.write(chunk)
                    progress(written)
                if not written or (rom.size and written != rom.size):
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Megathread ROM Browser")
        self.geometry("1000x610")
        self.minsize(670, 400)
        self.events = SimpleQueue()
        self.catalog = load_state("catalog.json", {})
        self.settings = load_state("settings.json", {})
        self.index = []
        self.coverage = ""
        self.rows = {}
        self.busy = False
        self.search = tk.StringVar()
        self.category = tk.StringVar(value=ALL_PLATFORMS)
        self.status = tk.StringVar()
        self.download_status = tk.StringVar()
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Megathread ROM Browser", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=12)
        self.combo = ttk.Combobox(bar, textvariable=self.category, state="readonly", width=24)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", lambda _: self.select_category())
        self.refresh = ttk.Button(bar, text="Refresh catalog", command=self.reload)
        self.refresh.pack(side="left", padx=6)
        entry = ttk.Entry(bar, textvariable=self.search)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Label(frame, text="Search by filename, region, or collection").pack(anchor="w")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=("file", "collection", "size", "access"), show="headings", selectmode="browse")
        for col, label, width in [
            ("file", "File", 430), ("collection", "Internet Archive collection", 280),
            ("size", "Size", 90), ("access", "Access", 95),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=col in {"file", "collection"})
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.enable_download())
        self.search.trace_add("write", lambda *_: self.render())
        ttk.Label(frame, textvariable=self.status).pack(anchor="w")
        self.sync_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.sync_status).pack(anchor="w")
        ttk.Button(frame, text="Catalog details…", command=self.show_catalog).pack(anchor="w", pady=(4, 0))
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(10, 0))
        self.download_button = ttk.Button(bottom, text="Download selected…", command=self.save)
        self.download_button.pack(side="left")
        ttk.Label(bottom, textvariable=self.download_status).pack(side="left", padx=10)
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
            urls = categories()
            if not urls:
                raise ValueError("No platform categories found.")
            metadata.cache_clear()
            failures = 0
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
            for label in previous.keys() - urls.keys():
                self.events.put((self.update_catalog, (label, None)))
            save_state("catalog.json", {label: previous[label] for label in urls})
            return f"Refresh complete • {len(urls)} categories checked • {failures} with gaps"

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
            f"{self.catalog[selected]['read']}/{self.catalog[selected]['total']} Archive items read on last refresh"
        )
        self.render()

    def show_catalog(self):
        window = tk.Toplevel(self)
        window.title("Catalog details")
        window.geometry("780x430")
        details = tk.Text(window, wrap="word", padx=10, pady=10)
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
            searchable = f"{rom.name} {rom.collection}".casefold()
            if not all(term in searchable for term in terms):
                continue
            count += 1
            if count <= 500:
                size = f"{rom.size / 1048576:,.1f} MB" if rom.size else "Unknown"
                row = self.tree.insert("", "end", values=(
                    rom.name, rom.collection, size, "Locked" if rom.locked else "Public"
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

    def enable_download(self):
        rom = self.selected()
        self.download_button.configure(state="normal" if rom and not rom.locked and not self.busy else "disabled")

    def save(self):
        rom = self.selected()
        if not rom or rom.locked or self.busy:
            return
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PurePosixPath(rom.name).name).rstrip(". ")
        if PureWindowsPath(name).is_reserved():
            name = "_" + name
        folder = self.settings.get("download_folder")
        chosen = filedialog.asksaveasfilename(
            title="Save ROM", initialfile=name, confirmoverwrite=False,
            initialdir=folder if folder and Path(folder).is_dir() else None,
        )
        if not chosen:
            return
        target = Path(chosen)
        if target.exists():
            messagebox.showerror("File already exists", "Choose a new filename to keep the existing file.")
            return
        if not messagebox.askokcancel("Confirm download", f"Download {rom.name}? Continue if you have permission to download this file."):
            return
        self.settings["download_folder"] = str(target.parent)
        try:
            save_state("settings.json", self.settings)
        except OSError as exc:
            self.sync_status.set(f"Could not remember the download folder • {exc}")
        self.busy = True
        self.enable_download()
        self.download_status.set("Connecting…")

        def report(written):
            if written % (4 * 1024 * 1024) == 0:
                self.events.put((self.download_status.set, f"Downloaded {written / 1048576:,.0f} MB…"))

        def finish(status):
            self.busy = False
            self.download_status.set(status)
            self.enable_download()

        def failed(error):
            finish("Download failed")
            messagebox.showerror("Download failed", error)

        def transfer():
            download(rom, target, report)
            return "Saved • SHA-1 matched" if rom.sha1 else "Saved • no checksum provided"

        self.background(transfer, finish, failed)


if __name__ == "__main__":
    App().mainloop()

