"""blitzen -- real-time lightning collection near a fixed location.

Data comes from the LightningMaps.org live feed (Blitzortung.org community
network). See the README for the licensing restrictions that come with it.
"""

from .config import Config
from .collector import Collector, IntervalReport, NearbyStroke
from .source import LightningMapsSource, Stroke
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "Collector",
    "Config",
    "IntervalReport",
    "LightningMapsSource",
    "NearbyStroke",
    "Store",
    "Stroke",
    "__version__",
]
