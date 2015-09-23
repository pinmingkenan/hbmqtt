# Copyright (c) 2015 Nicolas JOUANIN
#
# See the file license.txt for copying permission.
import logging
import collections

from asyncio import InvalidStateError
from blinker import Signal

from hbmqtt.mqtt import packet_class
from hbmqtt.mqtt.packet import *
from hbmqtt.mqtt.connack import ConnackPacket
from hbmqtt.mqtt.connect import ConnectPacket
from hbmqtt.mqtt.pingresp import PingRespPacket
from hbmqtt.mqtt.pingreq import PingReqPacket
from hbmqtt.mqtt.publish import PublishPacket
from hbmqtt.mqtt.pubrel import PubrelPacket
from hbmqtt.mqtt.puback import PubackPacket
from hbmqtt.mqtt.pubrec import PubrecPacket
from hbmqtt.mqtt.pubcomp import PubcompPacket
from hbmqtt.mqtt.suback import SubackPacket
from hbmqtt.mqtt.subscribe import SubscribePacket
from hbmqtt.mqtt.unsubscribe import UnsubscribePacket
from hbmqtt.mqtt.unsuback import UnsubackPacket
from hbmqtt.mqtt.disconnect import DisconnectPacket
from hbmqtt.adapters import ReaderAdapter, WriterAdapter
from hbmqtt.session import Session, OutgoingApplicationMessage, IncomingApplicationMessage
from hbmqtt.mqtt.constants import *
from hbmqtt.mqtt.protocol.inflight import *
from hbmqtt.plugins.manager import PluginManager

import sys
if sys.version_info < (3, 5):
    from asyncio import async as ensure_future

EVENT_MQTT_PACKET_SENT = 'mqtt_packet_sent'
EVENT_MQTT_PACKET_RECEIVED = 'mqtt_packet_received'


