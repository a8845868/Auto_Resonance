# NEMU input receipts and navigation postconditions

This document records the contracts implemented by
`NEMU_INPUT_RECEIPT_AND_NAVIGATION_POSTCONDITION_RECOVERY_V1`.

## Native ABI gate

The installed `external_renderer_ipc.dll` was inspected as a 64-bit PE image
(`IMAGE_FILE_MACHINE_AMD64`). The runtime binds every imported function with
explicit `argtypes` and `restype`. The x64 Windows platform ABI is used through
`ctypes.CDLL`; 32-bit DLLs and unknown machine types are rejected before an
instance connection or input call.

The audited touch functions take four integers: instance handle, display ID,
x, and y. The return type is an integer. A return value of zero is accepted;
positive and negative values are rejected or treated as unknown according to
the receipt boundary. This policy agrees with two independent open-source
wrappers and is intentionally narrower than inferring success from Python call
completion.

References used for the audit:

- <https://github.com/EvATive7/mumuipc.py/blob/main/src/mumuipc.py>
- <https://github.com/pur1fying/BAAS_Cpp/blob/main/include/device/BAASNemu.h>
- <https://github.com/pur1fying/BAAS_Cpp/blob/main/src/device/BAASNemu.cpp>
- <https://learn.microsoft.com/en-us/cpp/cpp/stdcall?view=msvc-170>
- <https://docs.python.org/3/library/ctypes.html>

No vendor SDK header was found locally. Consequently, the native signature
gate is limited to the inspected x64 DLL and the corroborated signatures; it
does not make a claim about other MuMu/NEMU versions or 32-bit builds.

## Touch receipt boundary

`NemuTouchReceipt` records the session, capture/display geometry, mapped point,
native down/up calls and return codes, delivery status, release status, timing,
and reason codes. The important distinction is:

- `NATIVE_ACCEPTED` means both native calls returned the accepted code.
- It does not mean that a visible UI effect occurred.
- A partial dispatch, exception after dispatch, or unknown release quarantines
  the session and never retries or falls back to ADB input.

Each input is preceded by instance, display, size, coordinate, process, and
foreground-package health checks. Capture points are mapped through an explicit
rotation-aware transform and validated against both source and destination
bounds.

## Evidence invariants

Navigation postconditions retain capture ID provenance, frame SHA-256,
timestamp, sequence when available, and session generation. A locally generated
sequence is marked as process-boundary provenance and is not represented as a
backend-native capture ID.

The following hard invariants apply:

- Equal pre/post hashes cannot prove a frame change or touch effect.
- The same detector classifying the same hash differently is detector
  nondeterminism and fails evidence consistency.
- Canonical page change requires a fresh frame and a changed hash.
- Native acceptance followed by unchanged NEMU frames is classified separately
  from delivery failure. An optional ADB screenshot may diagnose capture
  staleness, but ADB input is prohibited.

## Navigation and task outcomes

OCR text is a semantic anchor only. City and inventory actions require a unique
visual parent control on two fresh `HOME_READY` frames; the safe point is inside
the inset core of the final frame's parent control. Fixed coordinates, OCR
centres, random offsets, and first-candidate selection are not fallbacks.

Inventory page classification is centralized in `observe_inventory_page()`.
Known navigation and observation failures return `BLOCKED_SAFETY`, with no
incident escalation, queue halt, or automatic retry. Programming errors,
broken configuration structures, and invariant violations remain fatal.

`AutoReadInventory=true` is strict. A missing observation blocks the task.
Manual inventory is used only when `AllowManualInventoryFallback=true` and the
configured value is a validated non-negative integer.

## Live-gate boundary

The one-shot city gate requires a selected NEMU native backend. A selected
custom/ADB device blocks before capture or input and does not count as a live
action. Gate B is never started automatically after Gate A failure or block.
