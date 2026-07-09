import os

# Even with QWebEngineView (Chromium, out-of-process rendering), heavy WebGL/canvas-based
# sites can still take the whole host app down if the bundled Chromium's GPU process hits a
# driver bug -- this is a known failure mode on macOS in particular, and Bing Maps' modern
# map renderer is GPU-heavy in a way Google's/Apple's simpler embeds aren't. Forcing software
# rendering (no GPU process) trades a bit of smoothness for not crashing. These must be set
# before QtWebEngine spins up its first render process, so set them before importing it.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

# Bing Maps (and other modern, JS-heavy map sites) can crash QGIS outright when
# rendered with the legacy, in-process QtWebKit engine (QWebView) -- WebKit here is
# unmaintained since Qt 5.6 and a renderer fault takes the whole QGIS process with it.
# QWebEngineView (QtWebEngine/Chromium) runs its renderer out-of-process and is what
# QGIS's own browser panel uses, so we prefer it and only fall back to QWebView if a
# particular QGIS build wasn't compiled with QtWebEngine support.
try:
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView as WebView
    USING_WEBENGINE = True
except ImportError:
    from qgis.PyQt.QtWebKitWidgets import QWebView as WebView  # Fallback for QGIS < 3.6 / no QtWebEngine
    USING_WEBENGINE = False

from qgis.PyQt.QtWidgets import QAction, QMainWindow, QVBoxLayout, QWidget, QMenu, QDialog, QComboBox, QLabel, \
    QPushButton, QVBoxLayout, QToolBar, QMessageBox
from qgis.core import QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRectangle
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt, QUrl, QSettings, QSize, QTimer  # Import QUrl, QSettings, QTimer from qgis.PyQt.QtCore
from qgis.PyQt.QtGui import QIcon
import webbrowser
import math

from .resources import *

# Full superset of options, used as a fallback for any provider not listed below.
BASEMAP_OPTIONS = ["Roadmap", "Satellite", "Terrain"]
OVERLAY_OPTIONS = ["None", "Traffic", "Transit", "Bicycling"]

# What each provider actually supports -- the settings dialog filters its "Base map
# style" and "Overlay layer" dropdowns to these lists based on the selected provider,
# instead of always showing options that don't apply (e.g. "Terrain" for Bing, or any
# basemap switch at all for the single-style OSM-based providers).
PROVIDER_BASEMAPS = {
    "Google Maps": ["Roadmap", "Satellite", "Terrain"],
    "Bing Maps": ["Roadmap", "Satellite"],
    "Apple Maps": ["Roadmap", "Satellite"],
    "OpenStreetMap": ["Standard"],
    "OpenTopoMap": ["Topographic"],
    "Wikimedia Maps": ["Standard"],
}
PROVIDER_OVERLAYS = {
    "Google Maps": ["None", "Traffic", "Transit", "Bicycling"],
    "Bing Maps": ["None", "Traffic"],
    "Apple Maps": ["None", "Transit"],
    "OpenStreetMap": ["None", "Transit", "Bicycling"],
    "OpenTopoMap": ["None"],
    "Wikimedia Maps": ["None"],
}


def _strip_experimental_marker(map_type):
    # Settings combo shows "Bing Maps *" / "Apple Maps *" (the "*" just flags them as
    # experimental); strip it so it matches the plain provider name used everywhere else.
    return (map_type or "Google Maps").replace("*", "").strip()


