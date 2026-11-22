# Spine ROM Browser

A Windows desktop app for searching Internet Archive collections linked by the [r/ROMs Megathread](https://r-roms.gitlab.io/) and saving public files directly to your PC. BlueRoms adds titles from its supported platform catalogs. It can be used alongside [Spine](https://github.com/paperwolf2350-lab/Spine).

## Run on Windows

Download and open [Spine-ROM-Browser.exe](Spine-ROM-Browser.exe) on 64-bit Windows. The standalone build bundles Python and Tk.

### Run from source

1. Install Python 3.11 with Tcl/Tk support.
2. Double-click `megathread_rom_browser.pyw` or run `py megathread_rom_browser.pyw` from the project folder.
3. Choose a platform category and search by filename, region, or collection.
4. Select a public result and choose **Queue download…** to save it.

## Catalog coverage

The app gets its collection links from the Megathread and uses Internet Archive's file listings. Locked files are marked in the results. Files inside a larger archive are searchable only if the host lists them individually.

Downloads use HTTPS, restrict redirects to approved Internet Archive hosts, and compare files with the published SHA-1 when available. The app preserves existing files at the destination. A matching checksum identifies the listed file. Download only files you have permission to obtain.

## Local catalog

On first launch the app reads each Megathread category in the background. Search covers all indexed platforms by default, or you can select one category. Later launches open the saved catalog immediately. Use **Refresh catalog** to update it and **Catalog details** to see when each category was checked and which listings failed. If a previously indexed item fails during refresh, its cached listing stays searchable.

The app remembers the last confirmed download folder. Windows stores the catalog and folder preference under `%LOCALAPPDATA%\Megathread ROM Browser`.


## BlueRoms catalogs

Use **Refresh catalog** after upgrading to discover the other BlueRoms platforms. Select a category marked **(BlueRoms)** or search all sources together. The **Source** column identifies each listing. For a BlueRoms download, choose the destination folder and confirm the download. The app then looks up the file, shows transfer progress, and reports the full saved path or the reason it could not save the file. A title is listed before the app checks whether it has a supported direct file. Direct downloads support titles with one compatible direct file, and reports unavailable or ambiguous links in the app.





## Internet Archive sign-in

Choose **Connect Internet Archive…** to sign in with your Internet Archive account. The app sends your credentials only to Internet Archive over HTTPS. It keeps the session in memory until you disconnect or close the app. Public downloads are available without signing in. After connecting, select a restricted result to check whether Internet Archive allows your account to download it.

## Game Boy collection ZIP

The **Game Boy ZIP (Internet Archive)** category lists individual games from the No-Intro Game Boy collection when Internet Archive provides direct member links. Connect your Internet Archive account and select **Refresh catalog** to index it. **Catalog details** shows if the listing was unavailable. The original collection ZIP remains a separate result when Internet Archive lists it.

## Download queue and history

Select a result and choose **Queue download…**. Spine saves one file at a time. Open **Downloads…** to view progress, cancel queued or active transfers, retry failed or interrupted transfers, and see completed file paths and verification results. The app stores the most recent 100 finished entries locally. Retrying starts at the beginning of the file.

## Shared ROM library

Use **ROM library…** to choose the parent folder your launcher scans. When a result has a known platform and its console folder exists, the download dialog starts there. For ambiguous or unmatched results, the download dialog opens at the library root so you can choose the correct console folder. The app saves files in the format supplied by the source.
