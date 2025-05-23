#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Created By  : Matthew Davidson
# Created Date: 2023-01-23
# version ='1.0'
# ---------------------------------------------------------------------------
"""a_short_module_description"""
# ---------------------------------------------------------------------------
from __future__ import annotations
import logging
from pathlib import Path
import socket
import threading
from typing import Dict, List, NoReturn, Optional, Tuple, Union
import time
import asyncio

import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTErrorCode
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions
from prometheus_client import Counter

from mqtt_node_network.configuration import (
    MQTTConnectProperties,
    MQTTPublishProperties,
    TLSConfig,
    initialize_config,
    MQTTBrokerConfig,
    SubscribeConfig,
    MQTTPacketProperties,
    MQTTWillConfig,
    MQTTStatusConfig,
)


# Initialize your logger and adapter
logger = logging.getLogger(__name__)

import logging


def shorten_data(data: str, max_length: int = 75) -> str:
    """Shorten data to a maximum length."""
    if not isinstance(data, str):
        data = str(data)
    data = data.strip()
    return data[:max_length] + "..." if len(data) > max_length else data


def convert_bytes_to_human_readable(num: float) -> str:
    """Convert bytes to a human-readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} {unit}"


def extend_or_append(list_topics: List[str], topic: Union[str, Tuple]) -> None:
    """
    Recursively extend or append topics to a list.

    :param list_topics: A list of topic strings.
    :param topic: A topic to be added, which can be a string or tuple.
    """
    for item in topic:
        if isinstance(item, tuple):
            extend_or_append(list_topics, item)
        else:
            list_topics.append(item)


def parse_packet_properties_dict(properties: Dict[str, Union[str, int]]) -> Properties:
    """
    Convert a dictionary into MQTT packet_Properties.

    :param packet_properties: Dictionary containing packet_properties.
    :return: MQTT packet_Properties object.
    """
    publish_properties = Properties(PacketTypes.PUBLISH)

    if isinstance(properties, dict):
        for key, value in properties.items():
            if not isinstance(value, str):
                value = str(value)
            publish_properties.UserProperty = (key, value)
    elif isinstance(properties, Properties):
        publish_properties = properties
    else:
        raise ValueError(
            "User property must be a dictionary or a paho.mqtt.properties.Properties instance"
        )
    return publish_properties


def parse_topic(
    topic: Union[str, List[str], Tuple[str, ...]],
    qos: Optional[int] = None,
    options: mqtt.SubscribeOptions = None,
) -> Union[List[Tuple[str, mqtt.SubscribeOptions]], Tuple[str, mqtt.SubscribeOptions]]:
    """
    Parse a topic string, list, or tuple and apply MQTT subscription options.

    :param topic: A single topic or list/tuple of topics.
    :param qos: Quality of Service level (optional).
    :return: Parsed topic(s) with subscription options applied.
    """
    options = options or SubscribeOptions()
    if qos is not None and options.QoS != qos:
        options.QoS = qos
        logger.warning(
            f"Overriding QoS value in options with value {qos}",
        )
    if isinstance(topic, str):
        return (topic, options)
    elif isinstance(topic, tuple):
        return (topic[0], options)
    elif isinstance(topic, list):
        return [(topic_, options) for topic_ in topic]
    else:
        raise ValueError("Topic must be a string, tuple or list")


def dict_to_user_packet_properties(
    packet_properties_dict: Dict[str, str],
) -> List[Tuple[str, str]]:
    """
    Convert a dictionary to a list of tuples for user packet_properties.

    :param packet_properties_dict: Dictionary containing user packet_properties.
    :return: List of tuples where each tuple is (key, value).
    """
    return [(key, value) for key, value in packet_properties_dict.items()]


def user_packet_properties_to_dict(
    user_packet_properties: List[Tuple[str, str]],
) -> Dict[str, str]:
    """
    Convert a list of tuples (user packet_properties) to a dictionary.

    :param user_packet_properties: List of tuples where each tuple is (key, value).
    :return: Dictionary containing user packet_properties.
    """
    return dict(user_packet_properties)


class CustomLoggerAdapter(logging.LoggerAdapter):
    """
    A custom logger adapter that adds support for the merge_extra argument
    for Python versions prior to 3.13.
    """

    def __init__(self, logger, extra=None, merge_extra=False):
        # Check for Python version to decide whether to use merge_extra
        import sys

        if sys.version_info >= (3, 13):
            super().__init__(logger, extra)
            self.merge_extra = merge_extra
        else:
            # For Python versions < 3.13, ignore merge_extra
            super().__init__(logger, extra)
            self.merge_extra = False

    def process(self, msg, kwargs):
        """
        Process the logging message and keyword arguments to insert contextual
        information. This method is overridden to support the `merge_extra` feature.
        """
        if self.merge_extra and "extra" in kwargs:
            kwargs["extra"] = {**self.extra, **kwargs["extra"]}
        else:
            kwargs["extra"] = self.extra
        return msg, kwargs


class NodeError(Exception):
    """
    Exception raised for errors in the MQTTNode.
    """

    def __init__(self, message: str):
        self.message = message
        logger.error(self.message)
        super().__init__(self.message)


class MQTTNode:
    """
    A base class representing an MQTT Node, with integrated Prometheus metrics.
    """

    node_bytes_received_count = Counter(
        "node_bytes_received_total",
        "Total number of bytes received by node",
        labelnames=("node_id", "node_name", "node_type", "host"),
    )

    node_bytes_sent_count = Counter(
        "node_bytes_sent_total",
        "Total number of bytes sent by node",
        labelnames=("node_id", "node_name", "node_type", "host"),
    )

    node_messages_received_count = Counter(
        "node_messages_received_total",
        "Total number of messages received by node",
        labelnames=("node_id", "node_name", "node_type", "host"),
    )

    node_messages_sent_count = Counter(
        "node_messages_sent_total",
        "Total number of messages sent by node",
        labelnames=("node_id", "node_name", "node_type", "host"),
    )

    @classmethod
    def from_config_file(
        cls,
        config_file: Union[str, Path],
        secrets_file: Optional[Union[str, Path]] = None,
        **kwargs,
    ) -> MQTTNode:
        """
        Instantiate an MQTTNode from a configuration file.

        :param config_file: Path to the configuration file.
        :param secrets_file: Path to the secrets file (optional).
        :param kwargs: Additional keyword arguments. See MQTTNode.__init__ for details. These will override the config file.
        :return: An initialized MQTTNode instance.
        """
        configs = initialize_config(config=config_file, secrets=secrets_file)
        config = configs.get(cls.__name__, configs.get("MQTTNode", None))
        if config is None:
            raise NodeError(
                f"Configuration for class {cls.__name__} not found in config file. Please check that the configuration exists"
            )
        # Combine the configuration from the file with any additional keyword arguments
        # The keyword arguments will override the configuration file
        combined_args = {**config, **kwargs}
        return cls(**combined_args)

    def __init__(
        self,
        broker_config: MQTTBrokerConfig,
        name: str,
        node_id: Optional[str] = None,
        subscribe_config: SubscribeConfig = None,
        packet_properties: dict[str, MQTTPacketProperties] = None,
        transport_config: Optional[TLSConfig] = None,
        will_config: Optional[MQTTWillConfig] = None,
        status_config: Optional[MQTTStatusConfig] = None,
    ):
        """
        Initialize an MQTTNode instance.

        :param broker_config: The configuration for the MQTT broker.
        :param name: The name of the node.
        :param node_id: A unique identifier for the node (optional).
        :param subscribe_config: Configuration for subscribed topics.
        """
        self.name = name
        self.node_type = self.__class__.__name__
        self.node_id = node_id if node_id else self._get_id()
        self.subscriptions = subscribe_config.topics if subscribe_config else []
        self.subscribe_options = (
            subscribe_config.options if subscribe_config else SubscribeOptions()
        )
        self.will_config = will_config
        self.status_config = status_config

        self.hostname: str = broker_config.hostname
        self.port: int = broker_config.port
        self.address = (broker_config.hostname, broker_config.port)
        self.keepalive: int = broker_config.keepalive
        self.timeout: int = broker_config.timeout
        self.reconnect_attempts: int = broker_config.reconnect_attempts
        self.clean_session: bool = broker_config.clean_session

       default_packet_properties =  {
            PacketTypes.CONNECT: MQTTConnectProperties(),
            PacketTypes.PUBLISH: MQTTPublishProperties(),
        }
        self.packet_properties = (
            packet_properties if packet_properties else default_packet_properties
        )

        self._username: str = broker_config.username
        self._password: str = broker_config.password
        self._auth: Dict[str, str] = {
            "username": broker_config.username,
            "password": broker_config.password,
        }

        # Initialize paho client
        client_id = self.name or self.node_id
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
        if self._username and self._password:
            self.client.username_pw_set(self._username, self._password)
        if transport_config:
            self.client.tls_set(
                ca_certs=transport_config.cafile,
                certfile=transport_config.certfile,
                keyfile=transport_config.keyfile,
                cert_reqs=transport_config.cert_reqs,
                tls_version=transport_config.tls_version,
                ciphers=transport_config.ciphers,
            )

        if self.will_config.enabled:
            self.client.will_set(
                self.will_config.topic,
                self.will_config.payload,
                qos=self.will_config.qos,
                retain=self.will_config.retain,
                properties=self.will_config.properties,
            )

        # self.client.enable_logger(logger)

        self._connect_event = threading.Event()

        # Set client callbacks
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish
        self.is_connected = self.client.is_connected

        # Not currently used
        # ***************************************************************************
        # self.client.on_pre_connect = self.on_pre_connect
        # self.client.on_subscribe = self.on_subscribe
        # self.client.on_unsubscribe = self.on_unsubscribe
        # self.client.on_log = self.on_log
        # self.loop_forever = self.client.loop_forever

        # Set up custom logger for node with additional fields
        self.logger = CustomLoggerAdapter(
            logger,
            extra={
                "node_id": self.node_id,
                "node_name": self.name,
                "node_type": self.node_type,
                "host": self.hostname,
            },
            merge_extra=True,
        )

    def connect(
        self,
        packet_properties: Optional[Properties] = None,
        ensure_connected: bool = False,
    ) -> None:
        if self.is_connected() is False:
            self._connect_event.clear()  # Clear the event before connecting
            if packet_properties is None:
                packet_properties = self.packet_properties[PacketTypes.CONNECT].build()

            self.client.reconnect_delay_set(min_delay=1, max_delay=30)

            self.client.connect_async(
                host=self.hostname,
                port=self.port,
                keepalive=self.keepalive,
                clean_start=self.clean_session,
                properties=packet_properties,
            )
            self.loop_start()
            if ensure_connected:
                time.sleep(0.2)
                self.ensure_connection()
            self.update_node_status()

    # def connect(
    #     self,
    #     packet_properties: Optional[Properties] = None,
    #     ensure_connected: bool = False,
    # ) -> MQTTErrorCode:
    #     if self.is_connected() is False:
    #         if packet_properties is None:
    #             packet_properties = self.packet_properties[PacketTypes.CONNECT].build()
    #         error_code = self.client.connect(
    #             host=self.hostname,
    #             port=self.port,
    #             keepalive=self.keepalive,
    #             clean_start=self.clean_session,
    #             properties=packet_properties,
    #         )
    #         if error_code != 0:
    #             self.logger.warning(
    #                 f"Connection attempt to {self.hostname}:{self.port} failed"
    #             )
    #             return error_code
    #         self.client.socket().setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
    #         if ensure_connected:
    #             time.sleep(0.2)
    #             self.ensure_connection()
    #     return MQTTErrorCode.MQTT_ERR_SUCCESS

    def subscribe(
        self,
        topic: Union[str, tuple, list],
        qos: Optional[int] = None,
        options: mqtt.SubscribeOptions = None,
    ):
        """
        Subscribe to a topic
        :topic: str | tuple | list
        :qos: quality of service, 0 | 1 | 2

        """
        if options is None:
            options = self.subscribe_options

        topic = parse_topic(topic, qos, options)

        result = self.client.subscribe(topic)

        if result[0] != 0:
            error_string = mqtt.error_string(result[0])
            self.logger.error(
                f"{error_string}, failed to subscribe to topic: {topic}",
                extra={
                    "error_code": error_string,
                    "qos": qos,
                },
            )
        else:
            self.logger.info(
                f"Subscribed to topic: {topic}",
                extra={
                    "topic": topic,
                    "qos": qos,
                },
            )

        # Add the topic to the list of subscriptions
        self.add_subscription_topic(topic)

    def unsubscribe(
        self, topic: Union[str, list[str]], packet_properties=None
    ) -> MQTTErrorCode:
        """
        :param topic: A single string, or list of strings that are the subscription
            topics to unsubscribe from.
        :param packet_properties: (MQTT v5.0 only) a packet_Properties instance setting the MQTT v5.0 packet_properties
            to be included. Optional - if not set, no packet_properties are sent.
        """
        # remove from self.subscriptions
        if isinstance(topic, list):
            for t in topic:
                self.subscriptions.remove(t)
        elif isinstance(topic, str):
            self.subscriptions.remove(topic)
        err_code, _ = self.client.unsubscribe(topic, properties=packet_properties)
        return err_code

    def add_subscription_topic(self, topic: Union[str, list, tuple]):

        def append_topic(topic):
            assert isinstance(topic, str)
            if topic not in self.subscriptions:
                self.subscriptions.append(topic)

        if isinstance(topic, list):
            for t in topic:
                if isinstance(t, tuple):
                    append_topic(t[0])
                else:
                    append_topic(t)

        elif isinstance(topic, tuple):
            append_topic(topic[0])
        elif isinstance(topic, str):
            append_topic(topic)

    def restore_subscriptions(self):
        for topic in self.subscriptions:
            self.subscribe(topic)

    def unsubscribe_all(self) -> List[int]:
        error_codes = []
        for topic in self.subscriptions.copy():
            err_code = self.unsubscribe(topic)
            if err_code != 0:
                self.logger.error(
                    f"Failed to unsubscribe from topic {topic}",
                    extra={"topic": topic, "error_code": err_code},
                )
            error_codes.append(err_code)
        return error_codes

    def ensure_connection(self) -> None:
        if self.is_connected():
            return
        if not self._connect_event.wait(timeout=10):
            # If `on_connect` does not fire within 10 seconds, raise an error
            logger.error("Timed out waiting for on_connect.")

    def publish(
        self,
        topic,
        payload,
        qos=0,
        retain=False,
        properties=None,
        ensure_published=False,
    ) -> mqtt.MQTTMessageInfo:
        # self.ensure_connection()
        if properties:
            properties = parse_packet_properties_dict(properties)
        else:
            properties = self.packet_properties[PacketTypes.PUBLISH].build()
        message_info = self.client.publish(
            topic, payload, qos, retain, properties=properties
        )
        if ensure_published:
            message_info.wait_for_publish(timeout=self.timeout)
        return message_info

    def publish_every(
        self,
        topic,
        payload_func,
        qos=0,
        retain=False,
        properties=None,
        interval=1,
    ) -> NoReturn:
        """
        Publish a message every interval seconds.

        This is a blocking function, and will run indefinitely.
        :topic: str - The topic to publish to
        :payload_func: function - A function that returns the payload to publish
        :qos: int - The Quality of Service level
        :retain: bool - Whether to retain the message
        :packet_properties: dict - MQTT packet_properties
        :interval: int - The interval in seconds
        """

        while True:
            payload = payload_func()
            self.publish(topic, payload, qos, retain, properties)
            time.sleep(interval)

    async def publish_every_async(
        self,
        topic,
        payload_func,
        qos=0,
        retain=False,
        packet_properties=None,
        interval=1,
    ) -> NoReturn:
        while True:
            payload = payload_func()
            self.publish(topic, payload, qos, retain, packet_properties)
            await asyncio.sleep(interval)

    def check_loop_running(self):
        if self.client._thread is not None and self.client._thread.is_alive():
            return True
        return False

    def loop_forever(self, timeout: int = 1, reconnect_delay: int = 5) -> NoReturn:
        """
        Continuously checks connection status, keeps the client alive, and handles reconnections.
        If latency monitoring is enabled, starts periodic latency checks.

        Args:
            timeout (int): Time (in seconds) to wait between connection checks.
            reconnect_delay (int): Time (in seconds) to wait before attempting a reconnection.
        """
        if self.latency_config.enabled:
            self.start_periodic_latency_check()

        self.logger.info("Entering main loop with reconnection handling.")
        self.ensure_connection()
        try:
            while True:
                if self.is_connected():
                    self.logger.debug("Connection active. Maintaining connection.")
                    time.sleep(timeout)
                else:
                    self.logger.warning("Connection lost. Attempting to reconnect...")
                    try:
                        self.connect()
                        self.ensure_connection()
                        self.logger.info("Reconnected successfully.")
                    except Exception as e:
                        self.logger.error(f"Reconnection failed: {e}")
                        self.logger.info(f"Retrying in {reconnect_delay} seconds...")
                        time.sleep(reconnect_delay)
        except KeyboardInterrupt:
            self.logger.info("Loop interrupted by user. Stopping...")
        except Exception as e:
            self.logger.error(f"An unexpected error occurred in the main loop: {e}")
        finally:
            self.__del__()

    def loop_start(self) -> MQTTNode:

        error_code = self.client.loop_start()

        if error_code != 0:
            self.logger.error(
                f"Failed to start loop: {mqtt.error_string(error_code)}",
            )

        return self

    def loop_stop(self) -> MQTTNode:
        error_code = self.client.loop_stop()
        if error_code != 0:
            self.logger.error(
                f"Failed to stop loop: {mqtt.error_string(error_code)}",
            )
        return self

    def update_node_status(
        self, status: Union[str, int, float] = None, properties: Properties = None
    ) -> None:
        """
        Publish a status update to the status topic.
        :param status: The status to publish.
        :param properties: MQTT packet properties.
        """
        if self.status_config is None:
            self.logger.debug("Status configuration not provided.")
            return
        status = status or self.status_config.payload
        self.publish(
            self.status_config.topic,
            status,
            qos=self.status_config.qos,
            retain=self.status_config.retain,
            properties=properties,
        )
        logger.debug(
            f"Published status update: {status}",
        )

    # Callbacks
    # ***************************************************************************

    def on_connect(self, client, userdata, flags, reason_code, properties):

        if reason_code == 0:
            self.logger.info(
                f"Connected to broker at {client.host}:{client.port}",
            )
            self._connect_event.set()
            if not flags.session_present:
                logger.debug(
                    "No session present. Restoring subscriptions ...",
                )
                self.restore_subscriptions()
        else:
            logger.error(f"Connection failed with code {reason_code}")

    def on_connect_fail(self, client, userdata):
        self.logger.error(
            f"Failed to connect to broker at {client.host}:{client.port}",
        )

    def on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        self.logger.info(
            f"Disconnected with result code: {reason_code}",
        )

    def on_message(self, client, userdata, message):

        self.node_messages_received_count.labels(
            self.node_id, self.name, self.node_type, self.hostname
        ).inc()

        self.node_bytes_received_count.labels(
            self.node_id, self.name, self.node_type, self.hostname
        ).inc(len(message.payload))

        self.logger.debug(
            f"Received message on topic '{message.topic}': {shorten_data(message.payload.decode())}",
            extra={
                "topic": message.topic,
                "qos": message.qos,
            },
        )

    def on_publish(self, client, userdata, mid, reason_code, properties):
        self.node_messages_sent_count.labels(
            self.node_id, self.name, self.node_type, self.hostname
        ).inc()
        self.logger.debug(f"Published message #{mid}")

    # def on_subscribe(self, client, userdata, mid, reason_code_list, packet_properties):
    #  self.logger.info("Subscribed to topic")

    # def on_unsubscribe(self, client, userdata, mid, packet_properties, reason_codes):
    #  self.logger.info("Unsubscribed from topic")

    def message_callback_add(self, topic: str, callback: callable, qos: int = 0):
        """
        Add a callback to a topic. When a message is received on the topic, the callback will be called.
        The callback should take the form of a function that accepts three arguments: client, userdata, message.
        callback(client, userdata, message)
        :topic: str - The topic to add the callback to
        :callback: function - The function to be called
        """
        if not callable(callback):
            raise NodeError("Callback must be a function")
        if not isinstance(topic, str):
            raise NodeError("Topic must be a string")
        if not topic in self.subscriptions:
            self.logger.debug(
                f"Topic {topic} not in subscriptions. Adding topic to subscriptions.",
                extra={
                    "topic": topic,
                },
            )
            qos = qos or self.subscribe_options.QoS
            self.subscribe(topic, qos=qos, options=self.subscribe_options)
        self.client.message_callback_add(topic, callback)
        logger.debug(
            f"Added callback to topic: {topic}",
            extra={"topic": topic, "callback": callback.__name__},
        )

    def message_callback_remove(self, topic: str):
        """
        Remove a callback from a topic.
        :topic: str - The topic to remove the callback from
        """
        if not isinstance(topic, str):
            raise NodeError("Topic must be a string")
        if not topic in self.subscriptions:
            self.logger.debug(
                f"Removal of callback on topic {topic} requested, but not in subscriptions. Continuing ...",
                extra={
                    "topic": topic,
                },
            )
        self.client.message_callback_remove(topic)
        logger.debug(
            f"Removed callback from topic: {topic}",
            extra={"topic": topic},
        )

    def on_log(self, client, userdata, level, buf):
        self.logger.debug("Log: {}".format(buf))

    def _get_id(self):
        # Return a unique id for each node
        return f"{self.node_type}_{time.time_ns()}"

    def disconnect(self, reasoncode=None, properties=None) -> int:
        """Initiate an asynchronous disconnect, and return the Paho error code."""
        rc = self.client.disconnect(reasoncode=reasoncode, properties=properties)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            self.logger.error(f"Failed to initiate disconnect, error code: {rc}")
        else:
            self.logger.info(f"Disconnected from broker at {self.hostname}:{self.port}")
        return rc

    def __del__(self):
        try:
            self.disconnect(reasoncode=mqtt.MQTT_RC_DISCONNECT_WITH_WILL_MSG)
        except AttributeError:
            # Nothing to disconnect
            pass

    def close(self):
        self.__del__()
        self.loop_stop()
