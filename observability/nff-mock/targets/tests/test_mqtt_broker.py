"""Broker must accept anonymous clients, leak a control topic, and echo publishes."""

from __future__ import annotations

import asyncio

import pytest
from conftest import free_tcp_port, running

from nff_mock_targets.common.banners import ESP32
from nff_mock_targets.common.mqtt_broker import MqttBroker

aiomqtt = pytest.importorskip("aiomqtt")


def _broker(port: int) -> MqttBroker:
    # Fast publish interval so the control topic shows up quickly in the test.
    return MqttBroker(ESP32, "127.0.0.1", port, "device/cmd", publish_interval_s=0.2)


async def test_anonymous_connect_and_control_topic_seen():
    port = free_tcp_port()
    async with running(_broker(port)):
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe("#")
            async with asyncio.timeout(5):
                async for message in client.messages:
                    if str(message.topic) == "device/cmd":
                        break  # passive enum would see this -> /cmd control topic


async def test_publish_is_accepted_and_echoed():
    port = free_tcp_port()
    async with running(_broker(port)):
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe("test/echo")
            await client.publish("test/echo", b"hello")
            async with asyncio.timeout(5):
                async for message in client.messages:
                    if str(message.topic) == "test/echo" and message.payload == b"hello":
                        break  # mqtt_publish confirm_echo path
