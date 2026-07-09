from qgis.PyQt.QtWebKitWidgets import QWebView  # Use QWebView for QGIS < 3.6
from qgis.PyQt.QtWidgets import QAction, QMainWindow, QVBoxLayout, QWidget, QMenu, QDialog, QComboBox, QLabel, \
    QPushButton, QVBoxLayout, QToolBar
from qgis.core import QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRectangle
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt, QUrl, QSettings, QSize, QTimer  # Import QUrl, QSettings, QTimer from qgis.PyQt.QtCore
from qgis.PyQt.QtGui import QIcon
import webbrowser
import math

from .resources import *

# Base map style options offered in the settings UI, and how each provider maps them.
BASEMAP_OPTIONS = ["Roadmap", "Satellite", "Terrain"]
# Overlay options offered in the settings UI, and how each provider maps them
# (providers that don't support a given overlay just fall back to "no overlay").
OVERLAY_OPTIONS = ["None", "Traffic", "Transit", "Bicycling"]


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
        # Settings combo shows "Bing Maps *" / "Apple Maps *" (the "*" just flags
        # them as experimental); strip it so it matches the plain provider name.
        return (self.map_type or "Google Maps").replace("*", "").strip()

    def build_map_url(self, latitude, longitude):
        zoom = self.estimate_zoom_level()
        provider = self._normalized_map_type()

        if provider == "Bing Maps":
            return self._build_bing_url(latitude, longitude, zoom)
        elif provider == "Apple Maps":
            return self._build_apple_url(latitude, longitude, zoom)
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

    def open_google_maps_at_location(self, point=None):
        self.window = QMainWindow()
        self.window.setWindowTitle(self.map_type + " (Webview)")
        self.window.setGeometry(100, 100, 800, 600)

        self.webview = QWebView()  # Use QWebView instead of QWebEngineView

        latitude, longitude = self.get_canvas_location(point)
        url = self.build_map_url(latitude, longitude)

        print(f"URL: {url}")
        self.webview.setUrl(QUrl(url))  # Create a QUrl object

        central_widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.webview)
        central_widget.setLayout(layout)

        self.window.setCentralWidget(central_widget)
        self.window.show()
        # From here on, _on_canvas_extents_changed will keep this window in sync
        # with the QGIS view finder for as long as it stays open (self.window.isVisible()).

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
        map_type_combo.addItems(["Google Maps", "Bing Maps *", "Apple Maps *"])
        map_type_combo.setCurrentText(self.map_type)
        layout.addWidget(map_type_combo)

        # Base Map Style Selection
        basemap_label = QLabel("Base map style:")
        layout.addWidget(basemap_label)
        basemap_combo = QComboBox()
        basemap_combo.addItems(BASEMAP_OPTIONS)
        basemap_combo.setCurrentText(self.basemap_style)
        layout.addWidget(basemap_combo)

        # Overlay Layer Selection
        overlay_label = QLabel("Overlay layer:")
        overlay_note = QLabel("* Not every overlay is supported by every provider.")
        layout.addWidget(overlay_label)
        layout.addWidget(overlay_note)
        overlay_combo = QComboBox()
        overlay_combo.addItems(OVERLAY_OPTIONS)
        overlay_combo.setCurrentText(self.overlay_layer)
        layout.addWidget(overlay_combo)

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
