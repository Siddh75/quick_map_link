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
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView as WebView, QWebEnginePage
    USING_WEBENGINE = True

    class _DiagnosticWebEnginePage(QWebEnginePage):
        """Forwards the page's own JS console output (fetch/tile errors, blocked
        requests, CSP violations, etc.) to the QGIS Python console. This is usually
        the only way to see *why* a map failed to render -- a broken tile request
        or a blocked script doesn't raise anything at the Qt/Python level, it just
        shows up as a console.error() inside the page."""
        def javaScriptConsoleMessage(self, level, message, line, source):
            print(f"[QuickMapLink][JS] {message} (line {line} in {source})")

except ImportError:
    from qgis.PyQt.QtWebKitWidgets import QWebView as WebView, QWebPage  # Fallback for QGIS < 3.6 / no QtWebEngine
    USING_WEBENGINE = False

    class _DiagnosticWebPage(QWebPage):
        """Same idea as _DiagnosticWebEnginePage above, for the legacy QtWebKit engine
        (older signature: no severity level)."""
        def javaScriptConsoleMessage(self, message, line, source):
            print(f"[QuickMapLink][JS] {message} (line {line} in {source})")

from qgis.PyQt.QtWidgets import QAction, QVBoxLayout, QDialog, QComboBox, QLabel, \
    QPushButton, QToolBar, QCheckBox, QDockWidget, QMessageBox
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

# Bing: known to hard-crash QGIS in the embedded webview on some systems (see the
# QtWebEngine GPU-crash notes below). Apple: unreliable/inconsistent when rendered in
# an embedded Chromium view. Both are browser-only -- Webview isn't offered for them.
PROVIDERS_WITHOUT_WEBVIEW = {"Bing Maps", "Apple Maps"}

# Providers confirmed broken specifically under the legacy QtWebKit fallback (i.e. only
# when QtWebEngine isn't available at all) -- their JS bundle uses syntax that engine's
# frozen JS parser can't handle. OSM confirmed via JS console forwarding: a SyntaxError
# on object-spread syntax ('...') in its main bundle. Google Maps, OpenTopoMap, and
# Wikimedia Maps have all been confirmed to render fine on the same QtWebKit fallback,
# so this is deliberately narrow rather than disabling Webview for everything.
PROVIDERS_BROKEN_ON_WEBKIT = {"OpenStreetMap"}

# Empirical correction: Apple Maps' "z" URL parameter renders more zoomed-out than the
# same numeric value on Google/Bing/OSM at the shared zoom estimate, so it undershoots
# how zoomed-in the QGIS view actually is. See _build_apple_url.
APPLE_MAPS_ZOOM_OFFSET = 2

# Browser-follow can't update an already-open tab in place (webbrowser.open() always
# risks a new tab), so it's throttled much harder than the webview: a long debounce
# before it refreshes at all. Webview follow doesn't have this problem (setUrl() updates
# in place) so it stays responsive.
BROWSER_FOLLOW_DEBOUNCE_MS = 1000


def _strip_experimental_marker(map_type):
    # Settings combo shows "Bing Maps *" / "Apple Maps *" (the "*" just flags them as
    # experimental); strip it so it matches the plain provider name used everywhere else.
    return (map_type or "Google Maps").replace("*", "").strip()


def _webview_disabled(provider):
    """True if Webview mode shouldn't be offered for this provider at all."""
    if provider in PROVIDERS_WITHOUT_WEBVIEW:
        return True
    if not USING_WEBENGINE and provider in PROVIDERS_BROKEN_ON_WEBKIT:
        return True
    return False


