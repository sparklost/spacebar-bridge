import json
import logging
import os
import signal
import sys
import threading
import time

from bridge import discord, formatter, gateway

logger = logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    filename="spacebar_bridge.log",
    encoding="utf-8",
    filemode="w",
    format="{asctime} - {levelname}\n  [{module}]: {message}\n",
    style="{",
    datefmt="%Y-%m-%d-%H:%M:%S",
)
ERROR_TEXT = "\nUnhandled exception occurred. Please report here: https://github.com/sparklost/spacebar-bridge/issues"


def get_author_name(message):
    """Get author name from message"""
    if message["nick"]:
        return message["nick"]
    if message["global_name"]:
        return message["global_name"]
    if message["username"]:
        return message["username"]
    return "Unknown"


def get_author_pfp(message, cdn_url, size=80):
    """Get author pfp url from message"""
    avatar_id = message["avatar_id"]
    if avatar_id:
        return f"https://{cdn_url}/avatars/{message["user_id"]}/{avatar_id}.webp?size={size}"
    return None


class Bridge:
    """Bridge class"""

    def __init__(self):
        with open("config.json", "r") as f:
            config = json.load(f)
        self.run = True

        if config["database"]["postgresql_host"]:
            self.init_postgresql(config)
        else:
            self.init_sqlite(config)

        host_a = config["discord"]["host"]
        self.cdn_a = config["discord"]["cdn_host"]
        token_a = config["discord"]["token"]
        host_b = config["spacebar"]["host"]
        self.cdn_b = config["spacebar"]["cdn_host"]
        token_b = config["spacebar"]["token"]
        bridges = config["bridges"]
        self.channels = []   # should be loaded from gateway when guild_create event is parsed
        self.roles = []   # this too

        custom_status = config["custom_status"]
        custom_status_emoji = config["custom_status_emoji"]

        self.guild_id_a = config["discord_guild_id"]
        self.guild_id_b = config["spacebar_guild_id"]

        self.channels_a = []
        self.bridges_a = {}
        self.bridges_a_txt = []
        self.channels_b = []
        self.bridges_b = {}
        self.bridges_b_txt = []
        for bridge in bridges:
            a = bridge["discord_channel_id"]
            b = bridge["spacebar_channel_id"]
            self.channels_a.append(a)
            self.bridges_a[a] = b
            self.bridges_a_txt.append(f"pair_{a}_{b}")
            self.database_a.create_table(f"pair_{a}_{b}")
            self.channels_b.append(b)
            self.bridges_b[b] = a
            self.bridges_b_txt.append(f"pair_{b}_{a}")
            self.database_b.create_table(f"pair_{b}_{a}")

        print("Connecting to gateways")
        self.discord_a = discord.Discord(token_a, host_a, self.cdn_a, "Discord")
        self.gateway_a = gateway.Gateway(token_a, host_a, "Discord")
        self.gateway_a.connect()
        self.discord_b = discord.Discord(token_b, host_b, self.cdn_b, "Spacebar")
        self.gateway_b = gateway.Gateway(token_b, host_b, "Spacebar", compressed=False)
        self.gateway_b.connect()

        while not (self.gateway_a.get_ready() and self.gateway_b.get_ready()):
            if self.gateway_a.error:
                logger.fatal(f"Gateway A error: \n {self.gateway_a.error}")
                sys.exit(self.gateway_a.error + ERROR_TEXT)
            if self.gateway_b.error:
                logger.fatal(f"Gateway B error: \n {self.gateway_b.error}")
                sys.exit(self.gateway_b.error + ERROR_TEXT)
            if not self.gateway_a.run or not self.gateway_b.run:
                sys.exit()
            time.sleep(0.2)

        self.my_id_a = self.gateway_a.get_my_id()
        self.my_id_b = self.gateway_b.get_my_id()

        self.gateway_a.update_presence(
            status="online",
            custom_status=custom_status,
            custom_status_emoji=custom_status_emoji,
        )
        # self.gateway_b.update_presence(   # not supported by spacebar
        #     status="online",
        #     custom_status=custom_status,
        #     custom_status_emoji=custom_status_emoji,
        # )

        logger.info("Bridge initialized successfully")
        print("Bridge initialized successfully")

        threading.Thread(target=self.loop_b, daemon=True).start()
        self.loop_a()


    def init_sqlite(self, config):
        """Initialize SQLite database"""
        print("Initializing database")
        from bridge import database
        database_path = os.path.expanduser(config["database"]["dir_path"])
        cleanup_days = config["database"]["cleanup_days"]
        pair_lifetime_days = config["database"]["pair_lifetime_days"]
        if not os.path.exists(database_path):
            os.makedirs(database_path, exist_ok=True)
        databse_path_a = os.path.join(database_path, "discord.db")
        databse_path_b = os.path.join(database_path, "spacebar.db")
        self.database_a = database.PairStore(databse_path_a, cleanup_days, pair_lifetime_days, name="Discord")
        self.database_b = database.PairStore(databse_path_b, cleanup_days, pair_lifetime_days, name="Spacebar")


    def init_postgresql(self, config):
        """"Connect to PostgreSQL database"""
        print("Connecting to postgres databse")
        from bridge import database_postgres
        host = config["database"]["postgresql_host"]
        user = config["database"]["postgresql_user"]
        password = config["database"]["postgresql_password"]
        cleanup_days = config["database"]["cleanup_days"]
        pair_lifetime_days = config["database"]["pair_lifetime_days"]
        self.database_a = database_postgres.PairStore(host, user, password, "bridge_discord_msgs", cleanup_days, pair_lifetime_days, name="Discord")
        self.database_b = database_postgres.PairStore(host, user, password, "bridge_spacebar_msgs", cleanup_days, pair_lifetime_days, name="Spacebar")


    def loop_a(self):   # DISCORD -> SPACEBAR
        """Loop A"""
        while self.run:

            # get messages
            while self.run:
                new_message = self.gateway_a.get_messages()
                if new_message:
                    data = new_message["d"]
                    if data["channel_id"] in self.channels_a and data.get("user_id") != self.my_id_a:
                        op = new_message["op"]

                        if op == "MESSAGE_CREATE":
                            # build message
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_a[source_channel]
                            source_message = data["id"]
                            author_name = get_author_name(data)
                            author_pfp = get_author_pfp(data, self.cdn_a)
                            if data["referenced_message"]:
                                source_reference_id = data["referenced_message"]["id"]
                                if data["referenced_message"]["user_id"] == self.my_id_a:
                                    channel_pair = f"pair_{target_channel}_{source_channel}"
                                    target_reference_id = self.database_b.get_source(channel_pair, source_reference_id)
                                else:
                                    channel_pair = f"pair_{source_channel}_{target_channel}"
                                    target_reference_id = target_message = self.database_a.get_target(channel_pair, source_reference_id)
                                for mention in data["referenced_message"]["mentions"]:
                                    if mention["id"] == self.my_id_a:
                                        reply_ping = True
                                        break
                                else:
                                    reply_ping = False
                            else:
                                target_reference_id = None
                                reply_ping = True
                            message_text = formatter.build_message(
                                data,
                                self.roles,
                                self.channels,
                            )
                            if not message_text:
                                message_text = "*Unknown message content*"
                            embeds = [{
                                "type": "rich",
                                "author": {
                                    "name": author_name,
                                },
                                "description": message_text,
                            }]
                            if author_pfp:
                                embeds[0]["author"]["icon_url"] = author_pfp
                            # send message
                            target_message = self.discord_b.send_message(
                                channel_id=target_channel,
                                message_content="",
                                reply_id=target_reference_id,
                                reply_channel_id=target_channel,
                                reply_guild_id=self.guild_id_b,
                                reply_ping=reply_ping,
                                embeds=embeds,
                            )
                            # add to db
                            if target_message:
                                logger.debug(f"CREATE (A): = {source_channel} > {target_channel} = [{author_name}] - ({source_message}) - {message_text}")
                                channel_pair = f"pair_{source_channel}_{target_channel}"
                                if channel_pair in self.bridges_a_txt:
                                    self.database_a.add_pair(channel_pair, source_message, target_message)
                                else:
                                    logger.warning(f"Channel pair (A): {channel_pair} not initialized")

                        elif op == "MESSAGE_UPDATE":
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_a[source_channel]
                            channel_pair = f"pair_{source_channel}_{target_channel}"
                            if channel_pair in self.bridges_a_txt:
                                source_message = data["id"]
                                target_message = self.database_a.get_target(channel_pair, source_message)
                                if target_message:
                                    author_name = get_author_name(data)
                                    author_pfp = get_author_pfp(data, self.cdn_a)
                                    message_text = formatter.build_message(
                                        data,
                                        self.roles,
                                        self.channels,
                                    )
                                    if not message_text:
                                        message_text = "*Unknown message content*"
                                    embeds = [{
                                        "type": "rich",
                                        "author": {
                                            "name": author_name,
                                        },
                                        "description": message_text,
                                    }]
                                    if author_pfp:
                                        embeds[0]["author"]["icon_url"] = author_pfp
                                    self.discord_b.send_update_message(
                                        channel_id=target_channel,
                                        message_id=target_message,
                                        message_content="",
                                        embeds=embeds,
                                    )
                                    logger.debug(f"EDIT (A): = {source_channel} > {target_channel} = [{author_name}] - ({source_message}) - {message_text}")
                            else:
                                logger.warning(f"Channel pair (A): {channel_pair} not initialized")

                        elif op == "MESSAGE_DELETE":
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_a[source_channel]
                            channel_pair = f"pair_{source_channel}_{target_channel}"
                            if channel_pair in self.bridges_a_txt:
                                source_message = data["id"]
                                target_message = self.database_a.get_target(channel_pair, source_message)
                                if target_message:
                                    self.discord_b.send_delete_message(target_channel, target_message)
                                    logger.debug(f"DELETE (A): = {source_channel} > {target_channel} = ({source_message})")
                                    self.database_a.delete_pair(channel_pair, source_message)
                            else:
                                logger.warning(f"Channel pair (A): {channel_pair} not initialized")

                        elif op == "MESSAGE_REACTION_ADD":
                            # A receives reaction_add
                            # B reacts to itself if not already
                            pass

                        elif op == "MESSAGE_REACTION_REMOVE":
                            # A receives reaction_delete
                            # check if this is last non-self reaction
                            #     B removes self reaction
                            pass

                else:
                    break

            # check gateway for errors
            if self.gateway_a.error:
                logger.fatal(f"Gateway error: \n {self.gateway_a.error}")
                sys.exit(self.gateway_a.error + ERROR_TEXT)

            time.sleep(0.1)   # some reasonable delay
        self.run = False


    def loop_b(self):   # SPACEBAR -> DISCORD
        """Loop B"""
        while self.run:

            # get messages
            while self.run:
                new_message = self.gateway_b.get_messages()
                if new_message:
                    data = new_message["d"]
                    if data["channel_id"] in self.channels_b and data.get("user_id") != self.my_id_b:
                        op = new_message["op"]

                        if op == "MESSAGE_CREATE":
                            # build message
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_b[source_channel]
                            source_message = data["id"]
                            author_name = get_author_name(data)
                            author_pfp = get_author_pfp(data, self.cdn_b)
                            if data["referenced_message"]:
                                source_reference_id = data["referenced_message"]["id"]
                                if data["referenced_message"]["user_id"] == self.my_id_b:
                                    channel_pair = f"pair_{target_channel}_{source_channel}"
                                    target_reference_id = self.database_a.get_source(channel_pair, source_reference_id)
                                else:
                                    channel_pair = f"pair_{source_channel}_{target_channel}"
                                    target_reference_id = target_message = self.database_b.get_target(channel_pair, source_reference_id)
                                for mention in data["referenced_message"]["mentions"]:
                                    if mention["id"] == self.my_id_b:
                                        reply_ping = True
                                        break
                                else:
                                    reply_ping = False
                            else:
                                target_reference_id = None
                                reply_ping = True
                            # build message
                            message_text = formatter.build_message(
                                data,
                                self.roles,
                                self.channels,
                            )
                            if not message_text:
                                message_text = "*Unknown message content*"
                            embeds = [{
                                "type": "rich",
                                "author": {
                                    "name": author_name,
                                },
                                "description": message_text,
                            }]
                            if author_pfp:
                                embeds[0]["author"]["icon_url"] = author_pfp
                            # send message
                            target_message = self.discord_a.send_message(
                                channel_id=target_channel,
                                message_content="",
                                reply_id=target_reference_id,
                                reply_channel_id=target_channel,
                                reply_guild_id=self.guild_id_a,
                                reply_ping=reply_ping,
                                embeds=embeds,
                            )
                            # add to db
                            if target_message:
                                logger.debug(f"CREATE (B): {source_channel}-{source_message} > {target_channel}={target_message} = [{author_name}] - {message_text}")
                                channel_pair = f"pair_{source_channel}_{target_channel}"
                                if channel_pair in self.bridges_b_txt:
                                    self.database_b.add_pair(channel_pair, source_message, target_message)
                                else:
                                    logger.warning(f"Channel pair (B): {channel_pair} not initialized")

                        elif op == "MESSAGE_UPDATE":
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_b[source_channel]
                            channel_pair = f"pair_{source_channel}_{target_channel}"
                            if channel_pair in self.bridges_b_txt:
                                source_message = data["id"]
                                target_message = self.database_b.get_target(channel_pair, source_message)
                                if target_message:
                                    author_name = get_author_name(data)
                                    author_pfp = get_author_pfp(data, self.cdn_b)
                                    message_text = formatter.build_message(
                                        data,
                                        self.roles,
                                        self.channels,
                                    )
                                    if not message_text:
                                        message_text = "*Unknown message content*"
                                    embeds = [{
                                        "type": "rich",
                                        "author": {
                                            "name": author_name,
                                        },
                                        "description": message_text,
                                    }]
                                    if author_pfp:
                                        embeds[0]["author"]["icon_url"] = author_pfp
                                    self.discord_a.send_update_message(
                                        channel_id=target_channel,
                                        message_id=target_message,
                                        message_content="",
                                        embeds=embeds,
                                    )
                                    logger.debug(f"EDIT (B): = {source_channel}-{source_message} > {target_channel}={target_message} = [{author_name}] - {message_text}")
                            else:
                                logger.warning(f"Channel pair (B): {channel_pair} not initialized")

                        elif op == "MESSAGE_DELETE":
                            source_channel = data["channel_id"]
                            target_channel = self.bridges_b[source_channel]
                            channel_pair = f"pair_{source_channel}_{target_channel}"
                            if channel_pair in self.bridges_b_txt:
                                source_message = data["id"]
                                target_message = self.database_b.get_target(channel_pair, source_message)
                                if target_message:
                                    self.discord_a.send_delete_message(target_channel, target_message)
                                    logger.debug(f"DELETE (B): = {source_channel} > {target_channel} = ({source_message})")
                                    self.database_b.delete_pair(channel_pair, source_message)
                            else:
                                logger.warning(f"Channel pair (B): {channel_pair} not initialized")

                        elif op == "MESSAGE_REACTION_ADD":
                            pass

                        elif op == "MESSAGE_REACTION_REMOVE":
                            pass

                else:
                    break

            # check gateway for errors
            if self.gateway_a.error:
                logger.fatal(f"Gateway error: \n {self.gateway_b.error}")
                sys.exit(self.gateway_b.error + ERROR_TEXT)

            time.sleep(0.1)   # some reasonable delay
        self.run = False



def sigint_handler(_signum, _frame):
    """Handling Ctrl-C event"""
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, sigint_handler)
    bridge = Bridge()
