"""
Backward compatibility shim - imports from wayfinder.license

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.license.
"""

from wayfinder.license import (
    LicenseInfo,
    FeatureGate,
    PREMIUM_FEATURES,
    FREE_FEATURES,
    LICENSE_CHARS,
    get_machine_id,
    generate_license_key,
    validate_license_key,
    get_license_path,
    load_stored_license,
    store_license,
    remove_license,
    get_feature_gate,
)

__all__ = [
    "LicenseInfo",
    "FeatureGate",
    "PREMIUM_FEATURES",
    "FREE_FEATURES",
    "LICENSE_CHARS",
    "get_machine_id",
    "generate_license_key",
    "validate_license_key",
    "get_license_path",
    "load_stored_license",
    "store_license",
    "remove_license",
    "get_feature_gate",
]
