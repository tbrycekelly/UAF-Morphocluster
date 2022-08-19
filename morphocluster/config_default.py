from environs import Env

_env = Env()
_env.read_env()

# Redis (LRU for caching)
REDIS_LRU_URL = "redis://redis-lru:6379/0"

# Redis for rq
RQ_REDIS_URL = "redis://redis-rq:6379/0"

# Database connection
SQLALCHEMY_DATABASE_URI = (
    "postgresql://morphocluster:morphocluster@postgres/morphocluster"
)
SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_DATABASE_OPTIONS = {"connect_args": {"options": "-c statement_timeout=240s"}}

# Project export directory
PROJECT_EXPORT_DIR = "/data/export"

# Save the results of accept_recommended_objects
# to enable the calculation of scores like average precision
SAVE_RECOMMENDATION_STATS = False

DATASET_PATH = "/data"

# ORDER BY clause for node_get_next_unfilled
NODE_GET_NEXT_UNFILLED_ORDER_BY = "largest"

PREFERRED_URL_SCHEME = None

# Show the title (object_id, node_id) of cluster members
FRONTEND_SHOW_MEMBER_TITLE = _env.bool("FRONTEND_SHOW_MEMBER_TITLE", default=True)
