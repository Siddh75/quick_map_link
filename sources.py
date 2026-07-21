"""Map-source logic for QuickMapLink: the fixed list of "web map providers" (external
consumer map websites opened in a webview or the system browser) and "tile basemaps"
(public XYZ/WMS-tile raster services rendered natively as a QgsRasterLayer, no webview
involved), plus the provider URL builders and coordinate/zoom helpers they depend on.

Kept separate from quick_map_link.py (which holds the Qt/QGIS widget/plugin classes) so
adding or tweaking a source doesn't require touching the UI code, and vice versa.
Deliberately duplicated from the equivalent module in the QuickMapCompare plugin rather
than shared -- these are independent plugins, not a shared codebase.
"""

import math
import urllib.parse

from qgis.core import (
    QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRasterLayer,
)

# ----------------------------------------------------------------------
# Web map providers -- external consumer map websites, opened via WebView
# (QWebEngineView/QWebView) or the system browser.
# ----------------------------------------------------------------------

PROVIDERS = ["Google Maps", "Bing Maps", "Apple Maps", "OpenStreetMap", "OpenTopoMap", "Wikimedia Maps"]

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
# QtWebEngine GPU-crash notes in quick_map_link.py). Apple: unreliable/inconsistent
# when rendered in an embedded Chromium view. Both are browser-only -- Webview isn't
# offered for them.
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


def strip_experimental_marker(map_type):
    # Legacy compatibility: older saved settings could have "Bing Maps *" / "Apple
    # Maps *" (the "*" used to flag them as experimental in the combo box); strip it
    # so it still matches the plain provider name used everywhere else.
    return (map_type or "Google Maps").replace("*", "").strip()


def build_provider_url(provider, basemap_style, overlay_layer, latitude, longitude, zoom):
    if provider == "Bing Maps":
        return _build_bing_url(latitude, longitude, zoom, basemap_style, overlay_layer)
    elif provider == "Apple Maps":
        return _build_apple_url(latitude, longitude, zoom, basemap_style, overlay_layer)
    elif provider == "OpenStreetMap":
        return _build_osm_url(latitude, longitude, zoom, overlay_layer)
    elif provider == "OpenTopoMap":
        return _build_opentopomap_url(latitude, longitude, zoom)
    elif provider == "Wikimedia Maps":
        return _build_wikimedia_url(latitude, longitude, zoom)
    else:
        return _build_google_url(latitude, longitude, zoom, basemap_style, overlay_layer)  # default


def _build_google_url(latitude, longitude, zoom, basemap_style, overlay_layer):
    # https://developers.google.com/maps/documentation/urls/get-started#map-action
    basemap = {"Roadmap": "roadmap", "Satellite": "satellite", "Terrain": "terrain"}.get(
        basemap_style, "roadmap")
    layer = {"None": "none", "Traffic": "traffic", "Transit": "transit", "Bicycling": "bicycling"}.get(
        overlay_layer, "none")
    zoom = round(max(0, min(21, zoom)))
    return (f"https://www.google.com/maps/@?api=1&map_action=map&center={latitude},{longitude}"
            f"&zoom={zoom}&basemap={basemap}&layer={layer}")


def _build_bing_url(latitude, longitude, zoom, basemap_style, overlay_layer):
    # https://learn.microsoft.com/en-us/bingmaps/articles/create-a-custom-map-url
    # style: r=road, a=aerial (satellite), h=aerial with labels, o/b=bird's eye.
    # Bing has no terrain style, so it falls back to road.
    style = {"Roadmap": "r", "Satellite": "a", "Terrain": "r"}.get(basemap_style, "r")
    traffic = "1" if overlay_layer == "Traffic" else "0"
    zoom = round(max(1, min(20, zoom)))
    return (f"https://bing.com/maps/default.aspx?cp={latitude}~{longitude}"
            f"&lvl={zoom}&style={style}&trfc={traffic}")


