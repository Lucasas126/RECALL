import os

import dotenv
from telegram import Update

from src.telegram.bot import build_application
from src.utils.logger import setup_logging


def main() -> None:
    dotenv.load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment.")
    setup_logging("telegram")
    # Telegram remembers the last allowed_updates list; request all so button callbacks arrive.
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
