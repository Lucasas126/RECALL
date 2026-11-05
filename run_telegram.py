import os

import dotenv

dotenv.load_dotenv()  # before the imports below: settings read environment variables when imported

from telegram import Update  # noqa: E402

from src.config.secrets import get_secret  # noqa: E402
from src.telegram.bot import build_application  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402


def main() -> None:
    token = get_secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment or in .config/secrets.env.")
    setup_logging("telegram")
    # Telegram remembers the last allowed_updates list; request all so button callbacks arrive.
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
