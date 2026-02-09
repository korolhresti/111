import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Hello!')

if __name__ == '__main__':
    application = ApplicationBuilder().token('YOUR_TOKEN_HERE').build()
    # Register handlers
    application.add_handler(CommandHandler('start', start))
    # Run the bot
    application.run_polling()