class QuickMapLinkPlugin:
    def __init__(self, iface: QgisInterface):
        self.iface = iface
        # The one QuickMapLink action: same icon as before, used in both the toolbar
        # and the Plugins menu, and it simply opens Map Settings. The old separate
        # on/off toolbar toggle is gone -- that switch now lives inside the settings
        # dialog itself (see open_settings_dialog) so there's only one icon to find.
        self.settings_action = QAction(QIcon(":/plugins/quick_map_link/icon.png"),
                                        "QuickMapLink", self.iface.mainWindow())
        self.settings_action.setToolTip("Open Quick Map Link settings")
        self.settings_action.triggered.connect(self.open_settings_dialog)

        # Load the default map type / open mode / basemap style / overlay layer / enabled
        # state from settings
        self.settings = QSettings("MyOrganization", "QuickMapsLink Settings")
        self.map_type = self.settings.value("map_type", "Google Maps")
        self.open_mode = self.settings.value("open_mode", "Webview")  # "Webview" or "Browser"
        self.basemap_style = self.settings.value("basemap_style", "Roadmap")
        self.overlay_layer = self.settings.value("overlay_layer", "None")
        self.context_menu_enabled = self.settings.value("context_menu_enabled", True, type=bool)

        # --- Live "follow the view finder" state ---
        self.window = None          # internal webview window (tracked so we know if it's open)
        self.webview = None
        self.browser_follow_active = False  # whether browser-follow mode is currently active
        self._last_browser_location = None  # (lat, lon) last actually opened, for the move-threshold check

        # Webview follow: short debounce, coalesces rapid extentsChanged signals (e.g.
        # while dragging the canvas) so we only push an update shortly after settling.
        self._webview_follow_timer = QTimer()
        self._webview_follow_timer.setSingleShot(True)
        self._webview_follow_timer.setInterval(400)  # ms after the canvas view stops moving
        self._webview_follow_timer.timeout.connect(self._on_webview_follow_settled)

        # Browser follow: much longer debounce, since every refresh risks a new tab.
        self._browser_follow_timer = QTimer()
        self._browser_follow_timer.setSingleShot(True)
        self._browser_follow_timer.setInterval(BROWSER_FOLLOW_DEBOUNCE_MS)
        self._browser_follow_timer.timeout.connect(self._on_browser_follow_settled)

    def initGui(self):
        # Add directly to the Plugins menu itself (rather than addPluginToMenu, which
        # would wrap a single action in its own "QuickMapLink" submenu) so it's one
        # directly-clickable item, consistent with "Python Console" etc. above it.
        self.iface.pluginMenu().addAction(self.settings_action)

        # Add our entry directly into QGIS's own native canvas right-click menu (the one
        # that already has "Copy Coordinate" etc.), instead of showing a second, separate
        # popup -- a second popup is what caused the "close the first menu to get to ours"
        # behavior on macOS: the canvas already reacts to right-click on its own, and a
        # widget-level customContextMenuRequested handler just queued a second menu after it.
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._populate_context_menu)

        # Follow the QGIS view finder: fires on every pan/zoom/rotate of the canvas
        self.iface.mapCanvas().extentsChanged.connect(self._on_canvas_extents_changed)

        # Add the single toolbar button
        self.toolbar = self.iface.addToolBar("QuickMapLink")
        self.toolbar.addAction(self.settings_action)

    def unload(self):
        self.iface.pluginMenu().removeAction(self.settings_action)
        self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._populate_context_menu)
        self.iface.mapCanvas().extentsChanged.disconnect(self._on_canvas_extents_changed)

        if self.window is not None:
            self.iface.removeDockWidget(self.window)
            self.window = None
            self.webview = None

        # Remove toolbar button
        self.iface.removeToolBarIcon(self.settings_action)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        del self.toolbar

    # ------------------------------------------------------------------
    # Live-follow: react to the QGIS view finder moving
    # ------------------------------------------------------------------
    def _on_canvas_extents_changed(self):
        # Two independent debounces: the webview updates in place (cheap, stays snappy),
        # browser-follow risks a new tab per refresh (throttled much harder, see
        # BROWSER_FOLLOW_DEBOUNCE_MS).
        if self.window is not None and self.window.isVisible():
            self._webview_follow_timer.start()
        if self.browser_follow_active:
            self._browser_follow_timer.start()

    def _on_webview_follow_settled(self):
        if self.window is None or not self.window.isVisible():
            return
        latitude, longitude = self.get_canvas_location()
        url = self.build_map_url(latitude, longitude)
        print(f"[QuickMapLink] Following webview to: {url}")
        self.webview.setUrl(QUrl(url))

    def _on_browser_follow_settled(self):
        if not self.browser_follow_active:
            return

        latitude, longitude = self.get_canvas_location()
        self._last_browser_location = (latitude, longitude)
        url = self.build_map_url(latitude, longitude)
        print(f"[QuickMapLink] Following browser to: {url}")
        webbrowser.open(url)

    def toggle_browser_follow(self, checked):
        if checked:
            # Live-follow means a new browser tab opens every time the QGIS view moves
            # (browsers can't be updated in place from Python) -- confirm before turning
            # it on rather than surprising the user with a flood of tabs.
            seconds = BROWSER_FOLLOW_DEBOUNCE_MS / 1000
            confirm = QMessageBox.warning(
                self.iface.mainWindow(),
                "Enable live follow in browser?",
                f"While live follow is on, moving the QGIS view will open a new browser "
                f"tab (at most about once every {seconds:g}s).\n\nEnable live follow?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return  # Declined -- leave follow off (menu rebuilds unchecked next time)

            point = getattr(self, "context_point", None)
            self.open_google_maps_in_browser(point)
            self.browser_follow_active = True
            self._last_browser_location = self.get_canvas_location(point)
        else:
            self.browser_follow_active = False
            self._last_browser_location = None

    def open_google_maps_context(self):
        self.open_google_maps_at_location(self.context_point)

    def open_google_maps_context_browser(self):
        # Plain one-shot snapshot -- does not touch browser_follow_active.
        self.open_google_maps_in_browser(self.context_point)

    def _populate_context_menu(self, menu, event):
        """Add QuickMapLink's single "Open in <provider>" entry directly into QGIS's
        own native canvas right-click menu (see initGui for why), reflecting whichever
        provider and open-mode (Webview/Browser) are currently set in Map Settings."""
        if not self.context_menu_enabled:
            return  # Disabled via the checkbox in Map Settings

        self.context_point = event  # has .x()/.y(), same as the QPoint it replaces
        provider = self._normalized_map_type()
        # Bing/Apple are browser-only regardless of the saved "Open in" setting; OSM is
        # also browser-only specifically when QtWebEngine isn't available (see
        # PROVIDERS_BROKEN_ON_WEBKIT) -- everything else works fine on either engine.
        effective_open_mode = "Browser" if _webview_disabled(provider) else self.open_mode

        menu.addSeparator()
        if effective_open_mode == "Browser":
            # Default behavior is a one-off snapshot -- opening a tab per click, not per
            # QGIS view move. Live-follow is a separate, explicit opt-in (see
            # toggle_browser_follow) since it means a new tab every time the view moves.
            open_action = menu.addAction(f"Open in {provider} (Browser)")
            open_action.triggered.connect(self.open_google_maps_context_browser)

            follow_label = f"Stop Live Follow ({provider}, Browser)" if self.browser_follow_active \
                else f"Live Follow ({provider}, Browser)"
            follow_action = menu.addAction(follow_label)
            follow_action.setCheckable(True)
            follow_action.setChecked(self.browser_follow_active)
            follow_action.triggered.connect(self.toggle_browser_follow)
        else:
            action = menu.addAction(f"Open in {provider} (Webview)")
            action.triggered.connect(self.open_google_maps_context)

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
        # Apple's "z" doesn't follow the same standard-slippy-map scale Google/Bing/OSM's
        # zoom parameters do -- the same numeric value renders noticeably more zoomed-out
        # on Apple Maps, so nudge it in to actually match the QGIS view. Empirically tuned;
        # bump APPLE_MAPS_ZOOM_OFFSET further if it's still not zoomed in enough.
        zoom = round(max(2, min(21, zoom + APPLE_MAPS_ZOOM_OFFSET)))
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
        provider = self._normalized_map_type()
        if provider in PROVIDERS_WITHOUT_WEBVIEW:
            # Webview is disabled outright for these providers (Bing: known to hard-crash
            # QGIS on some systems; Apple: unreliable in an embedded Chromium view). The
            # settings dialog shouldn't let "Webview" be selected for them in the first
            # place, but fall back safely here too in case of stale/legacy saved settings.
            self.open_google_maps_in_browser(point)
            return

        if not USING_WEBENGINE and provider in PROVIDERS_BROKEN_ON_WEBKIT:
            # Confirmed via JS console forwarding: this provider's bundle uses syntax
            # (e.g. OSM's object-spread) that QtWebKit's frozen JS engine can't parse --
            # the page loads but silently fails to render anything. Google Maps,
            # OpenTopoMap, and Wikimedia Maps are all fine on this same fallback engine,
            # so only providers actually confirmed broken get redirected to the browser.
            print(f"[QuickMapLink] {provider} doesn't render on the QtWebKit fallback renderer "
                  f"(no QtWebEngine on this QGIS build). Opening in browser instead.")
            self.open_google_maps_in_browser(point)
            return

        if not USING_WEBENGINE:
            print(f"[QuickMapLink] QtWebEngine not found; using the legacy QtWebKit renderer "
                  f"for {provider}.")

        if self.window is None:
            # A QDockWidget instead of a plain floating window: it can be dragged to
            # any edge of the QGIS window and docked there, or dragged back out to
            # float, the same way QGIS's own Layers/Browser panels work. It starts
            # floating (like the old window did) but the user can pin it wherever
            # they like. Created once and reused on later opens, rather than piling
            # up a new window every time "Open in Webview" is used.
            self.window = QDockWidget("Quick Map Link (Webview)", self.iface.mainWindow())
            self.window.setObjectName("QuickMapLinkWebviewDock")  # lets QGIS remember its position
            self.window.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable
                                     | QDockWidget.DockWidgetFloatable)

            self.webview = WebView()  # QWebEngineView when available; QWebView as a last-resort fallback
            # A page subclass that forwards the page's own JS console output (fetch/tile
            # errors, CSP violations, etc.) to the QGIS Python console -- this is usually
            # the only way to see *why* a map failed to render, since the page itself won't
            # raise a Qt-level error for e.g. a blocked or failed tile request.
            if USING_WEBENGINE:
                self.webview.setPage(_DiagnosticWebEnginePage(self.webview))
            else:
                self.webview.setPage(_DiagnosticWebPage(self.webview))
            try:
                self.webview.loadStarted.connect(
                    lambda: print("[QuickMapLink] loadStarted"))
                self.webview.loadFinished.connect(self._on_webview_load_finished)
            except AttributeError:
                pass
            self.window.setWidget(self.webview)

            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.window)
            self.window.setFloating(True)
            self.window.resize(800, 600)

        # Show the dock (giving the webview its final size) BEFORE loading the URL.
        # Leaflet-based sites (OSM, OpenTopoMap, Wikimedia Maps) measure their container
        # size when the map initializes; if setUrl() runs first, the widget is still 0x0
        # (not yet laid out/shown), so the JS map inits at zero size and tiles never load
        # -- you just get a blank grey pane even though the page itself loaded fine.
        self.window.show()
        self.window.raise_()

        latitude, longitude = self.get_canvas_location(point)
        url = self.build_map_url(latitude, longitude)

        print(f"URL: {url}")
        self.webview.setUrl(QUrl(url))  # Create a QUrl object
        # From here on, _on_canvas_extents_changed will keep this dock in sync
        # with the QGIS view finder for as long as it stays open (self.window.isVisible()).

    def _on_webview_load_finished(self, ok):
        current_url = self.webview.url().toString() if self.webview is not None else "?"
        print(f"[QuickMapLink] loadFinished(ok={ok}) url={current_url}")
        self._nudge_map_resize(ok)

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

    def open_settings_dialog(self):
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("QuickMapLink Settings")
        layout = QVBoxLayout()

        # Enable/disable the right-click menu entry entirely (this used to be a separate
        # toolbar toggle; it lives here now so there's only one QuickMapLink icon). Placed
        # first since it gates whether everything below even applies.
        enabled_checkbox = QCheckBox("Enable")
        enabled_checkbox.setChecked(self.context_menu_enabled)
        layout.addWidget(enabled_checkbox)

        # Map Type Selection
        map_type_label = QLabel("Select web map provider:")
        layout.addWidget(map_type_label)
        map_type_combo = QComboBox()
        map_type_combo.addItems([
            "Google Maps", "Bing Maps", "Apple Maps",
            "OpenStreetMap", "OpenTopoMap", "Wikimedia Maps",
        ])
        map_type_combo.setCurrentText(self._normalized_map_type())
        layout.addWidget(map_type_combo)

        # Open Mode Selection -- determines what the single right-click menu entry does.
        # Options depend on the selected provider (some are browser-only).
        open_mode_label = QLabel("Open in:")
        layout.addWidget(open_mode_label)
        open_mode_combo = QComboBox()
        layout.addWidget(open_mode_combo)
        open_mode_note = QLabel()
        open_mode_note.setWordWrap(True)
        open_mode_note.setVisible(False)
        layout.addWidget(open_mode_note)

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

        # Everything above is moot while the entry itself is disabled -- grey it all out
        # rather than leaving it clickable but pointless.
        gated_widgets = [map_type_label, map_type_combo, open_mode_label, open_mode_combo,
                          open_mode_note, basemap_label, basemap_combo, overlay_label, overlay_combo]

        def apply_enabled_state(checked):
            for widget in gated_widgets:
                widget.setEnabled(checked)

        apply_enabled_state(enabled_checkbox.isChecked())
        enabled_checkbox.toggled.connect(apply_enabled_state)

        def refresh_provider_options(preferred_open_mode=None, preferred_basemap=None, preferred_overlay=None):
            provider = _strip_experimental_marker(map_type_combo.currentText())
            disabled = _webview_disabled(provider)
            open_modes = ["Browser"] if disabled else ["Webview", "Browser"]
            basemaps = PROVIDER_BASEMAPS.get(provider, BASEMAP_OPTIONS)
            overlays = PROVIDER_OVERLAYS.get(provider, OVERLAY_OPTIONS)

            open_mode_combo.blockSignals(True)
            open_mode_combo.clear()
            open_mode_combo.addItems(open_modes)
            open_mode_combo.setCurrentText(preferred_open_mode if preferred_open_mode in open_modes else open_modes[0])
            open_mode_combo.blockSignals(False)

            if provider in PROVIDERS_WITHOUT_WEBVIEW:
                open_mode_note.setText(f"* Webview isn't offered for {provider} (browser-only).")
                open_mode_note.setVisible(True)
            elif not USING_WEBENGINE and provider in PROVIDERS_BROKEN_ON_WEBKIT:
                open_mode_note.setText(
                    f"* Webview isn't offered for {provider}: this QGIS build has no QtWebEngine, "
                    f"and {provider}'s map doesn't render on the fallback renderer.")
                open_mode_note.setVisible(True)
            else:
                open_mode_note.setVisible(False)

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
        refresh_provider_options(self.open_mode, self.basemap_style, self.overlay_layer)
        map_type_combo.currentTextChanged.connect(lambda _: refresh_provider_options())

        # Save Button
        save_button = QPushButton("Save")
        save_button.clicked.connect(lambda: self.save_settings(
            map_type_combo.currentText(), open_mode_combo.currentText(),
            basemap_combo.currentText(), overlay_combo.currentText(),
            enabled_checkbox.isChecked(), dialog))
        layout.addWidget(save_button)

        dialog.setLayout(layout)
        dialog.exec_()

    def save_settings(self, map_type, open_mode, basemap_style, overlay_layer, context_menu_enabled, dialog):
        # Switching provider or open mode invalidates any in-progress browser-follow session.
        self.browser_follow_active = False

        self.map_type = map_type
        self.open_mode = open_mode
        self.basemap_style = basemap_style
        self.overlay_layer = overlay_layer
        self.context_menu_enabled = context_menu_enabled
        self.settings.setValue("map_type", map_type)
        self.settings.setValue("open_mode", open_mode)
        self.settings.setValue("basemap_style", basemap_style)
        self.settings.setValue("overlay_layer", overlay_layer)
        self.settings.setValue("context_menu_enabled", context_menu_enabled)
        dialog.close()

        # If the webview is already open, refresh it immediately with the new provider/
        # basemap/overlay instead of waiting for the next canvas pan/zoom to trigger it.
        if self.window is not None and self.window.isVisible():
            self._on_webview_follow_settled()


def classFactory(iface):
    return QuickMapLinkPlugin(iface)
