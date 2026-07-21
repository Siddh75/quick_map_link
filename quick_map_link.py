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
    HAS_WEBVIEW = True
except ImportError:
    try:
        # Legacy fallback for old Qt5-based QGIS builds compiled without QtWebEngine.
        # QtWebKitWidgets does not exist at all under Qt6 (Qt dropped WebKit outright),
        # so this raises ImportError there too -- caught below rather than left to crash
        # plugin load entirely.
        from qgis.PyQt.QtWebKitWidgets import QWebView as WebView
        USING_WEBENGINE = False
        HAS_WEBVIEW = True
    except ImportError:
        # Neither renderer is available on this QGIS build (most commonly a Qt6 build,
        # which has no QtWebKit and may not always ship QtWebEngine either). Degrade to
        # Browser-only mode everywhere a webview would otherwise be used -- see
        # _webview_disabled -- rather than crashing the whole plugin on import.
        WebView = None
        USING_WEBENGINE = False
        HAS_WEBVIEW = False

from qgis.PyQt.QtWidgets import QAction, QVBoxLayout, QDialog, QComboBox, QLabel, \
    QPushButton, QCheckBox, QDockWidget, QMessageBox, QWidget
from qgis.core import QgsProject
from qgis.gui import QgisInterface, QgsMapCanvas
from qgis.PyQt.QtCore import Qt, QUrl, QSettings, QTimer, QObject, QEvent, QPoint
from qgis.PyQt.QtGui import QIcon, QPainter, QColor, QPen
import webbrowser

from . import sources

ICON_PATH = os.path.join(os.path.dirname(__file__), "icon.png")

# Browser-follow can't update an already-open tab in place (webbrowser.open() always
# risks a new tab), so it's throttled much harder than the webview: a long debounce
# before it refreshes at all. Webview follow doesn't have this problem (setUrl() updates
# in place) so it stays responsive.
BROWSER_FOLLOW_DEBOUNCE_MS = 1000


def _webview_disabled(provider):
    """True if Webview mode shouldn't be offered for this provider at all. Only
    meaningful for web map providers -- tile basemaps (sources.is_tile_basemap)
    are handled separately since Browser mode never applies to them at all."""
    if not HAS_WEBVIEW:
        return True
    if provider in sources.PROVIDERS_WITHOUT_WEBVIEW:
        return True
    if not USING_WEBENGINE and provider in sources.PROVIDERS_BROKEN_ON_WEBKIT:
        return True
    return False


class _DockGeometryWatcher(QObject):
    """Relays Move/Resize events on the webview dock back to a save callback.
    QDockWidget doesn't expose signals for these, so an event filter is the standard
    Qt way to observe them -- and the filter target must be a QObject, which the
    plugin class itself isn't, hence this small standalone helper."""
    def __init__(self, save_callback, parent=None):
        super().__init__(parent)
        self._save_callback = save_callback

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Move, QEvent.Resize):
            self._save_callback()
        return False


class _CursorOverlay(QWidget):
    """A small crosshair drawn on top of the webview, repositioned to track where
    the QGIS canvas mouse cursor currently maps to on the embedded web map.

    This is a Qt-side overlay widget only -- it doesn't (and can't) reach into the
    third-party map page's own DOM/JS, since we're just loading Google Maps/OSM/etc.
    as a plain page, not driving them via their JS APIs. Its position is computed
    from standard web-mercator math against whatever center/zoom the plugin last
    set on the page (see QuickMapLinkPlugin._webview_map_center/_webview_map_zoom),
    so it stays accurate as long as that's still what's actually displayed -- if the
    user manually pans/zooms the embedded map themselves, this will drift out of
    sync until the next refresh (e.g. the next live-follow update)."""
    _SIZE = 18

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # clicks/drags pass through to the map
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = self._SIZE // 2
        radius = 5

        pen = QPen(QColor(255, 0, 0, 230))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QColor(255, 0, 0, 60))
        painter.drawEllipse(QPoint(center, center), radius, radius)
        painter.drawLine(center - 8, center, center - radius - 1, center)
        painter.drawLine(center + radius + 1, center, center + 8, center)
        painter.drawLine(center, center - 8, center, center - radius - 1)
        painter.drawLine(center, center + radius + 1, center, center + 8)