def _build_apple_url(latitude, longitude, zoom, basemap_style, overlay_layer):
    # https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/MapLinks/MapLinks.html
    # t: m=standard, k=satellite, h=hybrid (deprecated), r=transit view.
    # Apple has no terrain style and no separate traffic/bicycling overlay.
    if overlay_layer == "Transit":
        map_type = "r"
    else:
        map_type = {"Roadmap": "m", "Satellite": "k", "Terrain": "m"}.get(basemap_style, "m")
    # Apple's "z" doesn't follow the same standard-slippy-map scale Google/Bing/OSM's
    # zoom parameters do -- nudge it in so the opened map actually matches the QGIS view.
    zoom = round(max(2, min(21, zoom + APPLE_MAPS_ZOOM_OFFSET)))
    return f"https://maps.apple.com/?ll={latitude},{longitude}&z={zoom}&t={map_type}"


def _build_osm_url(latitude, longitude, zoom, overlay_layer):
    # https://wiki.openstreetmap.org/wiki/Browsing
    zoom = round(max(0, min(19, zoom)))
    url = f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map={zoom}/{latitude}/{longitude}"
    if overlay_layer == "Bicycling":
        url += "&layers=C"
    elif overlay_layer == "Transit":
        url += "&layers=H"  # closest available match: Humanitarian OSM Team style
    return url


def _build_opentopomap_url(latitude, longitude, zoom):
    # https://opentopomap.org -- single topographic/contour style.
    zoom = round(max(0, min(17, zoom)))  # OpenTopoMap tiles top out around z17
    return f"https://opentopomap.org/#map={zoom}/{latitude}/{longitude}"


def _build_wikimedia_url(latitude, longitude, zoom):
    # https://maps.wikimedia.org -- single default OSM-based style.
    zoom = round(max(0, min(18, zoom)))
    return f"https://maps.wikimedia.org/#{zoom}/{latitude}/{longitude}"


# ----------------------------------------------------------------------
# Tile basemaps -- public XYZ/WMS-tile raster services, rendered natively via a
# QgsRasterLayer inside a QgsMapCanvas rather than a webview. Unlike the providers
# above, these aren't websites: there's no consumer page to load, no zoom-estimation
# math needed for rendering (QGIS's own raster/reprojection handling deals with CRS
# and extent directly, same as any other layer), and no Browser-mode equivalent
# (browsing to a single tile URL just shows one raw tile image, not an interactive
# map). Curated from public, no-API-key-required services -- adding a private/
# authenticated WMS here would mean forwarding credentials outside QGIS's own auth
# handling, which this deliberately avoids.
#
# Three entries below are deliberately renamed from their "obvious" name to avoid
# colliding with an identically-named *web map provider* above that behaves quite
# differently (a real webview page vs. a native tile layer): "OpenStreetMap" ->
# "OpenStreetMap (tiles)", "Google Maps" -> "Google Maps (tiles)", "Bing Maps" ->
# "Bing Maps (Aerial tiles)".
# ----------------------------------------------------------------------

