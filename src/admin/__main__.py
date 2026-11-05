import dotenv

# like web.py and run_telegram.py: settings read environment variables when imported; existing variables win
dotenv.load_dotenv()

from src.admin.users import main  # noqa: E402

raise SystemExit(main())
