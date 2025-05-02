from qgis.PyQt.QtWebKitWidgets import QWebView  # Use QWebView for QGIS < 3.6
from qgis.PyQt.QtWidgets import QAction, QMainWindow, QVBoxLayout, QWidget, QMenu, QDialog, QComboBox, QLabel, \
    QPushButton, QVBoxLayout, QToolBar
from qgis.core import QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsRectangle
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt, QUrl, QSettings, QSize  # Import QUrl and QSettings from qgis.PyQt.QtCore
from qgis.PyQt.QtGui import QIcon
import webbrowser

from .resources import *


class QuickMapLinkPlugin:
    def __init__(self, iface: QgisInterface):
        self.iface = iface
        self.context_menu = QMenu()
        self.context_action = QAction("Open Map Here (Webview)", self.iface.mainWindow())
        self.context_action.triggered.connect(self.open_google_maps_context)
        self.context_browser_action = QAction("Open Map Here (Browser)", self.iface.mainWindow())
        self.context_browser_action.triggered.connect(self.open_google_maps_in_browser_context)
        self.settings_action = QAction("Map Settings", self.iface.mainWindow())
        self.settings_action.triggered.connect(self.open_settings_dialog)

        # Load the default map type from settings
        self.settings = QSettings("MyOrganization", "QuickMapsLink Settings")
        self.map_type = self.settings.value("map_type", "Google Maps")

        # Toolbar button
        self.toolbar_button = QAction(QIcon(":/plugins/quick_map_link/icon.png"), "Toggle QuickMapLink", self.iface.mainWindow())
        self.toolbar_button.setCheckable(True)
        self.toolbar_button.setChecked(True)  # Initially checked
        self.toolbar_button.triggered.connect(self.toggle_context_menu_options)

        # Add actions to context menu
        self.context_menu.addAction(self.context_action)
        self.context_menu.addAction(self.context_browser_action)

    def initGui(self):
        # Add the settings action to the plugin menu
        self.iface.addPluginToMenu("QuickMapLink", self.settings_action)
        self.iface.mapCanvas().setContextMenuPolicy(Qt.CustomContextMenu)
        self.iface.mapCanvas().customContextMenuRequested.connect(self.show_context_menu)

        # Add toolbar button
        self.toolbar = self.iface.addToolBar("QuickMapLink")
        self.toolbar.addAction(self.toolbar_button)

    def unload(self):
        self.iface.removePluginMenu("QuickMapLink", self.settings_action)
        self.iface.mapCanvas().customContextMenuRequested.disconnect(self.show_context_menu)

        # Remove toolbar button
        self.iface.removeToolBarIcon(self.toolbar_button)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        del self.toolbar

    def open_google_maps_context(self):
        self.open_google_maps_at_location(self.context_point)

    def open_google_maps_in_browser_context(self):
        self.open_google_maps_in_browser(self.context_point)

    def open_google_maps_at_location(self, point=None):
        self.window = QMainWindow()
        self.window.setWindowTitle(self.map_type + " (Webview)")
        self.window.setGeometry(100, 100, 800, 600)

        webview = QWebView()  # Use QWebView instead of QWebEngineView

        # Initialize extent and map_point
        extent = None
        map_point = None

        if point:
            # Get the coordinates of the clicked point
            map_point = self.iface.mapCanvas().getCoordinateTransform().toMapCoordinates(point.x(), point.y())
            # Get the extent of the map canvas
            extent = self.iface.mapCanvas().extent()
        else:
            # Get the extent of the map canvas
            extent = self.iface.mapCanvas().extent()
            # Calculate the center point of the extent
            center_point = extent.center()
            map_point = center_point

        # Get the CRS of the current project
        crs = QgsProject.instance().crs()
        # Transform the point to WGS 84 (EPSG:4326)
        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            map_point = transform.transform(map_point)
        # Format the coordinates for Google Maps URL
        latitude = map_point.y()
        longitude = map_point.x()

        # Construct the URL based on the selected map type
        if self.map_type == "Google Maps":
            url = f"https://www.google.com/maps/@{latitude},{longitude},{extent.height()}m"
        elif self.map_type == "Bing Maps":
            url = f"https://www.bing.com/maps?cp={latitude}~{longitude}&lvl=20"
        elif self.map_type == "Apple Maps":
            url = f"https://beta.maps.apple.com/?ll={latitude},{longitude}&z=20"
        else:
            url = f"https://www.google.com/maps/@{latitude},{longitude},15z"  # Default to Google Maps

        print(f"URL: {url}")
        webview.setUrl(QUrl(url))  # Create a QUrl object

        central_widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(webview)
        central_widget.setLayout(layout)

        self.window.setCentralWidget(central_widget)
        self.window.show()

    def open_google_maps_in_browser(self, point=None):

        # Initialize extent and map_point
        extent = None
        map_point = None

        if point:
            # Get the coordinates of the clicked point
            map_point = self.iface.mapCanvas().getCoordinateTransform().toMapCoordinates(point.x(), point.y())
            # Get the extent of the map canvas
            extent = self.iface.mapCanvas().extent()
        else:
            # Get the extent of the map canvas
            extent = self.iface.mapCanvas().extent()
            # Calculate the center point of the extent
            center_point = extent.center()
            map_point = center_point

        # Get the CRS of the current project
        crs = QgsProject.instance().crs()
        # Transform the point to WGS 84 (EPSG:4326)
        if crs.authid() != "EPSG:4326":
            transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            map_point = transform.transform(map_point)
        # Format the coordinates for Google Maps URL
        latitude = map_point.y()
        longitude = map_point.x()

        # Construct the URL based on the selected map type
        if self.map_type == "Google Maps":
            url = f"https://www.google.com/maps/@{latitude},{longitude},{extent.height()}m"
        elif self.map_type == "Bing Maps":
            url = f"https://www.bing.com/maps?cp={latitude}~{longitude}&lvl=20"
        elif self.map_type == "Apple Maps":
            url = f"https://beta.maps.apple.com/?ll={latitude},{longitude}&z=20"
        else:
            url = f"https://www.google.com/maps/@{latitude},{longitude},15z"  # Default to Google Maps

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

        # Save Button
        save_button = QPushButton("Save")
        save_button.clicked.connect(lambda: self.save_settings(map_type_combo.currentText(), dialog))
        layout.addWidget(save_button)

        dialog.setLayout(layout)
        dialog.exec_()

    def save_settings(self, map_type, dialog):
        self.map_type = map_type
        self.settings.setValue("map_type", map_type)
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