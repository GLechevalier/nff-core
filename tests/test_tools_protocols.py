"""Unit tests for protocol tools: mqtt_probe, mqtt_topic_enum, coap_discover, ble_scan, zigbee_scan."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import make_cmd

from nff_pentester.tools.protocols.ble import BleScanInput, ble_scan
from nff_pentester.tools.protocols.coap import CoapDiscoverInput, coap_discover
from nff_pentester.tools.protocols.mqtt import (
    MqttProbeInput,
    MqttTopicEnumInput,
    mqtt_probe,
    mqtt_topic_enum,
)
from nff_pentester.tools.protocols.zigbee import ZigbeeScanInput, zigbee_scan

_PATCH_COAP_RUN = "nff_pentester.tools.protocols.coap.run_command"


# ── mqtt_probe ─────────────────────────────────────────────────────────────────

async def test_mqtt_probe_no_aiomqtt(mock_ctx):
    with patch.dict(sys.modules, {"aiomqtt": None}):
        params = MqttProbeInput(host="127.0.0.1", port=1883)
        result = await mqtt_probe(params, mock_ctx)
    assert result.connected is False
    assert result.error == "aiomqtt not installed"


async def test_mqtt_probe_anonymous_access(mock_ctx):
    mock_client_instance = AsyncMock()
    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cm.__aexit__ = AsyncMock(return_value=False)

    mock_aiomqtt = MagicMock()
    mock_aiomqtt.Client = MagicMock(return_value=mock_client_cm)
    mock_aiomqtt.MqttConnectError = Exception

    with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
        params = MqttProbeInput(host="127.0.0.1", port=1883)
        result = await mqtt_probe(params, mock_ctx)

    assert result.anonymous_access is True
    assert result.connected is True
    assert result.error is None


async def test_mqtt_probe_auth_required(mock_ctx):
    class MockMqttConnectError(Exception):
        pass

    mock_error_cm = AsyncMock()
    mock_error_cm.__aenter__ = AsyncMock(side_effect=MockMqttConnectError("not authorised"))
    mock_error_cm.__aexit__ = AsyncMock(return_value=False)

    mock_aiomqtt = MagicMock()
    mock_aiomqtt.Client = MagicMock(return_value=mock_error_cm)
    mock_aiomqtt.MqttConnectError = MockMqttConnectError

    with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
        params = MqttProbeInput(host="127.0.0.1", port=1883)
        result = await mqtt_probe(params, mock_ctx)

    assert result.auth_required is True
    assert result.anonymous_access is False


async def test_mqtt_probe_connection_error(mock_ctx):
    mock_error_cm = AsyncMock()
    mock_error_cm.__aenter__ = AsyncMock(side_effect=OSError("Connection refused"))
    mock_error_cm.__aexit__ = AsyncMock(return_value=False)

    mock_aiomqtt = MagicMock()
    mock_aiomqtt.Client = MagicMock(return_value=mock_error_cm)
    mock_aiomqtt.MqttConnectError = Exception

    with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
        params = MqttProbeInput(host="127.0.0.1", port=1883)
        result = await mqtt_probe(params, mock_ctx)

    assert result.connected is False
    assert result.error is not None


# ── mqtt_topic_enum ────────────────────────────────────────────────────────────

async def test_mqtt_topic_enum_no_aiomqtt(mock_ctx):
    with patch.dict(sys.modules, {"aiomqtt": None}):
        params = MqttTopicEnumInput(host="127.0.0.1", listen_seconds=1)
        result = await mqtt_topic_enum(params, mock_ctx)
    assert result.error == "aiomqtt not installed"
    assert result.topics_found == []


async def test_mqtt_topic_enum_collects_topics(mock_ctx):
    msg1 = MagicMock()
    msg1.topic = "sensors/temperature"
    msg2 = MagicMock()
    msg2.topic = "sensors/humidity"

    async def fake_messages():
        yield msg1
        yield msg2
        raise asyncio.TimeoutError()

    mock_client_instance = AsyncMock()
    mock_client_instance.subscribe = AsyncMock()
    mock_client_instance.messages = fake_messages()

    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cm.__aexit__ = AsyncMock(return_value=False)

    mock_aiomqtt = MagicMock()
    mock_aiomqtt.Client = MagicMock(return_value=mock_client_cm)
    mock_aiomqtt.MqttConnectError = Exception

    with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
        params = MqttTopicEnumInput(host="127.0.0.1", listen_seconds=5)
        result = await mqtt_topic_enum(params, mock_ctx)

    assert result.message_count == 2
    assert "sensors/temperature" in result.topics_found
    assert "sensors/humidity" in result.topics_found
    assert result.error is None


# ── coap_discover ──────────────────────────────────────────────────────────────

async def test_coap_discover_binary_not_found(mock_ctx):
    with patch(_PATCH_COAP_RUN, new=AsyncMock(return_value=make_cmd(returncode=1, stderr="coap-client: not found"))):
        params = CoapDiscoverInput(host="192.168.1.1")
        result = await coap_discover(params, mock_ctx)
    assert result.error is not None
    assert result.resources == []


async def test_coap_discover_parses_core_link(mock_ctx):
    core_link = "</sensors/temp>;rt=temperature,</sensors/humidity>;rt=humidity"
    with patch(_PATCH_COAP_RUN, new=AsyncMock(return_value=make_cmd(stdout=core_link))):
        params = CoapDiscoverInput(host="192.168.1.1")
        result = await coap_discover(params, mock_ctx)
    assert result.error is None
    assert len(result.resources) == 2
    paths = [r.path for r in result.resources]
    assert "/sensors/temp" in paths
    assert "/sensors/humidity" in paths


async def test_coap_discover_timeout(mock_ctx):
    with patch(_PATCH_COAP_RUN, new=AsyncMock(return_value=make_cmd(timed_out=True))):
        params = CoapDiscoverInput(host="192.168.1.1")
        result = await coap_discover(params, mock_ctx)
    assert result.error is not None


# ── ble_scan ───────────────────────────────────────────────────────────────────

async def test_ble_scan_no_bleak(mock_ctx):
    with patch.dict(sys.modules, {"bleak": None}):
        params = BleScanInput(scan_seconds=1)
        result = await ble_scan(params, mock_ctx)
    assert result.device_count == 0
    assert result.error is not None
    assert "bleak" in result.error


async def test_ble_scan_discover_error(mock_ctx):
    mock_bleak = MagicMock()
    mock_scanner = MagicMock()
    mock_scanner.discover = AsyncMock(side_effect=OSError("No Bluetooth adapter"))
    mock_bleak.BleakScanner = mock_scanner

    with patch.dict(sys.modules, {"bleak": mock_bleak}):
        params = BleScanInput(scan_seconds=1)
        result = await ble_scan(params, mock_ctx)
    assert result.device_count == 0
    assert result.error is not None


async def test_ble_scan_returns_devices(mock_ctx):
    mock_device = MagicMock()
    mock_device.address = "AA:BB:CC:DD:EE:FF"
    mock_device.name = "SmartSensor"
    mock_device.rssi = -65
    mock_device.details = None
    del mock_device.metadata

    mock_bleak = MagicMock()
    mock_bleak.BleakScanner = MagicMock()
    mock_bleak.BleakScanner.discover = AsyncMock(return_value=[mock_device])

    with patch.dict(sys.modules, {"bleak": mock_bleak}):
        params = BleScanInput(scan_seconds=1)
        result = await ble_scan(params, mock_ctx)

    assert result.device_count == 1
    assert result.devices[0].address == "AA:BB:CC:DD:EE:FF"
    assert result.devices[0].name == "SmartSensor"
    assert result.error is None


async def test_ble_scan_filter_name(mock_ctx):
    d1 = MagicMock()
    d1.address = "AA:BB:CC:DD:EE:01"
    d1.name = "SmartSensor"
    d1.rssi = -60
    d1.details = None
    del d1.metadata

    d2 = MagicMock()
    d2.address = "AA:BB:CC:DD:EE:02"
    d2.name = "UnrelatedDevice"
    d2.rssi = -70
    d2.details = None
    del d2.metadata

    mock_bleak = MagicMock()
    mock_bleak.BleakScanner = MagicMock()
    mock_bleak.BleakScanner.discover = AsyncMock(return_value=[d1, d2])

    with patch.dict(sys.modules, {"bleak": mock_bleak}):
        params = BleScanInput(scan_seconds=1, filter_name="Smart")
        result = await ble_scan(params, mock_ctx)

    assert result.device_count == 1
    assert result.devices[0].name == "SmartSensor"


# ── zigbee_scan ────────────────────────────────────────────────────────────────

async def test_zigbee_scan_returns_stub_error(mock_ctx):
    params = ZigbeeScanInput()
    result = await zigbee_scan(params, mock_ctx)
    assert result.networks_found == 0
    assert result.networks == []
    assert result.error is not None
    assert "Zigbee" in result.error


async def test_zigbee_scan_specific_channel(mock_ctx):
    params = ZigbeeScanInput(channel=15, scan_seconds=2)
    result = await zigbee_scan(params, mock_ctx)
    assert result.networks_found == 0
    assert result.error is not None
