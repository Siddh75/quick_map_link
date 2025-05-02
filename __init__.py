from .quick_map_link import QuickMapLinkPlugin

def classFactory(iface):
    return QuickMapLinkPlugin(iface)