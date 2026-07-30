"""Structural detectors.

Importing this package registers every built-in detector. Third-party detectors register
themselves the same way, by applying :func:`register` to a :class:`Detector` subclass.
"""

from .base import REGISTRY, Detector, active_detectors, register
from .compliance import CompliancePropagationDetector
from .leakage import TargetLeakageDetector
from .semantic import BaselineColumn, SemanticBaseline, SilentSemanticChangeDetector
from .skew import TrainServeSkewDetector

__all__ = [
    "REGISTRY",
    "BaselineColumn",
    "CompliancePropagationDetector",
    "Detector",
    "SemanticBaseline",
    "SilentSemanticChangeDetector",
    "TargetLeakageDetector",
    "TrainServeSkewDetector",
    "active_detectors",
    "register",
]
