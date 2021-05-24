import discord
import datetime
from discord import channel
import yaml

bot = discord.Client(
    intents=discord.Intents(guilds=True, guild_messages=True, members=True)
)

bot.ready = False

with open("config.yaml", encoding="utf-8") as o:
    config = yaml.load(o.read(), Loader=yaml.FullLoader)


def cull_recent_member_cache(ts=None):
    if bot.RECENT_JOIN_THRESHOLD <= 0:
        return

    if not ts:
        ts = datetime.datetime.utcnow()

    cutoff_ts = ts - datetime.timedelta(seconds=bot.RECENT_JOIN_THRESHOLD)

    bot.RECENT_MEMBER_CACHE = [
        m
        for m in bot.RECENT_MEMBER_CACHE
        # It's easier to cull members who leave here than on leave
        if bot.GUILD.get_member(m.id)
        # Cutoff is inclusive
        and m.joined_at >= cutoff_ts
    ]


def setup_bot():
    bot.GUILD = bot.get_guild(config["GUILD_ID"])

    bot.AUTHORIZED_ROLE = (
        bot.GUILD.get_role(config["AUTHORIZED_ROLE_ID"]) if bot.GUILD else None
    )

    bot.STAFF_ROLE_ID = config["STAFF_ROLE_ID"]

    bot.ANNOUNCE_CHANNEL = config["ANNOUNCE_CHANNEL_ID"]

    if bot.ANNOUNCE_CHANNEL != "all":
        bot.ANNOUNCE_CHANNEL = (
            bot.GUILD.get_channel(bot.ANNOUNCE_CHANNEL) if bot.GUILD else None
        )

    bot.LOCKDOWN_ANNOUNCEMENT = config["LOCKDOWN_ANNOUNCEMENT"]
    bot.UNLOCKDOWN_ANNOUNCEMENT = config["UNLOCKDOWN_ANNOUNCEMENT"]

    bot.MENTION_THRESHOLD = config["MENTION_THRESHOLD"]

    bot.STAFF_CHANNEL = (
        bot.GUILD.get_channel(config["STAFF_CHANNEL_ID"]) if bot.GUILD else None
    )

    bot.RECENT_JOIN_THRESHOLD = config["RECENT_JOIN_THRESHOLD"]

    bot.DEVELOPER_ID = config["DEVELOPER_ID"]

    bot.LOCKED_DOWN_CHANNELS = set()

    bot.ANNOUNCE_MESSAGES = {}

    bot.AUTOLOCKDOWN_IN_PROGRESS = False

    bot.RECENT_MEMBER_CACHE = None

    if bot.RECENT_JOIN_THRESHOLD > 0:
        bot.RECENT_MEMBER_CACHE = bot.GUILD.members
        cull_recent_member_cache()


@bot.event
async def on_ready():
    setup_bot()
    print(
        f"Running on {bot.user.name}#{bot.user.discriminator} ({bot.user.id}) in guild: {bot.GUILD.name if bot.GUILD else '[Error] Guild not found.'}"
    )
    bot.ready = True


@bot.event
async def on_message(message):
    if (
        not bot.ready
        or message.author.bot
        or not message.content
        or not message.guild
        or message.guild.id != bot.GUILD.id
    ):
        return

    if (
        # Check auto-lockdown is enabled
        bot.MENTION_THRESHOLD > 0
        # Check auto-lockdown not already in progress
        and not bot.AUTOLOCKDOWN_IN_PROGRESS
        # Check channel is public
        and is_public_channel(message.channel)
        # Check for no roles (@everyone counts as a role internally)
        and len(message.author.roles) == 1
        # Check that mention regex search count exceeds threshold
        and len(message.mentions) >= bot.MENTION_THRESHOLD
    ):
        await execute_auto_lockdown(message)

    if (
        message.author.guild_permissions.manage_guild
        or message.author.id == bot.DEVELOPER_ID
        or bot.STAFF_ROLE_ID in [r.id for r in message.author.roles]
    ):
        for cmd in COMMAND_MAP:
            if message.content.lower().startswith(cmd):
                args = message.content[len(cmd) :].strip()
                await COMMAND_MAP[cmd](message, args)
                break


@bot.event
async def on_member_join(member):
    bot.RECENT_MEMBER_CACHE.append(member)
    cull_recent_member_cache()


def is_public_channel(channel):
    # Definition of a public channel:
    # (Will revert to None)
    #
    # @everyone role
    #    - Read messages: None/True
    #    - Send messages: None/True
    #
    # Authorized role
    #    - Read messages: None/True
    #    - Send messages: None/True

    default_role_override = channel.overwrites_for(channel.guild.default_role)
    authorized_role_override = channel.overwrites_for(bot.AUTHORIZED_ROLE)

    return all(
        [
            i in [None, True]
            for i in [
                default_role_override.read_messages,
                default_role_override.send_messages,
                authorized_role_override.read_messages,
                authorized_role_override.send_messages,
            ]
        ]
    )


def get_public_channels():
    return [
        c
        for c in bot.GUILD.text_channels
        if c.permissions_for(bot.GUILD.me).manage_channels and is_public_channel(c)
    ]


def parse_channel_list(args):
    if not args:
        return []

    arg_channels = args.split()
    affected_channels = set()

    for c in arg_channels:

        c = c.lower()
        try:
            c = int(c.strip("<#>"))
        except:
            pass
        affected_channels.add(c)

    return [
        c
        for c in bot.GUILD.channels
        if (c.id in affected_channels)
        or (c.name in affected_channels)
        and not isinstance(c, discord.TextChannel)
    ]