class ProtocolHandler:
    """
    Class implementing the MQTT communication protocol using asyncio features
    """

    on_packet_sent = Signal()
    on_packet_received = Signal()

    def __init__(self, session: Session, plugins_manager: PluginManager, loop=None):
        log = logging.getLogger(__name__)
        self.logger = logging.LoggerAdapter(log, {'client_id': session.client_id})
        self.session = session
        self.reader = session.reader
        self.writer = session.writer
        self.plugins_manager = plugins_manager

        self.keepalive_timeout = self.session.keep_alive
        if self.keepalive_timeout <= 0:
            self.keepalive_timeout = None

        if loop is None:
            self._loop = asyncio.get_event_loop()
        else:
            self._loop = loop
        self._reader_task = None
        self._keepalive_task = None
        self._reader_ready = None
        self._reader_stopped = asyncio.Event(loop=self._loop)

        self._puback_waiters = dict()
        self._pubrec_waiters = dict()
        self._pubrel_waiters = dict()
        self._pubcomp_waiters = dict()

    @asyncio.coroutine
    def start(self):
        self._reader_ready = asyncio.Event(loop=self._loop)
        self._reader_task = asyncio.Task(self._reader_loop(), loop=self._loop)
        yield from asyncio.wait([self._reader_ready.wait()], loop=self._loop)
        if self.keepalive_timeout:
            self._keepalive_task = self._loop.call_later(self.keepalive_timeout, self.handle_write_timeout)

        self.logger.debug("Handler tasks started")
        yield from self.retry_deliveries()
        self.logger.debug("Handler ready")

    @asyncio.coroutine
    def stop(self):
        # Stop incoming messages flow waiter
        #for packet_id in self.session.inflight_in:
        #    self.session.inflight_in[packet_id].cancel()
        self._reader_task.cancel()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        self.logger.debug("waiting for tasks to be stopped")
        yield from asyncio.wait(
            [self._reader_stopped.wait()], loop=self._loop)
        self.logger.debug("closing writer")
        yield from self.writer.close()

    @asyncio.coroutine
    def retry_deliveries(self):
        """
        Handle [MQTT-4.4.0-1] by resending PUBLISH and PUBREL messages for pending out messages
        :return:
        """
        self.logger.debug("Begin messages delivery retries")
        ack_packets = []
        for packet_id in self.session.inflight_out:
            message = self.session.inflight_out[packet_id]
            if message.is_acknowledged():
                ack_packets.append(packet_id)
            else:
                if not message.pubrec_packet:
                    self.logger.debug("Retrying publish message Id=%d acknowledgment", packet_id)
                    message.publish_packet = PublishPacket.build(
                        message.topic,
                        message.data,
                        message.packet_id,
                        True,
                        message.qos,
                        message.retain)
                    yield from self._send_packet(message.publish_packet)
                yield from self._handle_message_flow(message)
        for packet_id in ack_packets:
            del self.session.inflight_out[packet_id]
        self.logger.debug("%d messages redelivered" % len(ack_packets))
        self.logger.debug("End messages delivery retries")

    @asyncio.coroutine
    def mqtt_publish(self, topic, data, qos, retain, ack_timeout=None):
        if qos in (QOS_1, QOS_2):
            packet_id = self.session.next_packet_id
            if packet_id in self.session.inflight_out:
                raise HBMQTTException("A message with the same packet ID '%d' is already in flight" % packet_id)
        else:
            packet_id = None

        message = OutgoingApplicationMessage(packet_id, topic, qos, data, retain)
        # Handle message flow
        yield from asyncio.wait_for(self._handle_message_flow(message), 10, loop=self._loop)
        return message

    @asyncio.coroutine
    def _handle_message_flow(self, app_message):
        """
        Handle protocol flow for incoming and outgoing messages, depending on service level and according to MQTT
        spec. paragraph 4.3-Quality of Service levels and protocol flows
        :param app_message: PublishMessage to handle
        :return: nothing.
        """
        if app_message.qos == QOS_0:
            yield from self._handle_qos0_message_flow(app_message)
        elif app_message.qos == QOS_1:
            yield from self._handle_qos1_message_flow(app_message)
        elif app_message.qos == QOS_2:
            yield from self._handle_qos2_message_flow(app_message)
        else:
            raise HBMQTTException("Unexcepted QOS value '%d" % str(app_message.qos))

    @asyncio.coroutine
    def _handle_qos0_message_flow(self, app_message):
        """
        Handle QOS_0 application message acknowledgment
        For incoming messages, this method stores the message
        For outgoing messages, this methods sends PUBLISH
        :param app_message:
        :return:
        """
        assert app_message.qos == QOS_0
        if isinstance(app_message, OutgoingApplicationMessage):
            packet = app_message.build_publish_packet()
            # Send PUBLISH packet
            yield from self._send_packet(packet)
            app_message.publish_packet = packet
        elif isinstance(app_message, IncomingApplicationMessage):
            if app_message.publish_packet.dup_flag:
                self.logger.warning("[MQTT-3.3.1-2] DUP flag must set to 0 for QOS 0 message. Message ignored: %s" %
                                    repr(app_message.publish_packet))
            else:
                yield from self.session.delivered_message_queue.put(app_message)

    @asyncio.coroutine
    def _handle_qos1_message_flow(self, app_message):
        """
        Handle QOS_1 application message acknowledgment
        For incoming messages, this method stores the message and reply with PUBACK
        For outgoing messages, this methods sends PUBLISH and waits for the corresponding PUBACK
        :param app_message:
        :return:
        """
        assert app_message.qos == QOS_1
        if app_message.puback_packet:
            raise HBMQTTException("Message '%d' has already been acknowledged" % app_message.packet_id)
        if isinstance(app_message, OutgoingApplicationMessage):
            if app_message.packet_id not in self.session.inflight_out:
                # Store message in session
                self.session.inflight_out[app_message.packet_id] = app_message
            if app_message.publish_packet is not None:
                # A Publish packet has already been sent, this is a retry
                publish_packet = app_message.build_publish_packet(dup=True)
            else:
                publish_packet = app_message.build_publish_packet()
            # Send PUBLISH packet
            yield from self._send_packet(publish_packet)
            app_message.publish_packet = publish_packet

            # Wait for puback
            waiter = asyncio.Future(loop=self._loop)
            self._puback_waiters[app_message.packet_id] = waiter
            yield from waiter
            del self._puback_waiters[app_message.packet_id]
            app_message.puback_packet = waiter.result()

            # Discard inflight message
            del self.session.inflight_out[app_message.packet_id]
        elif isinstance(app_message, IncomingApplicationMessage):
            # Initiate delivery
            self.logger.debug("Add message to delivery")
            yield from self.session.delivered_message_queue.put(app_message)
            # Send PUBACK
            puback = PubackPacket.build(app_message.packet_id)
            yield from self._send_packet(puback)
            app_message.puback_packet = puback

    @asyncio.coroutine
    def _handle_qos2_message_flow(self, app_message):
        """
        Handle QOS_2 application message acknowledgment
        For incoming messages, this method stores the message, sends PUBREC, waits for PUBREL, initiate delivery
        and send PUBCOMP
        For outgoing messages, this methods sends PUBLISH, waits for PUBREC, discards messages and wait for PUBCOMP
        :param app_message:
        :return:
        """
        assert app_message.qos == QOS_2
        if isinstance(app_message, OutgoingApplicationMessage):
            if app_message.pubrel_packet and app_message.pubcomp_packet:
                raise HBMQTTException("Message '%d' has already been acknowledged" % app_message.packet_id)
            if not app_message.pubrel_packet:
                # Store message
                if app_message.publish_packet is not None:
                    # This is a retry flow, no need to store just check the message exists in session
                    if app_message.packet_id not in self.session.inflight_out:
                        raise HBMQTTException("Unknown inflight message '%d' in session" % app_message.packet_id)
                    publish_packet = app_message.build_publish_packet(dup=True)
                else:
                    # Store message in session
                    self.session.inflight_out[app_message.packet_id] = app_message
                    publish_packet = app_message.build_publish_packet()
                # Send PUBLISH packet
                yield from self._send_packet(publish_packet)
                app_message.publish_packet = publish_packet
                # Wait PUBREC
                if app_message.packet_id in self._pubrec_waiters:
                    # PUBREC waiter already exists for this packet ID
                    message = "Can't add PUBREC waiter, a waiter already exists for message Id '%s'" \
                              % app_message.packet_id
                    self.logger.warning(message)
                    raise HBMQTTException(message)
                waiter = asyncio.Future(loop=self._loop)
                self._pubrec_waiters[app_message.packet_id] = waiter
                yield from waiter
                del self._pubrec_waiters[app_message.packet_id]
                app_message.pubrec_packet = waiter.result()
            if not app_message.pubcomp_packet:
                # Send pubrel
                app_message.pubrel_packet = PubrelPacket.build(app_message.packet_id)
                yield from self._send_packet(app_message.pubrel_packet)
                # Wait for PUBCOMP
                waiter = asyncio.Future(loop=self._loop)
                self._pubcomp_waiters[app_message.packet_id] = waiter
                yield from waiter
                del self._pubcomp_waiters[app_message.packet_id]
                app_message.pubcomp_packet = waiter.result()
            # Discard inflight message
            del self.session.inflight_out[app_message.packet_id]
        elif isinstance(app_message, IncomingApplicationMessage):
            self.session.inflight_in[app_message.packet_id] = app_message
            # Send pubrec
            pubrec_packet = PubrecPacket.build(app_message.packet_id)
            yield from self._send_packet(pubrec_packet)
            app_message.pubrec_packet = pubrec_packet
            # Wait PUBREL
            if app_message.packet_id in self._pubrel_waiters:
                # PUBREL waiter already exists for this packet ID
                message = "Can't add PUBREC waiter, a waiter already exists for message Id '%s'" \
                          % app_message.packet_id
                self.logger.warning(message)
                raise HBMQTTException(message)
            waiter = asyncio.Future(loop=self._loop)
            self._pubrel_waiters[app_message.packet_id] = waiter
            yield from waiter
            del self._pubrel_waiters[app_message.packet_id]
            app_message.pubrel_packet = waiter.result()
            # Initiate delivery and discard message
            yield from self.session.delivered_message_queue.put(app_message)
            del self.session.inflight_in[app_message.packet_id]
            # Send pubcomp
            pubcomp_packet = PubcompPacket.build(app_message.packet_id)
            yield from self._send_packet(pubcomp_packet)

    @asyncio.coroutine
    def _reader_loop(self):
        self.logger.debug("%s Starting reader coro" % self.session.client_id)
        running_tasks = collections.deque()
        while True:
            try:
                self._reader_ready.set()
                while running_tasks and running_tasks[0].done():
                    running_tasks.popleft()
                keepalive_timeout = self.session.keep_alive
                if keepalive_timeout <= 0:
                    keepalive_timeout = None
                fixed_header = yield from asyncio.wait_for(
                    MQTTFixedHeader.from_stream(self.reader),
                    keepalive_timeout, loop=self._loop)
                if fixed_header:
                    if fixed_header.packet_type == RESERVED_0 or fixed_header.packet_type == RESERVED_15:
                        self.logger.warning("%s Received reserved packet, which is forbidden: closing connection" %
                                         (self.session.client_id))
                        yield from self.handle_connection_closed()
                    else:
                        cls = packet_class(fixed_header)
                        packet = yield from cls.from_stream(self.reader, fixed_header=fixed_header)
                        yield from self.plugins_manager.fire_event(
                            EVENT_MQTT_PACKET_RECEIVED, packet=packet, session=self.session)
                        self._loop.call_soon(self.on_packet_received.send, packet)
                        task = None
                        if packet.fixed_header.packet_type == CONNACK:
                            task = asyncio.ensure_future(self.handle_connack(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == SUBSCRIBE:
                            task = asyncio.ensure_future(self.handle_subscribe(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == UNSUBSCRIBE:
                            task = asyncio.ensure_future(self.handle_unsubscribe(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == SUBACK:
                            task = asyncio.ensure_future(self.handle_suback(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == UNSUBACK:
                            task = asyncio.ensure_future(self.handle_unsuback(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PUBACK:
                            task = asyncio.ensure_future(self.handle_puback(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PUBREC:
                            task = asyncio.ensure_future(self.handle_pubrec(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PUBREL:
                            task = asyncio.ensure_future(self.handle_pubrel(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PUBCOMP:
                            task = asyncio.ensure_future(self.handle_pubcomp(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PINGREQ:
                            task = asyncio.ensure_future(self.handle_pingreq(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PINGRESP:
                            task = asyncio.ensure_future(self.handle_pingresp(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == PUBLISH:
                            task = asyncio.ensure_future(self.handle_publish(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == DISCONNECT:
                            task = asyncio.ensure_future(self.handle_disconnect(packet), loop=self._loop)
                        elif packet.fixed_header.packet_type == CONNECT:
                            self.handle_connect(packet)
                        else:
                            self.logger.warning("%s Unhandled packet type: %s" %
                                             (self.session.client_id, packet.fixed_header.packet_type))
                        if task:
                            running_tasks.append(task)
                else:
                    self.logger.debug("%s No more data (EOF received), stopping reader coro" % self.session.client_id)
                    break
            except asyncio.CancelledError:
                self.logger.debug("Task cancelled, reader loop ending")
                while running_tasks:
                    running_tasks.popleft().cancel()
                break
            except asyncio.TimeoutError:
                self.logger.debug("%s Input stream read timeout" % self.session.client_id)
                self.handle_read_timeout()
            except NoDataException:
                self.logger.debug("%s No data available" % self.session.client_id)
            except Exception as e:
                self.logger.warning("%s Unhandled exception in reader coro: %s" % (self.session.client_id, e))
                break
        yield from self.handle_connection_closed()
        self._reader_stopped.set()
        self.logger.debug("%s Reader coro stopped" % self.session.client_id)

    @asyncio.coroutine
    def _send_packet(self, packet):
        try:
            yield from packet.to_stream(self.writer)
            if self._keepalive_task:
                self._keepalive_task.cancel()
                self._keepalive_task = self._loop.call_later(self.keepalive_timeout, self.handle_write_timeout)

            yield from self.plugins_manager.fire_event(EVENT_MQTT_PACKET_SENT, packet=packet, session=self.session)
            self._loop.call_soon(self.on_packet_sent.send, packet)
        except ConnectionResetError as cre:
            yield from self.handle_connection_closed()
            raise
        except Exception as e:
            self.logger.warning("Unhandled exception: %s" % e)
            raise

    @asyncio.coroutine
    def mqtt_deliver_next_message(self):
        self.logger.debug("%d message(s) available for delivery" % self.session.delivered_message_queue.qsize())
        message = yield from self.session.delivered_message_queue.get()
        self.logger.debug("Delivering message %s" % message)
        return message

    @asyncio.coroutine
    def mqtt_acknowledge_delivery(self, packet_id):
        try:
            message = self.session.inflight_in[packet_id]
            message.acknowledge_delivery()
            self.logger.debug('Message delivery acknowledged, packed_id=%d' % packet_id)
        except KeyError:
            pass

    def handle_write_timeout(self):
        self.logger.debug('%s write timeout unhandled' % self.session.client_id)

    def handle_read_timeout(self):
        self.logger.debug('%s read timeout unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_connack(self, connack: ConnackPacket):
        self.logger.debug('%s CONNACK unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_connect(self, connect: ConnectPacket):
        self.logger.debug('%s CONNECT unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_subscribe(self, subscribe: SubscribePacket):
        self.logger.debug('%s SUBSCRIBE unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_unsubscribe(self, subscribe: UnsubscribePacket):
        self.logger.debug('%s UNSUBSCRIBE unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_suback(self, suback: SubackPacket):
        self.logger.debug('%s SUBACK unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_unsuback(self, unsuback: UnsubackPacket):
        self.logger.debug('%s UNSUBACK unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_pingresp(self, pingresp: PingRespPacket):
        self.logger.debug('%s PINGRESP unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_pingreq(self, pingreq: PingReqPacket):
        self.logger.debug('%s PINGREQ unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_disconnect(self, disconnect: DisconnectPacket):
        self.logger.debug('%s DISCONNECT unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_connection_closed(self):
        self.logger.debug('%s Connection closed unhandled' % self.session.client_id)

    @asyncio.coroutine
    def handle_puback(self, puback: PubackPacket):
        packet_id = puback.variable_header.packet_id
        try:
            waiter = self._puback_waiters[packet_id]
            waiter.set_result(puback)
        except KeyError:
            self.logger.warning("Received PUBACK for unknown pending message Id: '%d'" % packet_id)
        except InvalidStateError:
            self.logger.warning("PUBACK waiter with Id '%d' already done" % packet_id)

    @asyncio.coroutine
    def handle_pubrec(self, pubrec: PubrecPacket):
        packet_id = pubrec.packet_id
        try:
            waiter = self._pubrec_waiters[packet_id]
            waiter.set_result(pubrec)
        except KeyError:
            self.logger.warning("Received PUBREC for unknown pending message with Id: %d" % packet_id)
        except InvalidStateError:
            self.logger.warning("PUBREC waiter with Id '%d' already done" % packet_id)

    @asyncio.coroutine
    def handle_pubcomp(self, pubcomp: PubcompPacket):
        packet_id = pubcomp.packet_id
        try:
            waiter = self._pubcomp_waiters[packet_id]
            waiter.set_result(pubcomp)
        except KeyError:
            self.logger.warning("Received PUBCOMP for unknown pending message with Id: %d" % packet_id)
        except InvalidStateError:
            self.logger.warning("PUBCOMP waiter with Id '%d' already done" % packet_id)

    @asyncio.coroutine
    def handle_pubrel(self, pubrel: PubrelPacket):
        packet_id = pubrel.packet_id
        try:
            waiter = self._pubrel_waiters[packet_id]
            waiter.set_result(pubrel)
        except KeyError:
            self.logger.warning("Received PUBREL for unknown pending message with Id: %d" % packet_id)
        except InvalidStateError:
            self.logger.warning("PUBREL waiter with Id '%d' already done" % packet_id)

    @asyncio.coroutine
    def handle_publish(self, publish_packet: PublishPacket):
        packet_id = publish_packet.variable_header.packet_id
        qos = publish_packet.qos

        incoming_message = IncomingApplicationMessage(packet_id, publish_packet.topic_name, qos, publish_packet.data, publish_packet.retain_flag)
        incoming_message.publish_packet = publish_packet
        yield from self._handle_message_flow(incoming_message)
