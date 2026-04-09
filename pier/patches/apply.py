"""Discover and apply all harbor patches."""

from __future__ import annotations

from pier.patches import patch_skip_install, patch_trial_chown

ALL_PATCHES = [
    ("trial-chown", patch_trial_chown),
    ("skip-install", patch_skip_install),
]


def apply_all(*, verbose: bool = True) -> int:
    """Apply all patches. Returns count of newly applied patches."""
    applied = 0
    for name, mod in ALL_PATCHES:
        if mod.is_applied():
            if verbose:
                print(f"  {name}: already applied")
            continue
        result = mod.apply()
        if result:
            applied += 1
            if verbose:
                if isinstance(result, list):
                    for path in result:
                        print(f"  {name}: patched {path}")
                else:
                    print(f"  {name}: patched {result}")
        else:
            if verbose:
                print(f"  {name}: skipped (harbor not found)")
    return applied
