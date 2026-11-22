# Spine ROM Downloader

A Windows desktop app for searching Internet Archive collections linked by the [r/ROMs Megathread](https://r-roms.gitlab.io/) and saving public files directly to your PC. BlueRoms adds titles from its supported platform catalogs. It can be used alongside [Spine](https://github.com/paperwolf2350-lab/Spine).

## Run on Windows

Download **Spine-ROM-Downloader.exe** and open it on 64-bit Windows. The standalone build bundles Python and Tk.

### Run from source

1. Install Python 3.11 with Tcl/Tk support.
2. Double-click `spine_rom_downloader.pyw` or run `py spine_rom_downloader.pyw` from the project folder.
3. Choose a platform category and search by filename, region, or collection. Click a column heading to sort by it. The list shows the first 500 matches; **Show 500 more** adds to it.
4. Select a public result and choose **Queue download…** to save it. Hold Ctrl or Shift to select several and queue them into one folder with a single confirmation.

### Build the .exe

Double-click `build.bat`. It installs PyInstaller and writes `dist\Spine-ROM-Downloader.exe`. Python 3.11 or newer from python.org is required.

## Catalog coverage

The app gets its collection links from the Megathread and uses Internet Archive's file listings. Locked files are marked in the results. Files inside a larger archive are searchable only if the host lists them individually.

Downloads use HTTPS, restrict redirects to approved Internet Archive hosts, and compare files with the published SHA-1 when available. The app preserves existing files at the destination. A matching checksum identifies the listed file. Download only files you have permission to obtain.

## Local catalog

On first launch the app reads each Megathread category in the background. Search covers all indexed platforms by default, or you can select one category. Later launches open the saved catalog immediately. With all platforms shown, **Refresh all** updates everything; with one category selected, the button becomes **Refresh this category** and updates only that one. **Catalog details** shows when each category was checked and which listings failed. If a previously indexed item fails during refresh, its cached listing stays searchable. You can keep browsing while a refresh runs.

The app remembers the last confirmed download folder. Windows stores the catalog and folder preference under `%LOCALAPPDATA%\Spine ROM Downloader`. A folder from the earlier name, `Megathread ROM Browser`, is carried over on first run. If a saved file there is damaged, the app starts fresh for that file, keeps the old one as `<name>.damaged`, and tells you. Problems are written to `errors.log` in the same folder.

## BlueRoms catalogs

Use **Refresh all** after upgrading to discover the other BlueRoms platforms. Select a category marked **(BlueRoms)** or search all sources together. The **Source** column identifies each listing. For a BlueRoms download, choose the destination folder and confirm the download. The app then looks up the file, shows transfer progress, and reports the full saved path or the reason it could not save the file. A title is listed before the app checks whether it has a supported direct file. Direct downloads support titles with one compatible direct file, and reports unavailable or ambiguous links in the app.

## Internet Archive sign-in

Choose **Connect Internet Archive…** to sign in with your Internet Archive account. The app sends your credentials only to Internet Archive over HTTPS. It keeps the session in memory until you disconnect or close the app. Public downloads are available without signing in. After connecting, select a restricted result to check whether Internet Archive allows your account to download it.

## Game Boy collection ZIP

The **Game Boy ZIP (Internet Archive)** category lists individual games from the No-Intro Game Boy collection when Internet Archive provides direct member links. Connect your Internet Archive account, select the category and choose **Refresh this category** to index it. **Catalog details** shows if the listing was unavailable. The original collection ZIP remains a separate result when Internet Archive lists it.

## Download queue and history

Select a result and choose **Queue download…**. Spine saves one file at a time. Open **Downloads…** to view progress, cancel queued or active transfers, retry failed or interrupted transfers, and see completed file paths and verification results. The app stores the most recent 100 finished entries locally.

If the connection drops during a download, the app reconnects and continues from where it stopped, and still checks the finished file as a whole. Retrying a failed download by hand starts at the beginning of the file. Before queuing, the app checks that the file is not already queued, that nothing with that name exists, and that the drive has room. BlueRoms publishes no checksums, so those files are saved unverified and the app says so.

## Shared ROM library

Use **ROM library…** to choose the parent folder your launcher scans. When a result has a known platform and its console folder exists, the download dialog starts there. For Internet Archive results the console is read from the collection and folder names, and common folder names are recognized (`psx` or `ps1`, `gc` or `gamecube`, `megadrive` or `genesis`). For ambiguous or unmatched results, the download dialog opens at the library root so you can choose the correct console folder. The app saves files in the format supplied by the source.

When you queue several files and choose the library root, each file goes into its console folder where one exists. Results whose file name already exists anywhere in the library are marked **In library** in the Access column. The match is by file name only, so a renamed or unpacked copy is not recognized.

## License

[MIT](LICENSE). The license covers this app's code only, not any files it downloads.
