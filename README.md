# QuickMapLink QGIS Plugin

[![License: GPL v2+](https://img.shields.io/badge/License-GPL%20v2+-blue.svg)](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html) <!-- Choose your license -->
![QGIS Version](https://img.shields.io/badge/QGIS-%3E%3D%203.0-brightgreen.svg)

Opens web maps (Google Maps, Bing Maps, or Apple Maps) in a separate window or browser tab directly from within QGIS. Provides a context menu option to open the map at a specific location and a settings option to choose the preferred map provider.

<!-- 
**RECOMMENDED:** Add a screenshot or GIF here showing the context menu in action and maybe the resulting web map view.
![QuickMapLink Screenshot](link_to_your_screenshot.png) 
-->

## Features

*   **Context Menu Integration:** Adds options directly to the map canvas right-click menu.
*   **Multiple Viewing Options:**
    *   Open the selected location in an **embedded web view** within QGIS (`QWebView`).
    *   Open the selected location in your system's **default web browser**.
*   **Multiple Map Providers:** Supports viewing locations in:
    *   Google Maps
    *   Bing Maps
    *   Apple Maps (Beta)
*   **Configurable:** Choose your preferred default map provider via a simple settings dialog.
*   **Toolbar Toggle:** Includes a toolbar button to quickly show or hide the plugin's context menu options if they clutter your workflow.
*   **Coordinate Transformation:** Automatically transforms coordinates from your project's CRS to WGS 84 (EPSG:4326) for compatibility with web maps.

## Installation
<!--
### From QGIS Plugin Repository (Recommended)

1.  Open QGIS.
2.  Go to `Plugins` -> `Manage and Install Plugins...`.
3.  Search for `QuickMapLink`.
4.  Select the plugin and click `Install Plugin`.

*(Note: You will need to package and upload your plugin to the official QGIS repository for this method to work.)*
-->
### Manual Installation

1.  Download the latest plugin release `.zip` file from the Releases page ([Replace with your actual GitHub repo link](https://github.com/Siddh75/quick_map_link/releases/tag/v1.2)).
2.  Open QGIS.
3.  Go to `Plugins` -> `Manage and Install Plugins...`.
4.  Switch to the `Install from ZIP` tab.
5.  Browse to the downloaded `.zip` file and click `Install Plugin`.
6.  Alternatively, unzip the file into your QGIS plugins directory:
    *   **Windows:** `C:\Users\<YourUsername>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\`
    *   **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
    *   **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
7.  Enable the `QuickMapLink` plugin in the `Installed` tab of the Plugin Manager.

## Usage

1.  Once installed and enabled, simply **right-click** anywhere on the QGIS map canvas.
2.  You will see two new options (if the toolbar button is active):
    *   `Open Map Here (Webview)`: Opens the location in a new window inside QGIS using the map provider selected in settings.
    *   `Open Map Here (Browser)`: Opens the location in your default web browser using the map provider selected in settings.
3.  Click the desired option.
4.  **Toolbar Button:** Find the QuickMapLink icon (icon.png) in your QGIS toolbars. Clicking this button toggles the visibility of the `Open Map Here...` options in the right-click context menu.

## Configuration

To change the default map provider:

1.  Go to the QGIS menu: `Plugins` -> `QuickMapLink` -> `Map Settings`.
2.  A dialog box will appear. Select your preferred map provider (Google Maps, Bing Maps, or Apple Maps) from the dropdown menu.
3.  Click `Save`.

The selected provider will now be used when you choose either the "Webview" or "Browser" option from the context menu.

## Compatibility

*   **QGIS:** Requires QGIS version 3.0 or higher.
*   **Web View:** This plugin currently uses `QWebView` for the embedded map view. `QWebView` is based on an older WebKit engine and is deprecated in newer Qt versions (which QGIS uses). While it should work on many QGIS 3.x installations, it might be removed in future QGIS releases. If the embedded view stops working in a future QGIS version, the "Open in Browser" option should still function. An update to use `QWebEngineView` might be required for future compatibility.

## Contributing

Contributions are welcome! If you find a bug or have a feature request, please open an issue. If you'd like to contribute code, please feel free to fork the repository and submit a pull request.

## License

This project is licensed under the **[Choose a License - e.g., GNU General Public License v2.0 or later (GPLv2+)]** - see the `LICENSE` file for details.

*(**Important:** You need to choose an open-source license (like GPLv2+ which is common for QGIS plugins, or MIT, Apache 2.0, etc.) and add a corresponding `LICENSE` file to your repository.)*

## Author

*   **Siddharth** - (siddharthgupta7may@gmail.com)
