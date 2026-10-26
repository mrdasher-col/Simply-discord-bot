
from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
import string
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import discord
import yt_dlp
from discord.ext import commands


# ============================================================
# CONFIGURACIÓN
# ============================================================

PREFIX = "/"
TOKEN = "................."

FFMPEG_EXECUTABLE = os.getenv(
    "FFMPEG_EXECUTABLE",
    str(Path("bin/ffmpeg.exe")) if Path("bin/ffmpeg.exe").exists()
    else "ffmpeg"
)

AUDIO_VOLUME = 0.5
MAX_QUEUE_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("DiscordBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(PREFIX),
    description="Bot de utilidades y música",
    intents=intents,
    help_command=None,
    case_insensitive=True,
)


# ============================================================
# YT-DLP / FFMPEG
# ============================================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": False,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_retries": 2,
    "socket_timeout": 15,
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class YTDLSource(discord.PCMVolumeTransformer):
    """Fuente de audio con metadatos y control de volumen."""

    def __init__(
        self,
        source: discord.AudioSource,
        *,
        data: dict,
        volume: float = AUDIO_VOLUME,
    ):
        super().__init__(source, volume=volume)

        self.data = data
        self.title = data.get("title", "Audio desconocido")
        self.webpage_url = data.get("webpage_url", "")
        self.duration = data.get("duration")
        self.thumbnail = data.get("thumbnail")

    @classmethod
    async def extract(cls, query: str) -> dict:
        """Extrae metadatos sin bloquear el bucle de eventos."""

        loop = asyncio.get_running_loop()

        def extract_info():
            is_url = urlparse(query).scheme in ("http", "https")
            target = query if is_url else f"ytsearch1:{query}"

            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as extractor:
                data = extractor.extract_info(target, download=False)

            if not data:
                raise ValueError("No se encontraron resultados.")

            if data.get("entries") is not None:
                entries = [
                    item for item in data["entries"] if item
                ]
                if not entries:
                    raise ValueError("La búsqueda no devolvió resultados.")
                data = entries[0]

            if not data.get("url"):
                raise ValueError("No se pudo obtener el audio.")

            return data

        return await loop.run_in_executor(None, extract_info)

    @classmethod
    async def from_query(cls, query: str) -> "YTDLSource":
        data = await cls.extract(query)

        source = discord.FFmpegPCMAudio(
            data["url"],
            executable=FFMPEG_EXECUTABLE,
            **FFMPEG_OPTIONS,
        )

        return cls(source, data=data)


# ============================================================
# UTILIDADES
# ============================================================

def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "Desconocida"

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"

    return f"{minutes}:{seconds:02}"


def music_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient | None:
    """Conecta al canal del autor o devuelve el cliente existente."""

    if ctx.guild is None:
        await ctx.send("Este comando solo funciona dentro de un servidor.")
        return None

    voice = ctx.voice_client

    if voice and voice.is_connected():
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("Debes estar conectado a un canal de voz.")
            return None

        if voice.channel != ctx.author.voice.channel:
            await voice.move_to(ctx.author.voice.channel)

        return voice

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Debes estar conectado a un canal de voz.")
        return None

    return await ctx.author.voice.channel.connect()


# ============================================================
# SISTEMA DE MÚSICA CON COLA
# ============================================================

