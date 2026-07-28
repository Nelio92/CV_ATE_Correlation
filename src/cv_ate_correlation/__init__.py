"""Profile-driven ATE-to-reference measurement correlation."""

from .models import CorrelationProfile, ExtractionProfile
from .correlation import CorrelationResult, correlate_frame
from .extraction import LegacyWideTeCsvAdapter

__all__ = [
    "CorrelationProfile",
    "CorrelationResult",
    "ExtractionProfile",
    "LegacyWideTeCsvAdapter",
    "correlate_frame",
]

__version__ = "0.1.0"
