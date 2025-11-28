import http.client
import json
import logging
import socket
import time
import urllib

from bridge.message import prepare_messages

logger = logging.getLogger(__name__)


def generate_nonce():
    """Generate nonce string - current UTC time as discord snowflake"""
    return str((int(time.time() * 1000) - 1420070400 * 1000) << 22)


class Discord():
    """Methods for fetching and sending data to Discord using REST API"""

    def __init__(self, token, host, cdn, name):
        host_obj = urllib.parse.urlsplit(host)
        if host_obj.netloc:
            self.host = host_obj.netloc
        else:
            self.host = host_obj.path
        self.name = name
        logger.debug(f"({self.name}) Endpoints: API={self.host}, CDN={cdn}")
        self.token = token
        self.header = {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
        }


    def get_connection(self, host, port):
        """Get connection object"""
        return http.client.HTTPSConnection(host, port, timeout=5)


    def get_messages(self, channel_id, num=50, before=None, after=None, around=None):
        """Get specified number of messages, optionally number before and after message ID"""
        message_data = None
        url = f"/api/v9/channels/{channel_id}/messages?limit={num}"
        if before:
            url += f"&before={before}"
        if after:
            url += f"&after={after}"
        if around:
            url += f"&around={around}"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("GET", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return None
        if response.status == 200:
            data = json.loads(response.read())
            connection.close()
            # debug_chat
            # with open("messages.json", "w") as f:
            #     json.dump(data, f, indent=2)
            return prepare_messages(data)
        logger.error(f"({self.name}) Failed to fetch messages. Response code: {response.status}")
        connection.close()
        return None


    def send_message(self, channel_id, message_content, reply_id=None, reply_channel_id=None, reply_guild_id=None, reply_ping=True, attachments=None, embeds=None, stickers=None):
        """Send a message in the channel with reply with or without ping"""
        message_dict = {
            "content": message_content,
            "tts": "false",
            "flags": 0,
            "nonce": generate_nonce(),
        }
        if reply_id and reply_channel_id:
            message_dict["message_reference"] = {
                "message_id": reply_id,
                "channel_id": reply_channel_id,
            }
            if reply_guild_id:
                message_dict["message_reference"]["guild_id"] = reply_guild_id
            if not reply_ping:
                if reply_guild_id:
                    message_dict["allowed_mentions"] = {
                        "parse": ["users", "roles", "everyone"],
                    }
                else:
                    message_dict["allowed_mentions"] = {
                        "parse": ["users", "roles", "everyone"],
                        "replied_user": False,
                    }
        if attachments:
            for attachment in attachments:
                if attachment["upload_url"]:
                    if "attachments" not in message_dict:
                        message_dict["attachments"] = []
                        message_dict["type"] = 0
                        message_dict["sticker_ids"] = []
                        message_dict["channel_id"] = channel_id
                        message_dict.pop("tts")
                        message_dict.pop("flags")
                    message_dict["attachments"].append({
                        "id": len(message_dict["attachments"]),
                        "filename": attachment["name"],
                        "uploaded_filename": attachment["upload_filename"],
                    })
        if embeds:
            message_dict["embeds"] = embeds
        if stickers:
            message_dict["sticker_ids"] = stickers
        message_data = json.dumps(message_dict)
        url = f"/api/v9/channels/{channel_id}/messages"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("POST", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return None
        if response.status == 200:
            message_id = json.loads(response.read())["id"]
            connection.close()
            return message_id
        logger.error(f"({self.name}) Failed to send message. Response code: {response.status}")
        connection.close()
        return None


    def send_update_message(self, channel_id, message_id, message_content, embeds):
        """Update the message in the channel"""
        message_dict = {
            "content": message_content,
        }
        if embeds:
            message_dict["embeds"] = embeds
        message_data = json.dumps(message_dict)
        url = f"/api/v9/channels/{channel_id}/messages/{message_id}"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("PATCH", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return False
        if response.status == 200:
            connection.close()
            return True
        logger.error(f"({self.name}) Failed to edit the message. Response code: {response.status}")
        connection.close()
        return False


    def send_delete_message(self, channel_id, message_id):
        """Delete the message from the channel"""
        message_data = None
        url = f"/api/v9/channels/{channel_id}/messages/{message_id}"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("DELETE", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return None
        if response.status != 204:
            logger.error(f"({self.name}) Failed to delete the message. Response code: {response.status}")
            connection.close()
            return False
        connection.close()
        return True


    def send_reaction(self, channel_id, message_id, reaction):
        """Send reaction to specified message"""
        encoded_reaction = urllib.parse.quote(reaction)
        message_data = None
        url = f"/api/v9/channels/{channel_id}/messages/{message_id}/reactions/{encoded_reaction}/%40me?location=Message%20Reaction%20Picker&type=0"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("PUT", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return None
        if response.status != 204:
            logger.error(f"({self.name}) Failed to send reaction: {reaction}. Response code: {response.status}")
            connection.close()
            return False
        connection.close()
        return True


    def remove_reaction(self, channel_id, message_id, reaction):
        """Remove reaction from specified message"""
        encoded_reaction = urllib.parse.quote(reaction)
        message_data = None
        url = f"/api/v9/channels/{channel_id}/messages/{message_id}/reactions/{encoded_reaction}/0/%40me?location=Message%20Inline%20Button&burst=false"
        try:
            connection = self.get_connection(self.host, 443)
            connection.request("DELETE", url, message_data, self.header)
            response = connection.getresponse()
        except (socket.gaierror, TimeoutError):
            connection.close()
            return None
        if response.status != 204:
            logger.error(f"({self.name}) Failed to delete reaction: {reaction}. Response code: {response.status}")
            connection.close()
            return False
        connection.close()
        return True
