# Megathread ROM Browser

A Windows desktop app for searching Internet Archive collections linked by the [r/ROMs Megathread](https://r-roms.gitlab.io/) and saving public files directly to your PC. It can be used alongside [Spine](https://github.com/paperwolf2350-lab/Spine).

## Run on Windows

1. Install Python 3.11 with Tcl/Tk support.
2. Double-click `megathread_rom_browser.pyw` or run `py megathread_rom_browser.pyw` from the project folder.
3. Choose a platform category and search by filename, region, or collection.
4. Select a public result and choose **Download selected…** to save it.

## Catalog coverage

The app gets its collection links from the Megathread and uses Internet Archive's file listings. Locked files are marked in the results. Files inside a larger archive are searchable only if the host lists them individually.

Downloads use HTTPS, restrict redirects to approved Internet Archive hosts, and compare files with the published SHA-1 when available. The app preserves existing files at the destination. A matching checksum identifies the listed file. Download only files you have permission to obtain.