class QuickMapLinkPlugin:
    def __init__(self, iface: QgisInterface):
        self.iface = iface
        self.context_menu = QMenu()
        self.context_action = QAction("Open Map Here (Webview)", self.iface.mainWindow())
        self.context_action.triggered.connect(self.open_google_maps_context)
        self.context_browser_action = QAction("Open Map Here (Browser)", self.iface.mainWindow())
        self.context_browser_action.setCheckable(True)
        self.context_browser_action.triggered.connect(self.toggle_browser_follow)
        self.settings_action = QAction("Map Settings", self.iface.mainWindow())
        self.settings_action.triggered.connect(self.open_settings_dialog)

        # Load the default map type / basemap style / overlay layer from settings
        self.settings = QSettings("MyOrganization", "QuickMapsLink Settings")
        self.map_type = self.settings.value("map_type", "Google Maps")
        self.basemap_style = self.settings.value("basemap_style", "Roadmap")
        self.overlay_layer = self.settings.value("overlay_layer", "None")

        # Toolbar button
        self.toolbar_button = QAction(QIcon(":/plugins/quick_map_link/icon.png"), "Toggle QuickMapLink", self.iface.mainWindow())
        self.toolbar_button.setCheckable(True)
        self.toolbar_button.setChecked(True)  # Initially checked
        self.toolbar_button.triggered.connect(self.toggle_context_menu_options)

        # Add actions to context menu
        self.context_menu.addAction(self.context_action)
        self.context_menu.addAction(self.context_browser_action)

        # --- Live "follow the view finder" state ---
        self.window = None          # internal webview window (tracked so we know if it's open)
        self.webview = None
        self.browser_follow_active = False  # whether "Open Map Here (Browser)" is in follow mode

        # Debounce timer: coalesces rapid extentsChanged signals (e.g. while dragging the canvas)
        # so we only push an update shortly after the view finder settles.
        self._follow_timer = QTimer()
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(400)  # ms after the canvas view stops moving
        self._follow_timer.timeout.connect(self._on_canvas_settled)

    def initGui(self):
        # Add the settings action to the plugin menu
        self.iface.addPluginToMenu("QuickMapLink", self.settings_action)
        self.iface.mapCanvas().setContextMenuPolicy(Qt.CustomContextMenu)
        self.iface.mapCanvas().customContextMenuRequested.connect(self.show_context_menu)

        # Follow the QGIS view finder: fires on every pan/zoom/rotate of the canvas
        self.iface.mapCanvas().extentsChanged.connect(self._on_canvas_extents_changed)

        # Add toolbar button
        self.toolbar = self.iface.addToolBar("QuickMapLink")
        self.toolbar.addAction(self.toolbar_button)

    def unload(self):
        self.iface.removePluginMenu("QuickMapLink", self.settings_action)
        self.iface.mapCanvas().customContextMenuRequested.disconnect(self.show_context_menu)
        self.iface.mapCanvas().extentsChanged.disconnect(self._on_canvas_extents_changed)

        # Remove toolbar button
        self.iface.removeToolBarIcon(self.toolbar_button)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        del self.toolbar

    # ------------------------------------------------------------------
    # Live-follow: react to the QGIS view finder moving
    # ------------------------------------------------------------------
    def _on_canvas_extents_changed(self):
        # Only bother debouncing if something is actually listening for updates
        if (self.window is not None and self.window.isVisible()) or self.browser_follow_active:
            self._follow_timer.start()  # (re)starts the debounce window

    def _on_canvas_settled(self):
        latitude, longitude = self.get_canvas_location()
        url = self.build_map_url(latitude, longitude)

        if self.window is not None and self.window.isVisible():
            print(f"[QuickMapLink] Following webview to: {url}")
            self.webview.setUrl(QUrl(url))

        if self.browser_follow_active:
            print(f"[QuickMapLink] Following browser to: {url}")
            webbrowser.open(url)

    def toggle_browser_follow(self, checked):
        if checked:
            # First activation: open the tab immediately at the current point, then follow
            self.open_google_maps_in_browser(getattr(self, "context_point", None))
            self.browser_follow_active = True
            self.context_browser_action.setText("Stop Following (Browser)")
        else:
            self.browser_follow_active = False
            self.context_browser_action.setText("Open Map Here (Browser)")

    def open_google_maps_context(self):
        self.open_google_maps_at_location(self.context_point)

    # ------------------------------------------------------------------
    # Coordinate / URL helpers (shared by webview, browser, and follow-mode)
    # ------------------------------------------------------------------
    def get_canvas_location(self, point=None):
        """Return (latitude, longitude) in WGS84 for a given canvas-widget
        pixel point, or for the current canvas center if no point is given."""
        extent = self.iface.mapCanvas().extent()

        if point:
            map_point = self.iface.mapCanvas().getCoordinateTransform().toMapCoordinates(point.x(), point.y())
        else:
            map_point = extent.center()

        # Transform the point to WGS 84 (EPSG:4326) if needed
        crs = QgsProject.instance().crs()
        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            map_point = transform.transform(map_point)

        return map_point.y(), map_point.x()

    def estimate_zoom_level(self):
        """Approximate a web-mercator zoom level that matches how zoomed-in the
        current QGIS canvas view is, so the opened map roughly matches what's
        visible in QGIS instead of always opening at a fixed zoom."""
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        crs = QgsProject.instance().crs()

        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            extent = transform.transformBoundingBox(extent)

        width_deg = extent.width()
        canvas_width_px = canvas.mapSettings().outputSize().width() or 800

        if width_deg <= 0:
            return 15.0

        # At zoom z, a 256px tile covers 360 degrees / 2^z of longitude.
        zoom = math.log2(360.0 * canvas_width_px / (256.0 * width_deg))
        return zoom

    def _normalized_map_type(self):
        return _strip_experimental_marker(self.map_type)

    def build_map_url(self, latitude, longitude):
        zoom = self.estimate_zoom_level()
        provider = self._normalized_map_type()

        if provider == "Bing Maps":
            return self._build_bing_url(latitude, longitude, zoom)
        elif provider == "Apple Maps":
            return self._build_apple_url(latitude, longitude, zoom)
        elif provider == "OpenStreetMap":
            return self._build_osm_url(latitude, longitude, zoom)
        elif provider == "OpenTopoMap":
            return self._build_opentopomap_url(latitude, longitude, zoom)
        elif provider == "Wikimedia Maps":
            return self._build_wikimedia_url(latitude, longitude, zoom)
        else:
            return self._build_google_url(latitude, longitude, zoom)  # default to Google Maps

    def _build_google_url(self, latitude, longitude, zoom):
        # https://developers.google.com/maps/documentation/urls/get-started#map-action
        basemap = {"Roadmap": "roadmap", "Satellite": "satellite", "Terrain": "terrain"}.get(
            self.basemap_style, "roadmap")
        layer = {"None": "none", "Traffic": "traffic", "Transit": "transit", "Bicycling": "bicycling"}.get(
            self.overlay_layer, "none")
        zoom = round(max(0, min(21, zoom)))
        return (f"https://www.google.com/maps/@?api=1&map_action=map&center={latitude},{longitude}"
                f"&zoom={zoom}&basemap={basemap}&layer={layer}")

    def _build_bing_url(self, latitude, longitude, zoom):
        # https://learn.microsoft.com/en-us/bingmaps/articles/create-a-custom-map-url
        # style: r=road, a=aerial (satellite), h=aerial with labels, o/b=bird's eye.
        # Bing has no terrain style, so it falls back to road.
        style = {"Roadmap": "r", "Satellite": "a", "Terrain": "r"}.get(self.basemap_style, "r")
        traffic = "1" if self.overlay_layer == "Traffic" else "0"
        zoom = round(max(1, min(20, zoom)))
        return (f"https://bing.com/maps/default.aspx?cp={latitude}~{longitude}"
                f"&lvl={zoom}&style={style}&trfc={traffic}")

    def _build_apple_url(self, latitude, longitude, zoom):
        # https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/MapLinks/MapLinks.html
        # t: m=standard, k=satellite, h=hybrid (deprecated), r=transit view.
        # Apple has no terrain style and no separate traffic/bicycling overlay.
        if self.overlay_layer == "Transit":
            map_type = "r"
        else:
            map_type = {"Roadmap": "m", "Satellite": "k", "Terrain": "m"}.get(self.basemap_style, "m")
        zoom = round(max(2, min(21, zoom)))
        return f"https://maps.apple.com/?ll={latitude},{longitude}&z={zoom}&t={map_type}"

    def _build_osm_url(self, latitude, longitude, zoom):
        # https://wiki.openstreetmap.org/wiki/Browsing
        # Single default render (Mapnik); the only real "layer" switch available is
        # &layers=C (CyclOSM) or &layers=H (Humanitarian). Basemap style (satellite/
        # terrain) doesn't apply -- OSM only has the one road-map style.
        zoom = round(max(0, min(19, zoom)))
        url = f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map={zoom}/{latitude}/{longitude}"
        if self.overlay_layer == "Bicycling":
            url += "&layers=C"
        elif self.overlay_layer == "Transit":
            url += "&layers=H"  # closest available match: Humanitarian OSM Team style
        return url

    def _build_opentopomap_url(self, latitude, longitude, zoom):
        # https://opentopomap.org -- single topographic/contour style, no basemap or
        # overlay options to switch (no satellite/traffic/transit equivalents).
        zoom = round(max(0, min(17, zoom)))  # OpenTopoMap tiles top out around z17
        return f"https://opentopomap.org/#map={zoom}/{latitude}/{longitude}"

    def _build_wikimedia_url(self, latitude, longitude, zoom):
        # https://maps.wikimedia.org -- single default OSM-based style, no basemap or
        # overlay switches in the URL.
        zoom = round(max(0, min(18, zoom)))
        return f"https://maps.wikimedia.org/#{zoom}/{latitude}/{longitude}"

    def open_google_maps_at_location(self, point=None):
        if not USING_WEBENGINE:
            # QtWebEngine isn't available in this QGIS build, so we're stuck with the
            # legacy QtWebKit renderer, which is the thing known to crash on heavy
            # sites like Bing Maps. Warn loudly rather than fail silently.
            print("[QuickMapLink] WARNING: QtWebEngine not found, falling back to QtWebKit's QWebView. "
                  "This renderer is unmaintained and known to crash on modern map sites (e.g. Bing Maps). "
                  "Consider using 'Open Map Here (Browser)' instead for those providers, or installing a "
                  "QGIS build with QtWebEngine support.")

        if self._normalized_map_type() == "Bing Maps":
            # Bing's embedded map has been observed to hard-crash QGIS (silently, no error
            # dialog) even under QWebEngineView -- most likely a GPU/graphics-driver crash
            # inside the bundled Chromium engine that the software-rendering flags above
            # don't always catch. Rather than risk losing unsaved QGIS work again, ask first.
            choice = QMessageBox.warning(
                self.iface.mainWindow(),
                "Bing Maps (Webview) is unstable",
                "The embedded Bing Maps view has been known to crash QGIS on some systems.\n\n"
                "Open it in your default browser instead (safe), or continue in the embedded "
                "webview anyway (risk of a crash)?",
                QMessageBox.Open | QMessageBox.Ignore | QMessageBox.Cancel,
                QMessageBox.Open,
            )
            if choice == QMessageBox.Cancel:
                return
            if choice == QMessageBox.Open:
                self.open_google_maps_in_browser(point)
                return
            # choice == QMessageBox.Ignore: fall through and open the webview anyway

        self.window = QMainWindow()
        self.window.setWindowTitle(self.map_type + " (Webview)")
        self.window.setGeometry(100, 100, 800, 600)

        self.webview = WebView()  # QWebEngineView when available; QWebView as a last-resort fallback
        try:
            self.webview.loadFinished.connect(self._nudge_map_resize)
        except AttributeError:
            pass

        central_widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.webview)
        central_widget.setLayout(layout)
        self.window.setCentralWidget(central_widget)

        # Show the window (giving the webview its final size) BEFORE loading the URL.
        # Leaflet-based sites (OSM, OpenTopoMap, Wikimedia Maps) measure their container
        # size when the map initializes; if setUrl() runs first, the widget is still 0x0
        # (not yet laid out/shown), so the JS map inits at zero size and tiles never load
        # -- you just get a blank grey pane even though the page itself loaded fine.
        self.window.show()

        latitude, longitude = self.get_canvas_location(point)
        url = self.build_map_url(latitude, longitude)

        print(f"URL: {url}")
        self.webview.setUrl(QUrl(url))  # Create a QUrl object
        # From here on, _on_canvas_extents_changed will keep this window in sync
        # with the QGIS view finder for as long as it stays open (self.window.isVisible()).

    def _nudge_map_resize(self, ok=True):
        """Some JS map libraries (Leaflet in particular) still mis-measure their
        container on first paint even when shown before load. Dispatching a resize
        event after the page finishes loading makes them recompute and fill in tiles."""
        if not ok or self.webview is None:
            return
        js = "window.dispatchEvent(new Event('resize'));"
        if USING_WEBENGINE:
            self.webview.page().runJavaScript(js)
        else:  # QWebView (QtWebKit) fallback
            self.webview.page().mainFrame().evaluateJavaScript(js)

    def open_google_maps_in_browser(self, point=None):
        latitude, longitude = self.get_canvas_location(point)
        url = self.build_map_url(latitude, longitude)

        print(f"URL: {url}")
        webbrowser.open(url)

    def show_context_menu(self, point):
        self.context_point = point
        if self.toolbar_button.isChecked():
            self.context_menu.exec_(self.iface.mapCanvas().mapToGlobal(point))

    def open_settings_dialog(self):
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("QuickMapLink Settings")
        layout = QVBoxLayout()

        # Map Type Selection
        map_type_label = QLabel("Select web map provider:")
        experimental_label = QLabel("* Some providers are experimental and may not work perfectly.:")
        layout.addWidget(map_type_label)
        layout.addWidget(experimental_label)
        map_type_combo = QComboBox()
        map_type_combo.addItems([
            "Google Maps", "Bing Maps *", "Apple Maps *",
            "OpenStreetMap", "OpenTopoMap", "Wikimedia Maps",
        ])
        map_type_combo.setCurrentText(self.map_type)
        layout.addWidget(map_type_combo)

        # Base Map Style Selection (options depend on the selected provider)
        basemap_label = QLabel("Base map style:")
        layout.addWidget(basemap_label)
        basemap_combo = QComboBox()
        layout.addWidget(basemap_combo)

        # Overlay Layer Selection (options depend on the selected provider)
        overlay_label = QLabel("Overlay layer:")
        layout.addWidget(overlay_label)
        overlay_combo = QComboBox()
        layout.addWidget(overlay_combo)

        def refresh_provider_options(preferred_basemap=None, preferred_overlay=None):
            provider = _strip_experimental_marker(map_type_combo.currentText())
            basemaps = PROVIDER_BASEMAPS.get(provider, BASEMAP_OPTIONS)
            overlays = PROVIDER_OVERLAYS.get(provider, OVERLAY_OPTIONS)

            basemap_combo.blockSignals(True)
            basemap_combo.clear()
            basemap_combo.addItems(basemaps)
            basemap_combo.setCurrentText(preferred_basemap if preferred_basemap in basemaps else basemaps[0])
            basemap_combo.blockSignals(False)

            overlay_combo.blockSignals(True)
            overlay_combo.clear()
            overlay_combo.addItems(overlays)
            overlay_combo.setCurrentText(preferred_overlay if preferred_overlay in overlays else overlays[0])
            overlay_combo.blockSignals(False)

        # Populate using the plugin's current saved choices, then re-filter (dropping
        # any selection the new provider doesn't support) whenever the provider changes.
        refresh_provider_options(self.basemap_style, self.overlay_layer)
        map_type_combo.currentTextChanged.connect(lambda _: refresh_provider_options())

        # Save Button
        save_button = QPushButton("Save")
        save_button.clicked.connect(lambda: self.save_settings(
            map_type_combo.currentText(), basemap_combo.currentText(), overlay_combo.currentText(), dialog))
        layout.addWidget(save_button)

        dialog.setLayout(layout)
        dialog.exec_()

    def save_settings(self, map_type, basemap_style, overlay_layer, dialog):
        self.map_type = map_type
        self.basemap_style = basemap_style
        self.overlay_layer = overlay_layer
        self.settings.setValue("map_type", map_type)
        self.settings.setValue("basemap_style", basemap_style)
        self.settings.setValue("overlay_layer", overlay_layer)
        dialog.close()

    def toggle_context_menu_options(self):
        if self.toolbar_button.isChecked():
            self.context_action.setVisible(True)
            self.context_browser_action.setVisible(True)
        else:
            self.context_action.setVisible(False)
            self.context_browser_action.setVisible(False)


def classFactory(iface):
    return QuickMapLinkPlugin(iface)
