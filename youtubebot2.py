#!/usr/bin/env python3
import re

import discord
from discord.ext import commands
import yt_dlp
import urllib
import asyncio
import threading
import os
import shutil
import sys
import subprocess as sp
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
PREFIX = os.getenv('BOT_PREFIX', '.')
YTDL_FORMAT = os.getenv('YTDL_FORMAT', 'worstaudio')
PRINT_STACK_TRACE = os.getenv('PRINT_STACK_TRACE', '1').lower() in ('true', 't', '1')
BOT_REPORT_COMMAND_NOT_FOUND = os.getenv('BOT_REPORT_COMMAND_NOT_FOUND', '1').lower() in ('true', 't', '1')
BOT_REPORT_DL_ERROR = os.getenv('BOT_REPORT_DL_ERROR', '0').lower() in ('true', 't', '1')
try:
    COLOR = int(os.getenv('BOT_COLOR', 'ff0000'), 16)
except ValueError:
    print('il BOT_COLOR in .env non è un colore valido')
    print('uso il default ff0000')
    COLOR = 0xff0000

bot = commands.Bot(command_prefix=PREFIX, intents=discord.Intents(voice_states=True, guilds=True, guild_messages=True, message_content=True))
queues = {} # {server_id: 'queue': [(vid_file, info), ...], 'loop': bool}

async def keep_alive():
    while True:
        await bot.change_presence(status=discord.Status.online, activity=discord.Game('Keeping alive...'))
        await asyncio.sleep(15)  # change presence every 60 seconds

    
def main():
    if TOKEN is None:
        return ("Nessun token dato. crea un file .env file contenente il token.\n")
    try: bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired as error:
        return error


@bot.command(name='queue', aliases=['q'])
async def queue(ctx: commands.Context, *args):
    try: queue = queues[ctx.guild.id]['queue']
    except KeyError: queue = None
    if queue == None:
        await ctx.send('il bot non sta suonando!')
    else:
        title_str = lambda val: '‣ %s\n\n' % val[1] if val[0] == 0 else '**%2d:** %s\n' % val
        queue_str = ''.join(map(title_str, enumerate([i[1]["title"] for i in queue])))
        embedVar = discord.Embed(color=COLOR)
        embedVar.add_field(name='Riproduco:', value=queue_str)
        await ctx.send(embed=embedVar)
    if not await sense_checks(ctx):
        return

@bot.command(name='skip', aliases=['s'])
async def skip(ctx: commands.Context, *args):
    try: queue_length = len(queues[ctx.guild.id]['queue'])
    except KeyError: queue_length = 0
    if queue_length <= 0:
        await ctx.send('il bot non sta suonando!')
    if not await sense_checks(ctx):
        return

    try: n_skips = int(args[0])
    except IndexError:
        n_skips = 1
    except ValueError:
        if args[0] == 'all': n_skips = queue_length
        else: n_skips = 1
    if n_skips == 1:
        message = 'Salto traccia'
    elif n_skips < queue_length:
        message = f'Salto `{n_skips}` di `{queue_length}` tracce'
    else:
        message = 'Salto tutte le tracce'
        n_skips = queue_length
    await ctx.send(message)

    voice_client = get_voice_client_from_channel_id(ctx.author.voice.channel.id)
    for _ in range(n_skips):
        queues[ctx.guild.id]['queue'].pop(0)
    if queues[ctx.guild.id]['queue']:
        voice_client.play(discord.FFmpegOpusAudio(queues[ctx.guild.id]['queue'][0][0]), after=lambda error=None, connection=voice_client, server_id=ctx.guild.id:
                                                                          after_track(error, connection, server_id))
    else:
        voice_client.stop()

@bot.command(name='play', aliases=['p'])
async def play(ctx: commands.Context, *args):
    voice_state = ctx.author.voice
    if not await sense_checks(ctx, voice_state=voice_state):
        return

    query = ' '.join(args)
    # this is how it's determined if the url is valid (i.e. whether to search or not) under the hood of yt-dlp
    will_need_search = not urllib.parse.urlparse(query).scheme

    server_id = ctx.guild.id

    # source address as 0.0.0.0 to force ipv4 because ipv6 breaks it for some reason
    # this is equivalent to --force-ipv4 (line 312 of https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/options.py)
    await ctx.send(f'Cerco `{query}`...')
    with yt_dlp.YoutubeDL({'format': YTDL_FORMAT,
                           'source_address': '0.0.0.0',
                           'default_search': 'ytsearch',
                           'outtmpl': '%(id)s.%(ext)s',
                           'noplaylist': True,
                           'allow_playlist_files': False,
                           # 'progress_hooks': [lambda info, ctx=ctx: video_progress_hook(ctx, info)],
                           # 'match_filter': lambda info, incomplete, will_need_search=will_need_search, ctx=ctx: start_hook(ctx, info, incomplete, will_need_search),
                           'paths': {'home': f'./dl/{server_id}'}}) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
        except yt_dlp.utils.DownloadError as err:
            await notify_about_failure(ctx, err)
            return

        if 'entries' in info:
            info = info['entries'][0]
        # send link if it was a search, otherwise send title as sending link again would clutter chat with previews
        await ctx.send('Scarico ' + (f'https://youtu.be/{info["id"]}' if will_need_search else f'`{info["title"]}`'))
        try:
            ydl.download([query])
        except yt_dlp.utils.DownloadError as err:
            await notify_about_failure(ctx, err)
            return
        path = f'./dl/{server_id}/{info["id"]}.{info["ext"]}'
        try:
            queues[server_id]['queue'].append((path, info))
        except KeyError: # first in queue
            queues[server_id] = {'queue': [(path, info)], 'loop': False}
            try: connection = await voice_state.channel.connect()
            except discord.ClientException: connection = get_voice_client_from_channel_id(voice_state.channel.id)
            connection.play(discord.FFmpegOpusAudio(path), after=lambda error=None, connection=connection, server_id=server_id:
                                                             after_track(error, connection, server_id))

