import dotenv
import uvicorn

from src.config.settings import WEB_HOST, WEB_PORT
from src.utils.logger import setup_logging
from src.web.app import create_app

dotenv.load_dotenv()


def main():
    setup_logging("web")
    uvicorn.run(create_app(), host=WEB_HOST, port=WEB_PORT)


if __name__ == "__main__":
    main()
