"""Master trade safety gate (control-center spec sections 13–36, 99).

Pure functions only: callers gather data, this package decides. Nothing here
performs I/O, so every decision is reproducible from its stored inputs.
"""

from yonixalpha_core.safety.gate import Assessment, assess, format_summary, requirements_for
from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.safety.models import RISK_ENGINE_VERSION
from yonixalpha_core.safety.planning import ratchet_trailing_stop
from yonixalpha_core.safety.settings import SafetySettings, clamp, settings_from_dict, settings_to_dict, validate

__all__ = [
    "Assessment",
    "ConstantProductModel",
    "RISK_ENGINE_VERSION",
    "SafetySettings",
    "assess",
    "clamp",
    "format_summary",
    "ratchet_trailing_stop",
    "requirements_for",
    "settings_from_dict",
    "settings_to_dict",
    "validate",
]
