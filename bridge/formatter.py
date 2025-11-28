import re
import time

match_d_emoji = re.compile(r"<(.?):(.*?):(\d*?)>")
match_mention = re.compile(r"<@(\d*?)>")
match_role = re.compile(r"<@&(\d*?)>")
match_channel = re.compile(r"<#(\d*?)>")
match_discord_channel_url = re.compile(r"https:\/\/discord\.com\/channels\/(\d*)\/(\d*)(?:\/(\d*))?")


def replace_discord_emoji(text):
    """
    Transform emoji strings into nicer looking ones:
    `some text <:emoji_name:emoji_id> more text` --> `some text :emoji_name: more text`
    """
    result = []
    last_pos = 0
    for match in re.finditer(match_d_emoji, text):
        result.append(text[last_pos:match.start()])
        result.append(f":{match.group(2)}:")
        last_pos = match.end()
    result.append(text[last_pos:])
    return "".join(result)


def replace_mentions(text, usernames_ids):
    """
    Transforms mention string into nicer looking one:
    `some text <@user_id> more text` --> `some text @username more text`
    """
    result = []
    last_pos = 0
    for match in re.finditer(match_mention, text):
        result.append(text[last_pos:match.start()])
        for user in usernames_ids:
            if match.group(1) == user["id"]:
                result.append(f"@{user["username"]}")
                break
        last_pos = match.end()
    result.append(text[last_pos:])
    return "".join(result)


def replace_roles(text, roles_ids):
    """
    Transforms roles string into nicer looking one:
    `some text <@role_id> more text` --> `some text @role_name more text`
    """
    result = []
    last_pos = 0
    for match in re.finditer(match_role, text):
        result.append(text[last_pos:match.start()])
        for role in roles_ids:
            if match.group(1) == role["id"]:
                result.append(f"@{role["name"]}")
                break
        else:
            result.append("@unknown_role")
        last_pos = match.end()
    result.append(text[last_pos:])
    return "".join(result)


def replace_discord_url(text):
    """Replace discord url for channel and message"""
    result = []
    last_pos = 0
    for match in re.finditer(match_discord_channel_url, text):
        result.append(text[last_pos:match.start()])
        if match.group(3):
            result.append(f"<#{match.group(2)}>>MSG")
        else:
            result.append(f"<#{match.group(2)}>")
        last_pos = match.end()
    result.append(text[last_pos:])
    return "".join(result)


def replace_channels(text, channels_ids):
    """
    Transforms channels string into nicer looking one:
    `some text <#channel_id> more text` --> `some text #channel_name more text`
    """
    result = []
    last_pos = 0
    for match in re.finditer(match_channel, text):
        result.append(text[last_pos:match.start()])
        for channel in channels_ids:
            if match.group(1) == channel["id"]:
                result.append(f"#{channel["name"]}")
                break
        else:
            result.append("@unknown_channel")
        last_pos = match.end()
    result.append(text[last_pos:])
    return "".join(result)


def clean_type(embed_type):
    r"""
    Clean embed type string from excessive information
    eg. `image\png` ---> `image`
    """
    return embed_type.split("/")[0]


def format_poll(poll):
    """Generate message text from poll data"""
    if poll["expires"] < time.time():
        status = "ended"
        expires = "Ended"
    else:
        status = "ongoing"
        expires = "Ends"
    content_list = [
        f"*Poll ({status}):*",
        poll["question"],
    ]
    total_votes = 0
    for option in poll["options"]:
        total_votes += int(option["count"])
    for option in poll["options"]:
        if total_votes:
            answer_votes = option["count"]
            percent = round((answer_votes / total_votes) * 100)
        else:
            answer_votes = 0
            percent = 0
        content_list.append(f"  {"*" if option["me_voted"] else "-"} {option["answer"]} ({answer_votes} votes, {percent}%)")
    content_list.append(f"{expires} <t:{poll["expires"]}:R>")
    content = ""
    for line in content_list:
        content += f"> {line}\n"
    return content.strip("\n")


def build_message(message, roles, channels):
    """Build message object into text"""
    content = ""

    if message["interaction"]:
        content = f"╭──⤙ {message["interaction"]["username"]} used [{message["interaction"]["command"]}]"

    if "poll" in message:
        message["content"] = format_poll(message["poll"])

    if message["content"]:
        if content:
            content += "\n"
        content = replace_discord_emoji(message["content"])
        content = replace_mentions(content, message["mentions"])
        content = replace_roles(content, roles)
        content = replace_discord_url(content)
        content = replace_channels(content, channels)

    for embed in message["embeds"]:
        embed_url = embed["url"]
        if embed_url and not embed.get("hidden") and embed_url not in content:
            if content:
                content += "\n"
            if "main_url" not in embed:
                content += f"[({clean_type(embed["type"])} attachment)]({embed_url})"
            elif embed["type"] == "rich":
                content += f"(rich embed):\n{embed_url}"
            else:
                content += f"[({clean_type(embed["type"])} embed)]({embed_url})"

    for sticker in message["stickers"]:
        sticker_type = sticker["format_type"]
        if content:
            content += "\n"
        if sticker_type == 1:
            content += f"[(png sticker)]({sticker["name"]})"
        elif sticker_type == 2:
            content += f"[(apng sticker)]({sticker["name"]})"
        elif sticker_type == 3:
            content += f"(lottie sticker: {sticker["name"]})"
        else:
            content += f"[(gif sticker)]({sticker["name"]})"

    return content
