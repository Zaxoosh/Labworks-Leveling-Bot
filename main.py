import discord
import os
from dotenv import load_dotenv

# 1. Load the secret .env file
load_dotenv()

class Client(discord.Client):
    async def on_ready(self):
        print(f'Logged in as {self.user}')

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.content.startswith('!hello'):
            await message.channel.send('Hello!')

intents = discord.Intents.default()
intents.message_content = True

client = Client(intents=intents)

# 2. Use the variable, NOT the raw string
client.run(os.getenv('DISCORD_TOKEN'))