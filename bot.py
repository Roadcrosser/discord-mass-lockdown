import discord
import yaml

bot = discord.Client()

bot.ready = False

with open("config.yaml") as o:
    config = yaml.load(o.read(), Loader=yaml.FullLoader)


def setup_bot():
    bot.GUILD = bot.get_guild(config["GUILD_ID"])

    bot.AUTHORIZED_ROLE = (
        bot.GUILD.get_role(config["AUTHORIZED_ROLE_ID"]) if bot.GUILD else None
    )

    bot.STAFF_ROLE_ID = config["STAFF_ROLE_ID"]

    bot.ANNOUNCE_CHANNEL = (
        bot.GUILD.get_channel(config["ANNOUNCE_CHANNEL_ID"]) if bot.GUILD else None
    )

    bot.LOCKDOWN_ANNOUNCEMENT = config["LOCKDOWN_ANNOUNCEMENT"]
    bot.UNLOCKDOWN_ANNOUNCEMENT = config["UNLOCKDOWN_ANNOUNCEMENT"]

    bot.DEVELOPER_ID = config["DEVELOPER_ID"]

    bot.LOCKED_DOWN_CHANNELS = set()


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
        or not message.guild
        or message.guild.id != bot.GUILD.id
    ):
        return

    if message.content and (
        message.author.guild_permissions.manage_guild
        or message.author.id == bot.DEVELOPER_ID
        or bot.STAFF_ROLE_ID in [r.id for r in message.author.roles]
    ):
        for cmd in COMMAND_MAP:
            if message.content.lower().startswith(cmd):
                args = message.content[len(cmd) :].strip()
                await COMMAND_MAP[cmd](message, args)
                return


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


async def announce_lockdown(lockdown):
    if (
        not bot.ANNOUNCE_CHANNEL
        or not bot.ANNOUNCE_CHANNEL.permissions_for(
            bot.ANNOUNCE_CHANNEL.guild.me
        ).send_messages
    ):
        return

    message = bot.LOCKDOWN_ANNOUNCEMENT if lockdown else bot.UNLOCKDOWN_ANNOUNCEMENT

    if not message:
        return

    await bot.ANNOUNCE_CHANNEL.send(message)


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

        # try:
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

        success_channels.append(str(c.id))

        if lockdown:
            bot.LOCKED_DOWN_CHANNELS.add(c.id)
        elif c.id in bot.LOCKED_DOWN_CHANNELS:
            bot.LOCKED_DOWN_CHANNELS.remove(c.id)
        # except:
        #     fail_channels.append(c.mention)

    ret = "{}ocked down the following channels:\n```\n{}\n```".format(
        "L" if lockdown else "Unl", "\n".join(success_channels)
    )

    if fail_channels:
        ret += "\nFailed to {}ockdown the following channels: {}".format(
            "l" if lockdown else "unl", " ".join(fail_channels)
        )

    if success_channels:
        await announce_lockdown(lockdown)

    return ret


@bot.event
async def lockdown(message, args):
    channel_list = parse_channel_list(args)
    if not channel_list:
        channel_list = [
            c
            for c in bot.GUILD.text_channels
            if c.permissions_for(bot.GUILD.me).manage_channels and is_public_channel(c)
        ]

    async with message.channel.typing():
        ret = await perform_lockdown(channel_list, True)
    await message.channel.send(ret)


@bot.event
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
    await message.channel.send(ret)


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
