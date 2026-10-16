"""nff-mock-core — the contract/identity layer shared by every mock complexity level.

Level 1 (``nff_mock``, a pure-Python protocol simulator) and Level 2 (``nff_mock_l2``, a
real firmware image booted in QEMU) are very different *engines*, but they present the same
device to the platform: the same ``NFF_MOCK_*`` env contract, the same mTLS cert-chain
assembly, the same MQTT topic schema, the same heartbeat / crash / ota_result wire shapes,
and the same NVS persistence model. That shared surface lives here so neither level can drift
from it.
"""
