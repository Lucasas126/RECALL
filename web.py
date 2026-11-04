import dotenv

dotenv.load_dotenv()  # before the imports below: settings read environment variables when imported

import uvicorn  # noqa: E402

from src.config.settings import WEB_HOST, WEB_PORT  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402
from src.web.app import create_app  # noqa: E402


def main():
    setup_logging("web")
    uvicorn.run(create_app(), host=WEB_HOST, port=WEB_PORT)


if __name__ == "__main__":
    main()
