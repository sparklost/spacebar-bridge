import http.client
import json
import logging
import random
import socket
import struct
import sys
import threading
import time
import traceback
import urllib
import urllib.parse
import zlib

import websocket

from bridge.message import prepare_message

DISCORD_HOST = "discord.com"
ZLIB_SUFFIX = b"\x00\x00\xff\xff"
inflator = zlib.decompressobj()
logger = logging.getLogger(__name__)


def zlib_decompress(data):
    """Decompress zlib data, if it is not zlib compressed, return data instead"""
    buffer = bytearray()
    buffer.extend(data)
    if len(data) < 4 or data[-4:] != ZLIB_SUFFIX:
        return data
    try:
        return inflator.decompress(buffer)
    except zlib.error as e:
        logger.error(f"zlib error: {e}")
        return None


def reset_inflator():
    """Resets inflator object"""
    global inflator
    del inflator
    inflator = zlib.decompressobj()   # noqa


class Gateway():
    """Methods for fetching and sending data to Discord gateway through websocket"""

    def __init__(self, token, host, name, compressed=True):
        if host:
            host_obj = urllib.parse.urlsplit(host)
            if host_obj.netloc:
                self.host = host_obj.netloc
            else:
                self.host = host_obj.path
        else:
            self.host = DISCORD_HOST
        self.header = [
            "Connection: keep-alive, Upgrade",
            "Sec-WebSocket-Extensions: permessage-deflate",
            "User-Agent: endcord",
        ]
        self.name = name
        self.compressed = compressed
        self.init_time = time.time() * 1000
        self.token = token
        self.run = True
        self.wait = False
        self.heartbeat_received = True
        self.sequence = None
        self.resume_gateway_url = ""
        self.session_id = ""
        self.ready = False
        self.my_id = None
        self.messages_buffer = []
        self.reconnect_requested = False
        self.legacy = False
        self.error = None
        self.resumable = False
        threading.Thread(target=self.thread_guard, daemon=True, args=()).start()


    def thread_guard(self):
        """
        Check if reconnect is requested and run reconnect thread if its not running.
        This is one in main thread so other threads are not further recursing when
        reconnecting multiple times.
        """
        while self.run:
            if self.reconnect_requested:
                self.reconnect_requested = False
                if not self.reconnect_thread.is_alive():
                    self.reconnect_thread = threading.Thread(target=self.reconnect, daemon=True, args=())
                    self.reconnect_thread.start()
            time.sleep(0.5)


    def connect_ws(self, resume=False):
        """Connect to websocket"""
        if resume and self.resume_gateway_url:
            gateway_url = self.resume_gateway_url
        else:
            gateway_url = self.gateway_url
        self.ws = websocket.WebSocket()
        if self.compressed:
            self.ws.connect(gateway_url + "/?v=9&encoding=json&compress=zlib-stream", header=self.header)
        else:
            self.ws.connect(gateway_url + "/?v=9&encoding=json", header=self.header)


    def connect(self):
        """Create initial connection to Discord gateway"""
        connection = http.client.HTTPSConnection(self.host, 443)

        # get gateway url
        try:
            # subscribe works differently in v10
            connection.request("GET", "/api/v9/gateway")
        except (socket.gaierror, TimeoutError):
            connection.close()
            logger.warning(f"({self.name}) No internet connection. Exiting...")
            raise SystemExit("No internet connection. Exiting...")
        response = connection.getresponse()
        if response.status == 200:
            data = response.read()
            connection.close()
            self.gateway_url = json.loads(data)["url"]
        else:
            connection.close()
            logger.error(f"({self.name}) Failed to get gateway url. Response code: {response.status}. Exiting...")
            raise SystemExit(f"Failed to get gateway url. Response code: {response.status}. Exiting...")

        self.connect_ws()
        data = self.ws.recv()
        if self.compressed:
            data = zlib_decompress(data)
        if data:
            self.heartbeat_interval = int(json.loads(data)["d"]["heartbeat_interval"])
        else:
            self.heartbeat_interval = 41250
        self.receiver_thread = threading.Thread(target=self.safe_function_wrapper, daemon=True, args=(self.receiver, ))
        self.receiver_thread.start()
        self.heartbeat_thread = threading.Thread(target=self.send_heartbeat, daemon=True)
        self.heartbeat_thread.start()
        self.reconnect_thread = threading.Thread()
        self.authenticate()


    def safe_function_wrapper(self, function, args=()):
        """
        Wrapper for a function running in a thread that captures error and stores it for later use.
        Error can be accessed from main loop and handled there.
        """
        try:
            function(*args)
        except BaseException as e:
            self.error = f"({self.name})" + "".join(traceback.format_exception(e))


    def send(self, request):
        """Send data to gateway"""
        try:
            self.ws.send(json.dumps(request))
        except websocket._exceptions.WebSocketException:
            self.reconnect_requested = True


    def receiver(self):
        """Receive and handle all traffic from gateway, should be run in a thread"""
        logger.info(f"({self.name}) Receiver started")
        self.resumable = False
        abnormal = False
        while self.run and not self.wait:
            try:
                ws_opcode, data = self.ws.recv_data()
            except (
                ConnectionResetError,
                websocket._exceptions.WebSocketConnectionClosedException,
                OSError,
            ):
                self.resumable = True
                break
            if ws_opcode == 8 and len(data) >= 2:
                if not data:
                    self.resumable = True
                    break
                code = struct.unpack("!H", data[0:2])[0]
                reason = data[2:].decode("utf-8", "replace")
                logger.warning(f"({self.name}) Gateway error code: {code}, reason: {reason}")
                self.resumable = code in (4000, 4009)
                if code == 4004:
                    self.run = False
                    print(f"{self.name} token is invalid")
                break
            try:
                if self.compressed:
                    data = zlib_decompress(data)
                if data:
                    try:
                        response = json.loads(data)
                        opcode = response["op"]
                    except ValueError:
                        response = None
                        opcode = None
                else:
                    response = None
                    opcode = None
            except Exception as e:
                logger.warning(f"({self.name}) Receiver error: {e}")
                self.resumable = True
                break
            logger.debug(f"({self.name}) Received: opcode={opcode}, optext={response["t"] if (response and "t" in response and response["t"] and "LIST" not in response["t"]) else 'None'}")
            # debug_events
            # if response.get("t"):
            #     debug.save_json(response, f"{response["t"]}.json", False)

            if opcode == 11:
                self.heartbeat_received = True

            elif opcode == 10:
                self.heartbeat_interval = int(response["d"]["heartbeat_interval"])

            elif opcode == 1:
                self.send({"op": 1, "d": self.sequence})

            elif opcode == 0:
                self.sequence = int(response["s"])
                optext = response["t"]
                data = response["d"]

                if optext == "READY":
                    self.resume_gateway_url = data["resume_gateway_url"]
                    self.session_id = data["session_id"]
                    self.my_id = data["user"]["id"]
                    self.ready = True

                elif optext == "MESSAGE_CREATE":
                    message = response["d"]
                    message_done = prepare_message(message)
                    message_done.update({
                        "channel_id": message["channel_id"],
                        "guild_id": message.get("guild_id"),
                    })
                    self.messages_buffer.append({
                        "op": "MESSAGE_CREATE",
                        "d": message_done,
                    })

                elif optext == "MESSAGE_UPDATE":
                    message = response["d"]
                    message_done = prepare_message(message)
                    message_done.update({
                        "channel_id": message["channel_id"],
                        "guild_id": message.get("guild_id"),
                    })
                    self.messages_buffer.append({
                        "op": "MESSAGE_UPDATE",
                        "d": message_done,
                    })

                elif optext == "MESSAGE_DELETE":
                    ready_data = {
                        "id": data["id"],
                        "channel_id": data["channel_id"],
                        "guild_id": data.get("guild_id"),
                    }
                    self.messages_buffer.append({
                        "op": "MESSAGE_DELETE",
                        "d": ready_data,
                    })

                elif optext == "MESSAGE_REACTION_ADD":
                    if "member" in data and "user" in data["member"]:   # spacebar_fix - "user" is mising
                        user_id = data["member"]["user"]["id"]
                        username = data["member"]["user"]["username"]
                        global_name = data["member"]["user"].get("global_name")   # spacebar_fix - get
                        nick = data["member"]["user"].get("nick")
                    else:
                        user_id = data["user_id"]
                        username = None
                        global_name = None
                        nick = None
                    ready_data = {
                        "id": data["message_id"],
                        "channel_id": data["channel_id"],
                        "guild_id": data.get("guild_id"),
                        "emoji": data["emoji"]["name"],
                        "emoji_id": data["emoji"].get("id"),   # spacebar_fix - get
                        "user_id": user_id,
                        "username": username,
                        "global_name": global_name,
                        "nick": nick,
                    }
                    self.messages_buffer.append({
                        "op": "MESSAGE_REACTION_ADD",
                        "d": ready_data,
                    })

                elif optext == "MESSAGE_REACTION_ADD_MANY":
                    channel_id = data["channel_id"]
                    guild_id = data.get("guild_id")
                    message_id = data["message_id"]
                    for reaction in data["reactions"]:
                        for user_id in reaction["users"]:
                            ready_data = {
                                "id": message_id,
                                "channel_id": channel_id,
                                "guild_id": guild_id,
                                "emoji": reaction["emoji"]["name"],
                                "emoji_id": reaction["emoji"]["id"],
                                "user_id": user_id,
                                "username": None,
                                "global_name": None,
                                "nick": None,
                            }
                            self.messages_buffer.append({
                                "op": "MESSAGE_REACTION_ADD",
                                "d": ready_data,
                            })

                elif optext == "MESSAGE_REACTION_REMOVE":
                    ready_data = {
                        "id": data["message_id"],
                        "channel_id": data["channel_id"],
                        "guild_id": data.get("guild_id"),
                        "emoji": data["emoji"]["name"],
                        "emoji_id": data["emoji"].get("id"),   # spacebar_fix - get
                        "user_id": data["user_id"],
                    }
                    self.messages_buffer.append({
                        "op": "MESSAGE_REACTION_REMOVE",
                        "d": ready_data,
                    })


            elif opcode == 7:
                logger.info(f"({self.name}) Host requested reconnect")
                self.resumable = True
                break

            elif opcode == 9:
                logger.info(f"({self.name}) Session invalidated, reconnecting")
                break

            if abnormal:
                self.resumable = True
                break

        logger.info(f"({self.name}) Receiver stopped")
        self.reconnect_requested = True
        self.heartbeat_running = False


    def send_heartbeat(self):
        """Send heartbeat to gateway, if response is not received, triggers reconnect, should be run in a thread"""
        logger.info(f"({self.name}) Heartbeater started, interval={self.heartbeat_interval/1000}s")
        self.heartbeat_running = True
        self.heartbeat_received = True
        heartbeat_interval_rand = int(self.heartbeat_interval * (0.8 - 0.6 * random.random()) / 1000)
        heartbeat_sent_time = int(time.time())
        while self.run and not self.wait and self.heartbeat_running:
            if time.time() - heartbeat_sent_time >= heartbeat_interval_rand:
                self.send({"op": 1, "d": self.sequence})
                heartbeat_sent_time = int(time.time())
                logger.debug(f"({self.name}) Sent heartbeat")
                if not self.heartbeat_received:
                    logger.warning(f"({self.name}) Heartbeat reply not received")
                    self.resumable = True
                    break
                self.heartbeat_received = False
                heartbeat_interval_rand = int(self.heartbeat_interval * (0.8 - 0.6 * random.random()) / 1000)
            # sleep(heartbeat_interval * jitter), but jitter is limited to (0.1 - 0.9)
            # in this time heartbeat ack should be received from discord
            time.sleep(1)
        logger.info(f"({self.name}) Heartbeater stopped")
        self.reconnect_requested = True


    def authenticate(self):
        """Authenticate client with discord gateway"""
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {
                    "os": sys.platform,
                    "browser": "endcord",
                    "device": "endcord",
                },
                "intents": 1536,
                "presence": {
                    "activities": [],
                    "status": "online",
                    "since": None,
                    "afk": False,
                },
            },
        }
        self.send(payload)
        logger.debug(f"({self.name}) Sent identify")


    def resume(self):
        """
        Try to resume discord gateway session on url provided by Discord in READY event.
        Return gateway response code, 9 means resumming has failed
        """
        self.ws.close(timeout=0)   # this will stop receiver
        time.sleep(1)   # so receiver ends before opening new socket
        reset_inflator()   # otherwise decompression wont work
        self.ws = websocket.WebSocket()
        try:
            self.connect_ws(resume=True)
        except websocket._exceptions.WebSocketBadStatusException:
            logger.info(f"({self.name}) Failed to resume connection")
            return 9
        if self.compressed:
            _ = zlib_decompress(self.ws.recv())
        else:
            _ = self.ws.recv()
        payload = {"op": 6, "d": {"token": self.token, "session_id": self.session_id, "seq": self.sequence}}
        self.send(payload)
        try:
            if self.compressed:
                op = json.loads(zlib_decompress(self.ws.recv()))["op"]
            else:
                op = json.loads(self.ws.recv())["op"]
            logger.info(f"({self.name}) Connection resumed")
            return op
        except (json.decoder.JSONDecodeError, websocket._exceptions.WebSocketConnectionClosedException):
            logger.info(f"({self.name}) Failed to resume connection")
            return 9


    def reconnect(self):
        """Try to resume session, if cant, create new one"""
        if not self.wait:
            logger.info(f"({self.name}) Trying to reconnect")
        try:
            code = None
            if self.resumable:
                self.resumable = False
                code = self.resume()
            if code == 9:
                self.ws.close(timeout=0)   # this will stop receiver
                time.sleep(1)   # so receiver ends before opening new socket
                reset_inflator()   # otherwise decompression wont work
                self.ready = False   # will receive new ready event
                self.ws = websocket.WebSocket()
                self.connect_ws()
                self.authenticate()
                logger.info(f"({self.name}) Restarting connection")
            self.wait = False
            # restarting threads
            if not self.receiver_thread.is_alive():
                self.receiver_thread = threading.Thread(target=self.safe_function_wrapper, daemon=True, args=(self.receiver, ))
                self.receiver_thread.start()
            if not self.heartbeat_thread.is_alive():
                self.heartbeat_thread = threading.Thread(target=self.send_heartbeat, daemon=True)
                self.heartbeat_thread.start()
            logger.info(f"({self.name}) Connection established")
        except websocket._exceptions.WebSocketAddressException:
            if not self.wait:   # if not running from wait_oline
                logger.warning(f"({self.name}) No internet connection")
                self.ws.close()
                threading.Thread(target=self.wait_online, daemon=True, args=()).start()


    def wait_online(self):
        """Wait for network, try to reconnect every 5s"""
        self.wait = True
        while self.run and self.wait:
            self.reconnect_requested = True
            time.sleep(5)


    def update_presence(self, status, custom_status=None, custom_status_emoji=None):
        """Update client status. Statuses: 'online', 'idle', 'dnd', 'invisible', 'offline'"""
        activities = []
        if custom_status:
            activities.append({
                "name": "Custom Status",
                "type": 4,
                "state": custom_status,
            })
            if custom_status_emoji:
                activities[0]["emoji"] = custom_status_emoji
        payload = {
            "op": 3,
            "d": {
                "status": status,
                "afk": "false",
                "since": 0,
                "activities": activities,
            },
        }
        self.send(payload)
        logger.debug(f"({self.name}) Updated presence")


    def get_ready(self):
        """Return wether gateway processed entire READY event"""
        return self.ready


    def get_my_id(self):
        """Get my discord user ID"""
        return self.my_id


    def get_messages(self):
        """
        Get message CREATE, EDIT, DELETE and ACK events for every guild and channel.
        Returns 1 by 1 event as an update for list of messages.
        """
        if len(self.messages_buffer) == 0:
            return None
        return self.messages_buffer.pop(0)