class Music(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_
        self.queues: dict[int, asyncio.Queue] = {}
        self.player_tasks: dict[int, asyncio.Task] = {}
        self.volumes: dict[int, float] = defaultdict(
            lambda: AUDIO_VOLUME
        )
        self.current: dict[int, dict | None] = {}

    def get_queue(self, guild_id: int) -> asyncio.Queue:
        if guild_id not in self.queues:
            self.queues[guild_id] = asyncio.Queue(
                maxsize=MAX_QUEUE_SIZE
            )

        return self.queues[guild_id]

    async def start_player(self, ctx: commands.Context):
        guild_id = ctx.guild.id
        task = self.player_tasks.get(guild_id)

        if task is None or task.done():
            task = asyncio.create_task(
                self.player_loop(ctx),
                name=f"music-player-{guild_id}",
            )
            self.player_tasks[guild_id] = task

    async def player_loop(self, ctx: commands.Context):
        guild_id = ctx.guild.id
        queue = self.get_queue(guild_id)

        try:
            while True:
                item = await queue.get()

                try:
                    voice = ctx.guild.voice_client

                    if not voice or not voice.is_connected():
                        break

                    source = await YTDLSource.from_query(item["query"])
                    source.volume = self.volumes[guild_id]

                    self.current[guild_id] = {
                        "title": source.title,
                        "url": source.webpage_url,
                        "duration": source.duration,
                        "requester": item["requester"],
                    }

                    finished = asyncio.Event()
                    playback_error = None
                    loop = asyncio.get_running_loop()

                    def after_playback(error):
                        nonlocal playback_error
                        playback_error = error
                        loop.call_soon_threadsafe(finished.set)

                    try:
                        voice.play(source, after=after_playback)
                    except Exception:
                        source.cleanup()
                        raise

                    embed = music_embed(
                        "🎵 Reproduciendo",
                        f"**[{source.title}]({source.webpage_url})**",
                    )
                    embed.add_field(
                        name="Duración",
                        value=format_duration(source.duration),
                        inline=True,
                    )
                    embed.add_field(
                        name="Solicitado por",
                        value=item["requester"].mention,
                        inline=True,
                    )

                    await ctx.send(embed=embed)
                    await finished.wait()

                    if playback_error:
                        logger.error(
                            "Error de reproducción: %s",
                            playback_error,
                        )

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Error en el reproductor")
                    await ctx.send(
                        f"⚠️ No se pudo reproducir esa canción: "
                        f"{type(exc).__name__}."
                    )
                finally:
                    self.current[guild_id] = None
                    queue.task_done()

        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()

            if self.player_tasks.get(guild_id) is current_task:
                self.player_tasks.pop(guild_id, None)

    @commands.command(name="play", aliases=["yt"])
    async def play(self, ctx: commands.Context, *, query: str):
        """Busca música por nombre o reproduce un enlace."""

        voice = await ensure_voice(ctx)
        if voice is None:
            return

        queue = self.get_queue(ctx.guild.id)

        try:
            queue.put_nowait({
                "query": query,
                "requester": ctx.author,
            })
        except asyncio.QueueFull:
            return await ctx.send(
                "⚠️ La cola está llena. Espera a que terminen "
                "algunas canciones."
            )

        await ctx.send(
            f"📥 Añadido a la cola: **{query[:200]}** "
            f"(posición aproximada: {queue.qsize()})"
        )

        await self.start_player(ctx)

    @commands.command(name="queue", aliases=["cola"])
    async def show_queue(self, ctx: commands.Context):
        """Muestra la canción actual y la cola."""

        guild_id = ctx.guild.id
        current = self.current.get(guild_id)
        queue = self.get_queue(guild_id)

        embed = music_embed("📜 Cola de música", "")

        if current:
            embed.add_field(
                name="Reproduciendo ahora",
                value=current["title"][:1024],
                inline=False,
            )

        items = list(queue._queue)

        if not items:
            description = "No hay canciones pendientes."
        else:
            description = "\n".join(
                f"`{i}.` {item['query'][:150]}"
                for i, item in enumerate(items[:10], start=1)
            )

            if len(items) > 10:
                description += f"\n... y {len(items) - 10} más."

        embed.add_field(
            name="Próximas canciones",
            value=description,
            inline=False,
        )

        await ctx.send(embed=embed)

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        """Pausa la reproducción."""

        voice = ctx.voice_client

        if voice and voice.is_playing():
            voice.pause()
            await ctx.send("⏸️ Música pausada.")
        else:
            await ctx.send("No hay música reproduciéndose.")

    @commands.command(name="resume", aliases=["unpause"])
    async def resume(self, ctx: commands.Context):
        """Reanuda la reproducción."""

        voice = ctx.voice_client

        if voice and voice.is_paused():
            voice.resume()
            await ctx.send("▶️ Reproducción reanudada.")
        else:
            await ctx.send("La música no está pausada.")

    @commands.command(name="skip", aliases=["next", "saltar"])
    async def skip(self, ctx: commands.Context):
        """Salta a la siguiente canción."""

        voice = ctx.voice_client

        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
            await ctx.send("⏭️ Canción saltada.")
        else:
            await ctx.send("No hay ninguna canción reproduciéndose.")

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx: commands.Context, volume: int):
        """Cambia el volumen entre 0 y 100."""

        if not 0 <= volume <= 100:
            return await ctx.send(
                "El volumen debe estar entre 0 y 100."
            )

        guild_id = ctx.guild.id
        self.volumes[guild_id] = volume / 100

        voice = ctx.voice_client

        if voice and isinstance(
            voice.source, discord.PCMVolumeTransformer
        ):
            voice.source.volume = volume / 100

        await ctx.send(f"🔊 Volumen establecido en **{volume}%**.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        """Detiene la música, vacía la cola y desconecta."""

        guild_id = ctx.guild.id
        voice = ctx.voice_client

        queue = self.get_queue(guild_id)

        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

        task = self.player_tasks.get(guild_id)

        if task and not task.done():
            task.cancel()

        if voice and voice.is_connected():
            if voice.is_playing() or voice.is_paused():
                voice.stop()

            await voice.disconnect(force=True)

        self.current[guild_id] = None
        await ctx.send("⏹️ Música detenida y cola vaciada.")

    @commands.command(name="leave", aliases=["disconnect"])
    async def leave(self, ctx: commands.Context):
        """Desconecta el bot del canal de voz."""

        voice = ctx.voice_client

        if voice and voice.is_connected():
            await voice.disconnect(force=True)
            await ctx.send("👋 Desconectado del canal de voz.")
        else:
            await ctx.send("No estoy conectado a un canal de voz.")

    @commands.command(name="local")
    async def local(self, ctx: commands.Context, *, filename: str):
        """Reproduce un archivo de audio local permitido."""

        voice = await ensure_voice(ctx)
        if voice is None:
            return

        path = Path(filename).expanduser().resolve()

        if not path.is_file():
            return await ctx.send("No se encontró ese archivo.")

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                str(path),
                executable=FFMPEG_EXECUTABLE,
                options="-vn",
            ),
            volume=self.volumes[ctx.guild.id],
        )

        if voice.is_playing() or voice.is_paused():
            voice.stop()

        voice.play(
            source,
            after=lambda error: logger.error(
                "Error en audio local: %s", error
            ) if error else None,
        )

        await ctx.send(f"🎵 Reproduciendo archivo local: **{path.name}**")

    async def cog_unload(self):
        for task in self.player_tasks.values():
            task.cancel()


# ============================================================
# COMANDOS GENERALES
# ============================================================

@bot.command(name="hello", aliases=["hola"])
async def hello(ctx: commands.Context):
    await ctx.send(f"¡Hola! Soy **{bot.user.name}** 🤖")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latencia: **{latency} ms**")


@bot.command(name="joined")
async def joined(ctx: commands.Context, member: discord.Member):
    if member.joined_at is None:
        return await ctx.send(
            "No se pudo determinar cuándo ingresó ese miembro."
        )

    await ctx.send(
        f"**{member.display_name}** se unió "
        f"{discord.utils.format_dt(member.joined_at, style='F')}."
    )


@bot.command(name="coinflip", aliases=["moneda"])
async def coinflip(ctx: commands.Context):
    result = random.choice(["Cara", "Cruz"])
    await ctx.send(f"🪙 Resultado: **{result}**")


@bot.command(name="gen_pasw", aliases=["password"])
async def gen_pasw(ctx: commands.Context):
    """Genera una contraseña y trata de enviarla por mensaje privado."""

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+"

    password = "".join(
        secrets.choice(alphabet) for _ in range(20)
    )

    try:
        await ctx.author.send(
            f"🔑 Tu contraseña generada es:\n`{password}`\n"
            "Guárdala en un lugar seguro."
        )
        await ctx.send("🔐 Te envié la contraseña por mensaje privado.")
    except discord.Forbidden:
        await ctx.send(
            "No puedo enviarte un mensaje privado. "
            "Revisa tu configuración de privacidad e inténtalo de nuevo."
        )







## RNG GAMBLING!!



def rng():
    rng_decidir = random.choices(
        ["Comun 🥚 80%", "Raro 🌀15%", "EEEpico 🦕 4%", "🍭 Legendario 1%" , "🫆  imposible 0.5%" , "👀  Haker detectado 0.3%" , "😭 admin 0.02%" , ":fire:  0.0000001% DesemPLEADOOOOOOOO"],
        weights=[80, 15, 4, 1, 0.5, 0.3, 0.02, 0.00001],
        k=1
    )
    return rng_decidir[0]



@bot.command('rng')
async def rng_command(ctx):
    result = rng()
    await ctx.send(f"🎲 Has obtenido un item **{result}**!")





















## MEME

@bot.command(name="mem", aliases=["meme"])
async def mem(ctx: commands.Context):
    img_dir = Path("./img")

    # Buscar todas las imágenes disponibles
    images = [
        *img_dir.glob("*.jpg"),
        *img_dir.glob("*.jpeg"),
        *img_dir.glob("*.png"),
        *img_dir.glob("*.webp"),
    ]

    if not images:
        return await ctx.send(
            "⚠️ No hay imágenes en la carpeta `img`."
        )

    # Elegir una imagen aleatoria
    image_path = random.choice(images)

    try:
        await ctx.send(file=discord.File(image_path))
    except discord.HTTPException:
        await ctx.send("❌ No se pudo enviar la imagen.")



import requests

## PATOS!
def get_duck():
    url = 'https://random-d.uk/api/v2/random'
    res = requests.get(url)
    data = res.json()
    return data['url']

@bot.command('duck')
async def duck(ctx):
    image_url = get_duck()
    await ctx.send(image_url)






## Animales!
def get_animal():
    url_patos = 'https://random-d.uk/api/v2/random'
    url_perros = 'https://random.dog/woof.json'
    url_zoros = 'https://randomfox.ca/floof/'
    random_url = random.choice([url_patos, url_perros, url_zoros])
    res = requests.get(random_url)
    data = res.json()
    if random_url == url_zoros:
        return data['image']
    
    return data['url']

@bot.command('animal')
async def animal(ctx):
    image_url = get_animal()
    if image_url.endswith(('.mp4', '.webm')):
        get_animal()  # Llamar recursivamente hasta obtener una imagen
    else:
        await ctx.send(image_url)




# ============================================================
# AYUDA PERSONALIZADA
# ============================================================

@bot.command(name="help", aliases=["ayuda"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="🤖 Ayuda del bot",
        description=(
            f"Prefijo: `{PREFIX}`\n"
            "También puedes mencionar al bot para usar comandos."
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Utilidades",
        value=(
            "`/help` — Mostrar esta ayuda\n"
            "`/ping` — Ver latencia\n"
            "`/hello` — Saludar\n"
            "`/joined @usuario` — Fecha de ingreso\n"
            "`/coinflip` — Lanzar una moneda\n"
            "`/gen_pasw` — Generar contraseña privada\n"
            "`/mem` — Enviar un meme"
        ),
        inline=False,
    )

    embed.add_field(
        name="Música",
        value=(
            "`/play <nombre o URL>` — Buscar y reproducir\n"
            "`/yt <nombre o URL>` — Alias de play\n"
            "`/queue` — Ver cola\n"
            "`/pause` — Pausar\n"
            "`/resume` — Reanudar\n"
            "`/skip` — Saltar canción\n"
            "`/volume <0-100>` — Cambiar volumen\n"
            "`/stop` — Detener y desconectar\n"
            "`/leave` — Desconectar\n"
            "`/local <archivo>` — Reproducir archivo local"
        ),
        inline=False,
    )

    embed.set_footer(text="Bot de utilidades y música")

    await ctx.send(embed=embed)


# ============================================================
# EVENTOS Y ERRORES
# ============================================================

@bot.event
async def on_ready():
    logger.info(
        "Conectado como %s (ID: %s)",
        bot.user,
        bot.user.id,
    )


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            f"⚠️ Falta un argumento: `{error.param.name}`. "
            f"Usa `{PREFIX}help` para ver los comandos."
        )

    if isinstance(error, commands.BadArgument):
        return await ctx.send(
            "⚠️ Uno de los argumentos no es válido. "
            f"Usa `{PREFIX}help` para consultar el formato."
        )

    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send(
            "Este comando solo funciona dentro de un servidor."
        )

    logger.error(
        "Error ejecutando %s: %s",
        getattr(ctx.command, "qualified_name", "desconocido"),
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )

    await ctx.send(
        "⚠️ Ocurrió un error inesperado al ejecutar el comando."
    )


# ============================================================
# INICIALIZACIÓN
# ============================================================

async def main():
    if not TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno DISCORD_TOKEN."
        )

    async with bot:
        await bot.add_cog(Music(bot))
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido manualmente.")
