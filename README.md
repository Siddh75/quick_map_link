# QuickMapLink QGIS Plugin

[![License: GPL v2+](https://img.shields.io/badge/License-GPL%20v2+-blue.svg)](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html)
![QGIS Version](https://img.shields.io/badge/QGIS-%3E%3D%203.16-brightgreen.svg)
[![Latest Release](https://img.shields.io/github/v/release/Siddh75/quick_map_link)](https://github.com/Siddh75/quick_map_link/releases)
[![Open Issues](https://img.shields.io/github/issues/Siddh75/quick_map_link)](https://github.com/Siddh75/quick_map_link/issues)

Right-click the QGIS map canvas to instantly open that location in Google Maps, Bing Maps, Apple Maps, OpenStreetMap, OpenTopoMap, or Wikimedia Maps — either in an embedded, dockable webview or your system browser.

## Contents

- [Demo](#demo)
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Compatibility](#compatibility)
- [Known Limitations](#known-limitations)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Author](#author)

## Demo

![QuickMapLink demo](assets/demo.gif)

*Right-click anywhere on the canvas, pick a provider, and the dockable panel opens and live-follows your QGIS view as you pan and zoom.*

## Features

- **Native context menu integration:** Adds a single "Open in `<Provider>`" entry directly to QGIS's right-click context menu on the map canvas — no extra clicks to get past.
- **Six map providers:** Google Maps, Bing Maps, Apple Maps, OpenStreetMap, OpenTopoMap, and Wikimedia Maps.
- **Two viewing modes:**
  - **Webview:** Opens the location in a dockable panel inside QGIS. Dock it to any side of the QGIS window or leave it floating.
  - **Browser:** Opens the location in your system's default web browser, either as a one-off snapshot or in live-follow mode.
- **Live-follow:** In webview mode (and optionally in browser mode), the map view updates automatically as you pan and zoom the QGIS canvas, debounced to avoid excessive reloads.
- **Cursor overlay:** A crosshair on the embedded map mirrors your mouse position on the QGIS canvas in real time, so you can visually cross-reference the two views.
- **Configurable via Settings:** Choose your default provider and viewing mode (webview or browser) once in the settings dialog; the context menu then reflects that choice with a single click.
- **Per-provider basemap/overlay styles:** Pick satellite, terrain, labels, and other styles where a provider supports them.
- **Coordinate transformation:** Automatically transforms coordinates from your project's CRS to WGS 84 (EPSG:4326) for compatibility with web maps.

> **Coming soon (pending QGIS Plugin Repository approval):** native tile basemaps — Esri, USGS Topo, CartoDB, CyclOSM, and more — rendered directly in QGIS's own map canvas alongside the web providers above, plus Qt6 compatibility. See [Roadmap](#roadmap).

## Installation

### From QGIS Plugin Repository

1. Open QGIS.
2. Go to `Plugins` -> `Manage and Install Plugins...`.
3. Search for `QuickMapLink`.
4. Select the plugin and click `Install Plugin`.

### Manual Installation

1. Download the latest plugin release `.zip` from the [Releases page](https://github.com/Siddh75/quick_map_link/releases).
2. Open QGIS.
3. Go to `Plugins` -> `Manage and Install Plugins...`.
4. Switch to the `Install from ZIP` tab.
5. Browse to the downloaded `.zip` file and click `Install Plugin`.
6. Alternatively, unzip the file into your QGIS plugins directory:
   - **Windows:** `C:\Users\<YourUsername>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\`
   - **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
7. Enable the `QuickMapLink` plugin in the `Installed` tab of the Plugin Manager.

## Usage

1. Once installed and enabled, **right-click** anywhere on the QGIS map canvas.
2. Click the `Open in <Provider>` entry that appears in the context menu. It opens in whichever mode (webview or browser) you've configured in Settings.
3. In webview mode, the panel can be docked to any edge of the QGIS window or left floating, and will live-follow your QGIS view as you pan and zoom.

## Configuration

1. Go to the QGIS menu: `Plugins` -> `QuickMapLink`.
2. In the settings dialog, check `Enable` to activate the context menu entry.
3. Choose your preferred map provider and viewing mode (Webview or Browser).
4. If using Browser mode, optionally enable live-follow (a new tab opens on every canvas movement — a confirmation prompt explains this before you turn it on).
5. Click `Save`. Open webviews refresh immediately to reflect the new settings.

## Compatibility

- **QGIS:** Requires QGIS 3.16 or higher (the plugin relies on `QgsMapCanvas.contextMenuAboutToShow`, introduced in 3.16).
- **Webview:** Uses `QWebEngineView` where available, with a `QWebView` (QtWebKit) fallback on installations without QtWebEngine. Some providers that rely on modern JavaScript (e.g. OpenStreetMap) are automatically restricted to Browser mode when only the QtWebKit fallback is available, since QtWebKit cannot render their pages correctly.
- **Qt6 / QGIS 4.x:** Not yet supported in the current published release — see [Roadmap](#roadmap).

## Known Limitations

- **Street View shows a black screen** in Google Maps' Street View mode on some systems. Tracked as an open issue — see the [Issues page](https://github.com/Siddh75/quick_map_link/issues) for status, or to report it if you hit it too.
- **Bing Maps and Apple Maps** are Browser-mode only (not offered in Webview) due to instability when embedded.

If you run into something not listed here, please [open an issue](https://github.com/Siddh75/quick_map_link/issues) rather than assuming it's known.

## Roadmap

The next release (currently awaiting QGIS Plugin Repository approval) adds:

- Native tile basemaps — Esri, USGS Topo, CartoDB, CyclOSM, and more — rendered directly in QGIS's own map canvas, unified into the same provider dropdown as the web providers.
- Qt6 compatibility, so the plugin loads correctly on QGIS builds based on Qt6.

## Contributing

Contributions are welcome. If you find a bug or have a feature request, please [open an issue](https://github.com/Siddh75/quick_map_link/issues). To contribute code, fork the repository and submit a pull request.

## License

This project is licensed under the GNU General Public License v2.0 or later (GPLv2+) — see the [`LICENSE`](LICENSE) file for details.

## Author

- **Siddharth** - (siddharthgupta7may@gmail.com)