async def announce_lockdown(channel_list, lockdown):
    if not bot.ANNOUNCE_CHANNEL:
        return

    to_announce = channel_list
    if bot.ANNOUNCE_CHANNEL != "all":
        to_announce = [bot.ANNOUNCE_CHANNEL]

    for c in to_announce:
        if not c.permissions_for(c.guild.me).send_messages:
            continue

        message = bot.LOCKDOWN_ANNOUNCEMENT if lockdown else bot.UNLOCKDOWN_ANNOUNCEMENT

        if message:
            msg = await c.send(message)
            if c.permissions_for(c.guild.me).manage_messages and lockdown:
                try:
                    await msg.pin(reason="[Mass Lockdown Announcement]")
                    bot.ANNOUNCE_MESSAGES[c.id] = msg
                except:
                    pass

        if c.permissions_for(c.guild.me).manage_messages and not lockdown:
            pinned_msg = bot.ANNOUNCE_MESSAGES.pop(c.id, None)
            if pinned_msg:
                try:
                    await pinned_msg.unpin(reason="[Mass Unlockdown Announcement]")
                except:
                    pass


async def perform_lockdown(channel_list, lockdown):
    success_channels = []
    fail_channels = []

    for c in channel_list:
        default_role_override = c.overwrites_for(c.guild.default_role)
        authorized_role_override = c.overwrites_for(bot.AUTHORIZED_ROLE)
        bot_override = c.overwrites_for(c.guild.me)

        if lockdown:
            default_role_override.send_messages = False
            authorized_role_override.send_messages = True
            bot_override.send_messages = True
        else:
            default_role_override.send_messages = None
            authorized_role_override.send_messages = None
            bot_override.send_messages = None

        try:
            for i, u in [
                (c.guild.default_role, default_role_override),
                (bot.AUTHORIZED_ROLE, authorized_role_override),
                (c.guild.me, bot_override),
            ]:
                if u.is_empty():
                    u = None

                await c.set_permissions(
                    i,
                    overwrite=u,
                    reason="[Mass {}ockdown]".format("L" if lockdown else "Unl"),
                )

            success_channels.append(c)

            if lockdown:
                bot.LOCKED_DOWN_CHANNELS.add(c.id)
            elif c.id in bot.LOCKED_DOWN_CHANNELS:
                bot.LOCKED_DOWN_CHANNELS.remove(c.id)
        except:
            fail_channels.append(c.mention)

    ret = "{}ocked down the following channels:\n```\n{}\n```".format(
        "L" if lockdown else "Unl", "\n".join([str(c.id) for c in success_channels])
    )

    if fail_channels:
        ret += "\nFailed to {}ockdown the following channels: {}".format(
            "l" if lockdown else "unl", " ".join(fail_channels)
        )

    if success_channels:
        await announce_lockdown(success_channels, lockdown)

    return ret


async def lockdown(message, args):
    channel_list = parse_channel_list(args)
    if not channel_list:
        channel_list = get_public_channels()

    async with message.channel.typing():
        ret = await perform_lockdown(channel_list, True)
    await message.channel.send(ret)


async def unlockdown(message, args):
    channel_list = parse_channel_list(args)
    if not channel_list:
        channel_list = [
            c
            for c in bot.GUILD.text_channels
            if c.permissions_for(bot.GUILD.me).manage_channels
            and c.id in bot.LOCKED_DOWN_CHANNELS
        ]
        if not channel_list:
            await message.channel.send(
                "Error: No locked down channels were cached (or had no permissions to modify them).\nPlease specify list of IDs to unlockdown."
            )
            return

    async with message.channel.typing():
        ret = await perform_lockdown(channel_list, False)

    bot.AUTOLOCKDOWN_IN_PROGRESS = False
    await message.channel.send(ret)


async def execute_auto_lockdown(message):
    bot.AUTOLOCKDOWN_IN_PROGRESS = True

    channel_list = get_public_channels()

    staff_channel_accessible = (
        bot.STAFF_CHANNEL
        and bot.STAFF_CHANNEL.permissions_for(bot.STAFF_CHANNEL.guild.me).send_messages
    )

    if staff_channel_accessible:
        staff_announce_msg = f"{message.author.mention} ({message.author.id}) mentioned `{len(message.mentions)}` members in {message.channel.mention}."

        if bot.RECENT_JOIN_THRESHOLD > 0:
            cull_recent_member_cache(message.created_at)
            staff_announce_msg += (
                f"\nMembers who joined in the last {bot.RECENT_JOIN_THRESHOLD} seconds: "
                + " ".join([m.mention for m in bot.RECENT_MEMBER_CACHE])
            )

        staff_announce_msg += (
            "\n\nNow locking down the following channels: "
            + " ".join([c.mention for c in channel_list])
        )

        await bot.STAFF_CHANNEL.send(staff_announce_msg)

    ret = await perform_lockdown(channel_list, True)

    if staff_channel_accessible:
        await bot.STAFF_CHANNEL.send(ret)


_ = None


async def evaluate(message, args):
    if message.author.id != bot.DEVELOPER_ID:
        return

    global _

    if args.split(" ", 1)[0] == "await":
        try:
            _ = await eval(args.split(" ", 1)[1])
            await message.channel.send(_)
        except Exception as e:
            await message.channel.send("```\n" + str(e) + "\n```")
    else:
        try:
            _ = eval(args)
            await message.channel.send(_)
        except Exception as e:
            await message.channel.send("```\n" + str(e) + "\n```")
    return True


COMMAND_MAP = {
    **{i: lockdown for i in config["LOCKDOWN_COMMANDS"]},
    **{i: unlockdown for i in config["UNLOCKDOWN_COMMANDS"]},
}

if config["EVAL_COMMAND"]:
    COMMAND_MAP[config["EVAL_COMMAND"]] = evaluate

bot.run(config["TOKEN"])
