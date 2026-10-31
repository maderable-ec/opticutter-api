import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Importing each module's models populates ``Base.metadata`` so that
# ``alembic revision --autogenerate`` detects the tables.
from src.modules.additional_services.model import (  # noqa: F401
    AdditionalServiceModel,
)
from src.modules.branches.model import BranchModel  # noqa: F401
from src.modules.clients.model import ClientModel  # noqa: F401
from src.modules.notifications.model import NotificationModel  # noqa: F401
from src.modules.optimization_drafts.model import (  # noqa: F401
    OptimizationDraftModel,
)
from src.modules.orders import model as orders_model  # noqa: F401
from src.modules.preorders import model as preorders_model  # noqa: F401
from src.modules.print_jobs import model as print_jobs_model  # noqa: F401
from src.modules.products.model import ProductModel  # noqa: F401
from src.modules.projects.model import ProjectModel  # noqa: F401
from src.modules.settings.model import SettingsModel  # noqa: F401
from src.modules.users.login_event_model import UserLoginEventModel  # noqa: F401
from src.modules.users.model import UserModel  # noqa: F401
from src.modules.users.refresh_token_model import RefreshTokenModel  # noqa: F401
from src.shared.config import config as app_config
from src.shared.database import Base

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL from the project's configuration
config.set_main_option("sqlalchemy.url", app_config.DATABASE_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
