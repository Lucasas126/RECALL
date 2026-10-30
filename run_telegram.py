import os

import dotenv

from src.telegram.bot import build_application
from src.utils.logger import setup_logging


def main() -> None:
    dotenv.load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment.")
    setup_logging("telegram")
    build_application(token).run_polling()


if __name__ == "__main__":
    main()
