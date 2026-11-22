# Megathread ROM Browser

A Windows desktop app for searching Internet Archive collections linked by the [r/ROMs Megathread](https://r-roms.gitlab.io/) and saving public files directly to your PC. An experimental BlueRoms integration adds Game Boy Advance titles. It can be used alongside [Spine](https://github.com/paperwolf2350-lab/Spine).

## Run on Windows

1. Install Python 3.11 with Tcl/Tk support.
2. Double-click `megathread_rom_browser.pyw` or run `py megathread_rom_browser.pyw` from the project folder.
3. Choose a platform category and search by filename, region, or collection.
4. Select a public result and choose **Download selected…** to save it.

## Catalog coverage

The app gets its collection links from the Megathread and uses Internet Archive's file listings. Locked files are marked in the results. Files inside a larger archive are searchable only if the host lists them individually.

Downloads use HTTPS, restrict redirects to approved Internet Archive hosts, and compare files with the published SHA-1 when available. The app preserves existing files at the destination. A matching checksum identifies the listed file. Download only files you have permission to obtain.

## Local catalog

On first launch the app reads each Megathread category in the background. Search covers all indexed platforms by default, or you can select one category. Later launches open the saved catalog immediately. Use **Refresh catalog** to update it and **Catalog details** to see when each category was checked and which listings failed. If a previously indexed item fails during refresh, its cached listing stays searchable.

The app remembers the last confirmed download folder. Windows stores the catalog and folder preference under `%LOCALAPPDATA%\Megathread ROM Browser`.


## BlueRoms pilot

Select **Game Boy Advance (BlueRoms)** to explore just that catalog, or search all sources together. The **Source** column identifies each listing. BlueRoms links are resolved when you select a download, using the site's direct HTTPS file host. A title is listed before the app checks whether it has a supported direct file. The pilot supports titles with one direct file, and reports unavailable or ambiguous links in the app.