@bot.command('loop', aliases=['l'])
async def loop(ctx: commands.Context, *args):
    if not await sense_checks(ctx):
        return
    try:
        loop = queues[ctx.guild.id]['loop']
    except KeyError:
        await ctx.send('il bot non sta suonando!')
        return
    queues[ctx.guild.id]['loop'] = not loop

    await ctx.send('looping è ora ' + ('on' if not loop else 'off'))

def get_voice_client_from_channel_id(channel_id: int):
    for voice_client in bot.voice_clients:
        if voice_client.channel.id == channel_id:
            return voice_client

def after_track(error, connection, server_id):
    if error is not None:
        print(error)
    try:
        last_video_path = queues[server_id]['queue'][0][0]
        if not queues[server_id]['loop']:
            os.remove(last_video_path)
            queues[server_id]['queue'].pop(0)
    except KeyError: return # probably got disconnected
    if last_video_path not in [i[0] for i in queues[server_id]['queue']]: # check that the same video isn't queued multiple times
        try: os.remove(last_video_path)
        except FileNotFoundError: pass
    try: connection.play(discord.FFmpegOpusAudio(queues[server_id]['queue'][0][0]), after=lambda error=None, connection=connection, server_id=server_id:
                                                                          after_track(error, connection, server_id))
    except IndexError: # that was the last item in queue
        queues.pop(server_id) # directory will be deleted on disconnect
        asyncio.run_coroutine_threadsafe(safe_disconnect(connection), bot.loop).result()

async def safe_disconnect(connection):
    if not connection.is_playing():
        await connection.disconnect()

async def sense_checks(ctx: commands.Context, voice_state=None) -> bool:
    if voice_state is None: voice_state = ctx.author.voice
    if voice_state is None:
        await ctx.send('devi essere in un canale vocale per usarmi :C')
        return False

    if bot.user.id not in [member.id for member in ctx.author.voice.channel.members] and ctx.guild.id in queues.keys():
        await ctx.send('devi essere nel mio stesso canale vocale per usarmi :(')
        return False
    return True

@bot.event
async def on_voice_state_update(member: discord.User, before: discord.VoiceState, after: discord.VoiceState):
    if member != bot.user:
        return
    if before.channel is None and after.channel is not None: # joined vc
        return
    if before.channel is not None and after.channel is None: # disconnected from vc
        # clean up
        server_id = before.channel.guild.id
        try: queues.pop(server_id)
        except KeyError: pass
        try: shutil.rmtree(f'./dl/{server_id}/')
        except FileNotFoundError: pass

@bot.event
async def on_command_error(ctx: discord.ext.commands.Context, err: discord.ext.commands.CommandError):
    # now we can handle command errors
    if isinstance(err, discord.ext.commands.errors.CommandNotFound):
        if BOT_REPORT_COMMAND_NOT_FOUND:
            await ctx.send("comando non riconosciuto".format(PREFIX))
        return

    # we ran out of handlable exceptions, re-start. type_ and value are None for these
    sys.stderr.write(f'ECCEZIONE ERRORE NON TRACCIATO, {err=}')
    sys.stderr.flush()
    os.execl(sys.executable, sys.executable, *sys.argv)

@bot.event
async def on_ready():
    print(f'Login corretto come {bot.user.name}')
    
async def notify_about_failure(ctx: commands.Context, err: yt_dlp.utils.DownloadError):
    if BOT_REPORT_DL_ERROR:
        # remove shell colors for discord message
        sanitized = re.compile(r'\x1b[^m]*m').sub('', err.msg).strip()
        if sanitized[0:5].lower() == "error":
            # if message starts with error, strip it to avoid being redundant
            sanitized = sanitized[5:].strip(" :")
        await ctx.send('Ho fallito il download : {}'.format(sanitized))
    else:
        await ctx.send('scusa Ho fallito')
    return

@bot.command(name='kill', aliases=['k'])
async def kill(ctx: commands.Context, *args):
    voice_client = ctx.voice_client
    if voice_client:
        await voice_client.disconnect()
        await ctx.send('Il bot si è disconnesso dal canale vocale!')
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        await ctx.send('Il bot non è connesso a un canale vocale!')
    
        
    
if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemError as error:
        if PRINT_STACK_TRACE:
            raise KeyboardInterrupt()
        else:
            print(error)
            raise KeyboardInterrupt()