TILE_BASEMAPS = {
    "Esri World Hillshade": {
        "styles": {
            "Standard": "https://services.arcgisonline.com/arcgis/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 16,
    },
    "Esri World Topographic": {
        "styles": {
            "Standard": "https://services.arcgisonline.com/arcgis/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 19,
    },
    "USGS Topo": {
        "styles": {
            "Standard": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 16,
    },
    "OpenStreetMap (tiles)": {
        "styles": {
            "Standard": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        },
        "zmax": 19,
    },
    "CyclOSM": {
        "styles": {
            "Standard": "https://a.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
        },
        "zmax": 20,
    },
    "Google Maps (tiles)": {
        "styles": {
            "Roadmap": "https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}",
            "Satellite": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
            "Terrain": "https://mt1.google.com/vt/lyrs=t&x={x}&y={y}&z={z}",
            "Hybrid": "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        },
        "zmax": 20,
    },
    "CartoDB": {
        "styles": {
            "Light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            "Dark": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
            "Voyager": "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        },
        "zmax": 20,
    },
    "Esri Ocean": {
        "styles": {
            "Standard": "https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 13,
    },
    "Esri NatGeo": {
        "styles": {
            "Standard": "https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 16,
    },
    "Esri Light Gray": {
        "styles": {
            "Standard": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        },
        "zmax": 16,
    },
    # Uses Bing's quadkey tile addressing ("{q}") rather than {z}/{x}/{y} -- QGIS's
    # XYZ tile source supports this token natively. "a" in the URL path selects
    # Bing's aerial imagery layer specifically.
    "Bing Maps (Aerial tiles)": {
        "styles": {
            "Standard": "http://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1",
        },
        "zmax": 19,
    },
}


def is_tile_basemap(name):
    return name in TILE_BASEMAPS


def basemap_styles(name):
    """List of style names for a TILE_BASEMAPS entry, in definition order."""
    return list(TILE_BASEMAPS.get(name, {}).get("styles", {}).keys())


def make_xyz_raster_layer(name, style=None):
    """Build a QgsRasterLayer for one of the built-in TILE_BASEMAPS entries, using
    the given style name (falls back to the entry's first/only style if the given
    one isn't found or not given). QGIS's "wms" provider handles plain XYZ tile URLs
    via a "type=xyz&url=..." data source string, parsed as its own key=value&key=value
    scheme -- so the *entire* tile URL value needs to be percent-encoded (matching
    what QGIS's own "Add XYZ Layer" dialog generates, e.g. ".../%7Bz%7D/%7Bx%7D/%7By%7D.png"),
    not just the "&" in query-string-based tile URLs. QGIS percent-decodes the value
    back before substituting {x}/{y}/{z}/{q}, so the placeholders still work correctly
    once round-tripped -- but leaving "{"/"}" (or "=", "&") unencoded breaks the outer
    parsing for every entry, not just the query-string-based ones, since every URL here
    contains at least one "{...}" token."""
    config = TILE_BASEMAPS.get(name)
    if config is None:
        return None

    styles = config["styles"]
    url = styles.get(style) if style in styles else next(iter(styles.values()))
    url = urllib.parse.quote(url, safe=":/")
    zmax = config.get("zmax", 19)
    uri = f"type=xyz&url={url}&zmax={zmax}&zmin=0"
    return QgsRasterLayer(uri, name, "wms")


# ----------------------------------------------------------------------
# Coordinate / zoom helpers, shared by provider URL building and the webview
# mouse-cursor overlay.
# ----------------------------------------------------------------------

def map_point_to_wgs84(map_point, project_crs):
    """Transform a QgsPointXY in project coordinates to (latitude, longitude) in WGS84."""
    if project_crs.authid() != "EPSG:4326":
        transform = QgsCoordinateTransform(
            project_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        map_point = transform.transform(map_point)
    return map_point.y(), map_point.x()


def estimate_zoom_level(canvas):
    """Approximate a web-mercator zoom level that matches how zoomed-in the given
    QGIS canvas's current view is, so an opened web map roughly matches what's
    visible in QGIS instead of always opening at a fixed zoom."""
    extent = canvas.extent()
    project_crs = QgsProject.instance().crs()

    if project_crs.authid() != "EPSG:4326":
        transform = QgsCoordinateTransform(
            project_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        extent = transform.transformBoundingBox(extent)

    width_deg = extent.width()
    canvas_width_px = canvas.mapSettings().outputSize().width() or 800

    if width_deg <= 0:
        return 15.0

    # At zoom z, a 256px tile covers 360 degrees / 2^z of longitude.
    return math.log2(360.0 * canvas_width_px / (256.0 * width_deg))


def mercator_pixel(latitude, longitude, zoom):
    """Standard spherical web-mercator pixel position (256px tiles) for a lat/lon at
    an integer zoom -- the same tiling scheme Google Maps and every Leaflet-based
    provider here use, so the *difference* between two such pixel positions is a
    reliable on-screen offset regardless of which provider is actually showing."""
    scale = 256 * (2 ** zoom)
    x = (longitude + 180.0) / 360.0 * scale
    lat_rad = math.radians(max(min(latitude, 85.05112878), -85.05112878))
    merc_y = math.log(math.tan(math.pi / 4 + lat_rad / 2))
    y = (0.5 - merc_y / (2 * math.pi)) * scale
    return x, y