class QuickMapLinkPlugin:
    def __init__(self, iface: QgisInterface):
        self.iface = iface
        # The one QuickMapLink action: same icon as before, used in both the toolbar
        # and the Plugins menu, and it simply opens Map Settings. The old separate
        # on/off toolbar toggle is gone -- that switch now lives inside the settings
        # dialog itself (see open_settings_dialog) so there's only one icon to find.
        self.settings_action = QAction(QIcon(ICON_PATH),
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
        self.window = None          # internal dock widget (tracked so we know if it's open)
        self.webview = None         # WebView instance, when the dock is showing a web map provider
        self.map_canvas = None      # QgsMapCanvas instance, when the dock is showing a tile basemap
        self._tile_layer = None     # QgsRasterLayer currently shown in map_canvas -- kept alive here since
                                     # setLayers() doesn't itself hold a Python reference; without this the
                                     # layer gets garbage-collected right after this method returns, leaving
                                     # the canvas pointing at a dead layer (renders blank, no error anywhere)
        self._content_kind = None   # "provider" or "tile" -- whichever of the above is currently installed
        self._dock_geometry_watcher = None  # watches self.window for move/resize, see _save_webview_dock_state
        self.browser_follow_active = False  # whether browser-follow mode is currently active
        self._last_browser_location = None  # (lat, lon) last actually opened, for the move-threshold check

        # --- Webview mouse-cursor overlay state ---
        self._cursor_overlay = None       # the _CursorOverlay widget, created alongside the webview
        self._webview_map_center = None   # (lat, lon) last set as the webview's URL center
        self._webview_map_zoom = None     # integer zoom last used for that URL

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

        # Mirror the canvas mouse position onto the webview (see _on_canvas_mouse_moved).
        # xyCoordinates already gives us the position in map (project) coordinates, same
        # signal QGIS's own status bar coordinate display uses.
        self.iface.mapCanvas().xyCoordinates.connect(self._on_canvas_mouse_moved)

        # Add the single toolbar button
        self.toolbar = self.iface.addToolBar("QuickMapLink")
        self.toolbar.addAction(self.settings_action)

    def unload(self):
        self.iface.pluginMenu().removeAction(self.settings_action)
        self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._populate_context_menu)
        self.iface.mapCanvas().extentsChanged.disconnect(self._on_canvas_extents_changed)
        self.iface.mapCanvas().xyCoordinates.disconnect(self._on_canvas_mouse_moved)

        if self.window is not None:
            self._save_webview_dock_state()
            self.iface.removeDockWidget(self.window)
            self.window = None
            self.webview = None
            self.map_canvas = None
            self._tile_layer = None
            self._content_kind = None
            self._cursor_overlay = None

        # Remove toolbar button
        self.iface.removeToolBarIcon(self.settings_action)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        del self.toolbar

    # ------------------------------------------------------------------
    # Webview dock persistence: remember where it was left (docked to which
    # side, or floating, and at what size/position) across QGIS restarts.
    # ------------------------------------------------------------------
    def _save_webview_dock_state(self, *_args):
        if self.window is None:
            return
        self.settings.setValue("webview/floating", self.window.isFloating())
        self.settings.setValue(
            "webview/dock_area", int(self.iface.mainWindow().dockWidgetArea(self.window)))
        self.settings.setValue("webview/geometry", self.window.saveGeometry())

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
        if self._content_kind == "tile":
            # No lat/lon/zoom URL involved -- just mirror the QGIS canvas extent
            # directly onto the native tile-basemap canvas, same coordinate space.
            self._refresh_tile_canvas()
            return
        latitude, longitude = self.get_canvas_location()
        url = self.build_map_url(latitude, longitude)
        self._remember_webview_map_view(latitude, longitude)
        self.webview.setUrl(QUrl(url))

    def _on_browser_follow_settled(self):
        if not self.browser_follow_active:
            return

        latitude, longitude = self.get_canvas_location()
        self._last_browser_location = (latitude, longitude)
        url = self.build_map_url(latitude, longitude)
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
            label = f"Open in {provider}" if sources.is_tile_basemap(provider) else f"Open in {provider} (Webview)"
            action = menu.addAction(label)
            action.triggered.connect(self.open_google_maps_context)

    # ------------------------------------------------------------------
    # Coordinate / URL helpers (shared by webview, browser, and follow-mode)
    # ------------------------------------------------------------------
    def get_canvas_location(self, point=None):
        """Return (latitude, longitude) in WGS84 for a given canvas-widget
        pixel point, or for the current canvas center if no point is given."""
        if point:
            map_point = self.iface.mapCanvas().getCoordinateTransform().toMapCoordinates(point.x(), point.y())
        else:
            map_point = self.iface.mapCanvas().extent().center()

        return self._map_point_to_wgs84(map_point)

    def _map_point_to_wgs84(self, map_point):
        """Transform a QgsPointXY already in map (project) coordinates to
        (latitude, longitude) in WGS84 -- shared by get_canvas_location (pixel-based)
        and _on_canvas_mouse_moved (already map-coordinate-based, via xyCoordinates)."""
        return sources.map_point_to_wgs84(map_point, QgsProject.instance().crs())

    def estimate_zoom_level(self):
        """Approximate a web-mercator zoom level that matches how zoomed-in the
        current QGIS canvas view is, so the opened map roughly matches what's
        visible in QGIS instead of always opening at a fixed zoom."""
        return sources.estimate_zoom_level(self.iface.mapCanvas())

    def _normalized_map_type(self):
        return sources.strip_experimental_marker(self.map_type)

    def build_map_url(self, latitude, longitude):
        zoom = self.estimate_zoom_level()
        provider = self._normalized_map_type()
        return sources.build_provider_url(
            provider, self.basemap_style, self.overlay_layer, latitude, longitude, zoom)

    def open_google_maps_at_location(self, point=None):
        provider = self._normalized_map_type()

        if sources.is_tile_basemap(provider):
            # Tile basemaps render natively (QgsMapCanvas), not through a webview or
            # browser at all -- see _open_tile_basemap.
            self._open_tile_basemap(provider)
            return

        if not HAS_WEBVIEW:
            # This QGIS build has no usable embedded-webview renderer at all (neither
            # QtWebEngine nor the legacy QtWebKit -- typically a Qt6 build). Every web
            # provider falls back to the system browser; tile basemaps above are
            # unaffected since they never touch WebView at all.
            self.open_google_maps_in_browser(point)
            return

        if provider in sources.PROVIDERS_WITHOUT_WEBVIEW:
            # Webview is disabled outright for these providers (Bing: known to hard-crash
            # QGIS on some systems; Apple: unreliable in an embedded Chromium view). The
            # settings dialog shouldn't let "Webview" be selected for them in the first
            # place, but fall back safely here too in case of stale/legacy saved settings.
            self.open_google_maps_in_browser(point)
            return

        if not USING_WEBENGINE and provider in sources.PROVIDERS_BROKEN_ON_WEBKIT:
            # Confirmed via JS console forwarding: this provider's bundle uses syntax
            # (e.g. OSM's object-spread) that QtWebKit's frozen JS engine can't parse --
            # the page loads but silently fails to render anything. Google Maps,
            # OpenTopoMap, and Wikimedia Maps are all fine on this same fallback engine,
            # so only providers actually confirmed broken get redirected to the browser.
            self.open_google_maps_in_browser(point)
            return

        self._ensure_dock()
        self._ensure_provider_webview()
        self.window.setWindowTitle(f"Quick Map Link — {provider} (Webview)")

        # Show the dock (giving the webview its final size) BEFORE loading the URL.
        # Leaflet-based sites (OSM, OpenTopoMap, Wikimedia Maps) measure their container
        # size when the map initializes; if setUrl() runs first, the widget is still 0x0
        # (not yet laid out/shown), so the JS map inits at zero size and tiles never load
        # -- you just get a blank grey pane even though the page itself loaded fine.
        self.window.show()
        self.window.raise_()

        latitude, longitude = self.get_canvas_location(point)
        url = self.build_map_url(latitude, longitude)
        self._remember_webview_map_view(latitude, longitude)

        self.webview.setUrl(QUrl(url))  # Create a QUrl object
        # From here on, _on_canvas_extents_changed will keep this dock in sync
        # with the QGIS view finder for as long as it stays open (self.window.isVisible()).

    def _open_tile_basemap(self, name):
        self._ensure_dock()
        self._ensure_tile_canvas()
        self.window.setWindowTitle(f"Quick Map Link — {name}")

        self.window.show()
        self.window.raise_()
        self._refresh_tile_canvas(name)

    def _refresh_tile_canvas(self, name=None):
        if self.map_canvas is None:
            return
        name = name or self._normalized_map_type()
        available_styles = sources.basemap_styles(name)
        style = self.basemap_style if self.basemap_style in available_styles else None
        layer = sources.make_xyz_raster_layer(name, style)
        if layer is None or not layer.isValid():
            return
        # Keep a reference on self -- setLayers() does not itself keep the Python
        # wrapper alive, so without this the layer can be garbage-collected right
        # after this call, leaving the canvas pointing at a dead layer (blank,
        # silent -- no error surfaces anywhere).
        self._tile_layer = layer
        self.map_canvas.setLayers([layer])
        self.map_canvas.setExtent(self.iface.mapCanvas().extent())
        self.map_canvas.refresh()

    # ------------------------------------------------------------------
    # Dock/content management: the single dock widget can hold either a WebView
    # (web map provider) or a QgsMapCanvas (tile basemap) as its content, swapped
    # out as needed if the user switches between the two kinds of source -- see
    # _ensure_provider_webview / _ensure_tile_canvas.
    # ------------------------------------------------------------------
    def _ensure_dock(self):
        """Create the dock widget shell itself (position/size persistence included)
        if it doesn't exist yet. Its content is managed separately, since it can
        change between a WebView and a QgsMapCanvas depending on the selected source."""
        if self.window is not None:
            return

        # A QDockWidget instead of a plain floating window: it can be dragged to
        # any edge of the QGIS window and docked there, or dragged back out to
        # float, the same way QGIS's own Layers/Browser panels work. Created once
        # and reused on later opens, rather than piling up a new window every time
        # "Open in Webview" is used.
        self.window = QDockWidget("Quick Map Link", self.iface.mainWindow())
        self.window.setObjectName("QuickMapLinkWebviewDock")  # lets QGIS remember its position
        self.window.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable
                                 | QDockWidget.DockWidgetFloatable)

        # Restore whichever dock area, floating state, and size/position the dock
        # was left in last time (falls back to floating on the right, 800x600, the
        # first time it's ever opened / if nothing's been saved yet).
        saved_area = int(self.settings.value("webview/dock_area", Qt.RightDockWidgetArea))
        self.iface.addDockWidget(Qt.DockWidgetArea(saved_area), self.window)

        was_floating = self.settings.value("webview/floating", True, type=bool)
        self.window.setFloating(was_floating)

        saved_geometry = self.settings.value("webview/geometry")
        if saved_geometry is not None:
            self.window.restoreGeometry(saved_geometry)
        elif was_floating:
            self.window.resize(800, 600)

        # Persist the dock area / floating state as the user redocks or pops it back
        # out, and geometry whenever it moves or resizes, so it reopens the same way
        # next time -- including across QGIS restarts (unlike QGIS's own automatic
        # window-state restore, which only covers dock widgets that already exist at
        # startup, not ones a plugin creates lazily on first use).
        self.window.dockLocationChanged.connect(self._save_webview_dock_state)
        self.window.topLevelChanged.connect(self._save_webview_dock_state)
        self._dock_geometry_watcher = _DockGeometryWatcher(
            self._save_webview_dock_state, self.window)
        self.window.installEventFilter(self._dock_geometry_watcher)

    def _teardown_dock_content(self):
        """Remove whatever's currently installed in the dock (WebView or
        QgsMapCanvas) so a new one of a possibly-different kind can take its place."""
        if self.window is not None:
            self.window.setWidget(None)
        if self.webview is not None:
            self.webview.deleteLater()
            self.webview = None
        if self.map_canvas is not None:
            self.map_canvas.deleteLater()
            self.map_canvas = None
        self._tile_layer = None
        self._cursor_overlay = None
        self._content_kind = None

    def _ensure_provider_webview(self):
        """Make sure a WebView is installed in the dock, tearing down a tile-basemap
        canvas first if that's what's currently there."""
        if self._content_kind == "provider" and self.webview is not None:
            return
        self._teardown_dock_content()

        self.webview = WebView()  # QWebEngineView when available; QWebView as a last-resort fallback
        try:
            self.webview.loadFinished.connect(self._on_webview_load_finished)
        except AttributeError:
            pass
        self.window.setWidget(self.webview)

        # Floating crosshair that tracks the QGIS canvas mouse position onto the
        # embedded map (see _CursorOverlay and _on_canvas_mouse_moved). Tile
        # basemaps get their own copy of this in _ensure_tile_canvas.
        self._cursor_overlay = _CursorOverlay(self.webview)
        self._content_kind = "provider"


    def _ensure_tile_canvas(self):
        """Make sure a QgsMapCanvas is installed in the dock, tearing down a
        WebView first if that's what's currently there."""
        if self._content_kind == "tile" and self.map_canvas is not None:
            return
        self._teardown_dock_content()

        canvas = QgsMapCanvas()
        canvas.setCanvasColor(Qt.white)
        canvas.enableAntiAliasing(True)
        canvas.setDestinationCrs(QgsProject.instance().crs())
        self.map_canvas = canvas
        self.window.setWidget(self.map_canvas)

        # Same crosshair widget as the webview case, just parented to the map
        # canvas instead -- see _update_tile_cursor_overlay for why this one can
        # be positioned exactly rather than estimated.
        self._cursor_overlay = _CursorOverlay(self.map_canvas)
        self._content_kind = "tile"

    def _on_webview_load_finished(self, ok):
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

        webbrowser.open(url)

    # ------------------------------------------------------------------
    # Webview mouse-cursor overlay: mirror the QGIS canvas mouse position onto
    # the embedded map. See _CursorOverlay for why this is Qt-side math against
    # a remembered center/zoom rather than anything driven by the page itself.
    # ------------------------------------------------------------------
    def _remember_webview_map_view(self, latitude, longitude):
        """Record the center/zoom just used to build the webview's URL, so the
        cursor overlay can work out where later mouse positions fall relative to
        it. Zoom is rounded/clamped the same way build_map_url's per-provider
        builders do, so it matches what's actually rendered closely enough."""
        self._webview_map_center = (latitude, longitude)
        self._webview_map_zoom = round(max(0, min(21, self.estimate_zoom_level())))

    def _on_canvas_mouse_moved(self, map_point):
        if self.window is None or not self.window.isVisible() or self._cursor_overlay is None:
            return

        if self._content_kind == "tile":
            self._update_tile_cursor_overlay(map_point)
        elif self._content_kind == "provider":
            self._update_webview_cursor_overlay(map_point)

    def _update_tile_cursor_overlay(self, map_point):
        """Position the crosshair on the tile-basemap QgsMapCanvas. Unlike the
        webview case, this canvas is a real QgsMapCanvas kept in sync with the
        main QGIS canvas's extent (see _refresh_tile_canvas), so its own
        map-to-pixel transform can be used directly -- exact, and with no
        remembered center/zoom to drift out of sync."""
        if self.map_canvas is None:
            return

        to_pixel = self.map_canvas.getCoordinateTransform()
        pixel_point = to_pixel.transform(map_point)
        x, y = pixel_point.x(), pixel_point.y()

        if x < 0 or y < 0 or x > self.map_canvas.width() or y > self.map_canvas.height():
            # Off the visible canvas area (can happen right at the edges, or
            # briefly while the extent is still catching up after a pan/zoom).
            self._cursor_overlay.hide()
            return

        size = self._cursor_overlay.width()
        self._cursor_overlay.move(round(x - size / 2), round(y - size / 2))
        self._cursor_overlay.show()
        self._cursor_overlay.raise_()

    def _update_webview_cursor_overlay(self, map_point):
        """Position the crosshair on the webview, computed from web-mercator math
        against whatever center/zoom the plugin last set on the page -- see
        _CursorOverlay for why (we don't control the third-party page's own
        rendering, so this is an estimate that can drift until the next refresh)."""
        if (self.webview is None or self._webview_map_center is None
                or self._webview_map_zoom is None):
            return

        latitude, longitude = self._map_point_to_wgs84(map_point)
        center_lat, center_lon = self._webview_map_center
        zoom = self._webview_map_zoom

        cx, cy = sources.mercator_pixel(center_lat, center_lon, zoom)
        px, py = sources.mercator_pixel(latitude, longitude, zoom)
        dx, dy = px - cx, py - cy

        half_w = self.webview.width() / 2
        half_h = self.webview.height() / 2
        if abs(dx) > half_w or abs(dy) > half_h:
            # The QGIS cursor currently maps to a point off the visible webview
            # area (or the two views have drifted out of sync) -- hide rather
            # than draw a marker outside the widget's own bounds.
            self._cursor_overlay.hide()
            return

        size = self._cursor_overlay.width()
        self._cursor_overlay.move(round(half_w + dx - size / 2), round(half_h + dy - size / 2))
        self._cursor_overlay.show()
        self._cursor_overlay.raise_()

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

        # Map Type Selection -- web map providers (opened via webview/browser) and
        # native tile basemaps (rendered directly, no webview) share one dropdown;
        # a separator line visually splits the two groups.
        map_type_label = QLabel("Select map provider:")
        layout.addWidget(map_type_label)
        map_type_combo = QComboBox()
        map_type_combo.addItems(sources.PROVIDERS)
        map_type_combo.insertSeparator(len(sources.PROVIDERS))
        map_type_combo.addItems(list(sources.TILE_BASEMAPS.keys()))
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
            provider = sources.strip_experimental_marker(map_type_combo.currentText())
            is_tile = sources.is_tile_basemap(provider)

            if is_tile:
                # Tile basemaps are a native map layer, not a web page -- there's no
                # browser-native way to view one interactively, so Webview (meaning
                # the dock's native QgsMapCanvas here, not a literal web view) is the
                # only option, and there's no separate overlay-layer concept at all.
                open_modes = ["Webview"]
                basemaps = sources.basemap_styles(provider)
                overlays = ["None"]
            else:
                disabled = _webview_disabled(provider)
                open_modes = ["Browser"] if disabled else ["Webview", "Browser"]
                basemaps = sources.PROVIDER_BASEMAPS.get(provider, sources.BASEMAP_OPTIONS)
                overlays = sources.PROVIDER_OVERLAYS.get(provider, sources.OVERLAY_OPTIONS)

            open_mode_combo.blockSignals(True)
            open_mode_combo.clear()
            open_mode_combo.addItems(open_modes)
            open_mode_combo.setCurrentText(preferred_open_mode if preferred_open_mode in open_modes else open_modes[0])
            open_mode_combo.blockSignals(False)

            if is_tile:
                open_mode_note.setText(
                    f"* {provider} is a native map layer, not a web page -- it opens in the "
                    f"dock directly (no separate Browser option).")
                open_mode_note.setVisible(True)
            elif not HAS_WEBVIEW:
                open_mode_note.setText(
                    "* Embedded webview isn't available on this QGIS build (no QtWebEngine "
                    "or QtWebKit found) -- opens in your system browser instead.")
                open_mode_note.setVisible(True)
            elif provider in sources.PROVIDERS_WITHOUT_WEBVIEW:
                open_mode_note.setText(f"* Webview isn't offered for {provider} (browser-only).")
                open_mode_note.setVisible(True)
            elif not USING_WEBENGINE and provider in sources.PROVIDERS_BROKEN_ON_WEBKIT:
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
